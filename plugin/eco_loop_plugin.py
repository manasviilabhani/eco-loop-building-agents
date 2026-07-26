"""EnergyPlus Python Plugin: the in-simulation hook for the Eco-Loop closed
loop. Runs inside EnergyPlus's own embedded interpreter (stdlib only -- no
third-party packages available here, see docs/ARCHITECTURE.md). Talks to the
LLM agent (agent/service.py) over a local HTTP bridge.

Each simulated hour (on the hour):
  1. Reads sensor state via the Exchange API (zone mean air temp, PMV per
     zone, facility electricity demand, current setpoints).
  2. POSTs a compact JSON snapshot to the agent service.
  3. On a successful response, clamps (defense in depth -- the agent service
     already clamps too) and writes the new cooling/heating setpoints back
     via the Actuator API onto the Clg-SetP-Sch / Htg-SetP-Sch Schedule:Compact
     objects -- this is live, mid-simulation Forward Injection: all 5 zones
     share these two schedules, so one decision affects the whole building.
  4. On any failure (HTTP error, timeout, malformed response, agent
     couldn't commit a decision), holds the last known-good setpoints and
     logs a warning -- the simulation must never crash because the agent
     was slow or wrong.
"""

import json
import os
import time
import urllib.request
import urllib.error
import uuid

from pyenergyplus.plugin import EnergyPlusPlugin

AGENT_URL = "http://127.0.0.1:8765/decide"
HTTP_TIMEOUT_SECONDS = 30
DECISION_CADENCE_MINUTES = 60

SETPOINT_MIN_C = 22.0  # narrow occupied-hours safety envelope, see agent/tools.py
SETPOINT_MAX_C = 26.0
MIN_DEADBAND_C = 2.0
MAX_STEP_C = 0.5

ZONES = ["SPACE1-1", "SPACE2-1", "SPACE3-1", "SPACE4-1", "SPACE5-1"]


def _clamp_pair(cooling_c: float, heating_c: float, last_cooling_c: float | None, last_heating_c: float | None) -> tuple[float, float]:
    """Final backstop clamp, independent of whatever the agent service
    already did -- the plugin must never trust an upstream HTTP response
    enough to actuate a value that could crash the simulation
    (DualSetPointWithDeadBand fires fatally if heating >= cooling), or one
    that swings several degrees in a single cycle and overshoots the comfort
    band the other way (an early prompt-only version of the closed loop did
    exactly this -- see docs/ARCHITECTURE.md)."""
    if last_cooling_c is not None:
        cooling_c = last_cooling_c + max(-MAX_STEP_C, min(MAX_STEP_C, cooling_c - last_cooling_c))
    if last_heating_c is not None:
        heating_c = last_heating_c + max(-MAX_STEP_C, min(MAX_STEP_C, heating_c - last_heating_c))
    cooling = max(SETPOINT_MIN_C, min(SETPOINT_MAX_C, cooling_c))
    heating = max(SETPOINT_MIN_C, min(SETPOINT_MAX_C, heating_c))
    if heating > cooling - MIN_DEADBAND_C:
        heating = cooling - MIN_DEADBAND_C
    return cooling, heating


def _synthetic_carbon_intensity(hour: int) -> float:
    """Stand-in grid carbon-intensity curve (0-1): higher during the
    afternoon peak-demand window, lower overnight. A real deployment would
    pull this from a grid API (e.g. WattTime/electricityMap); documented as
    synthetic in docs/ARCHITECTURE.md."""
    if 13 <= hour <= 18:
        return 0.8
    if 6 <= hour <= 12 or 19 <= hour <= 21:
        return 0.5
    return 0.2


class EcoLoopController(EnergyPlusPlugin):
    def __init__(self):
        super().__init__()
        self.handles_initialized = False
        self.last_decision_hour_key = None
        self.last_good_cooling_c = None
        self.last_good_heating_c = None

        # Identifies this simulation run for the live dashboard view (see
        # agent/live_push.py) -- generated once per EnergyPlus process so
        # separate runs don't get plotted as one continuous series.
        #
        # The site is encoded as a run_id prefix rather than a new column,
        # deliberately: the live_decisions table is already deployed on a
        # hosted Supabase project, and a prefix keeps the live view
        # site-aware with no schema migration and no breakage of the rows
        # already in there (rows written before this change have no prefix
        # and are read back as the default site). scripts/run_comparison.py
        # sets ECO_LOOP_LOCATION when it launches EnergyPlus; a bare
        # `energyplus` invocation just gets the default.
        site = os.environ.get("ECO_LOOP_LOCATION", "chicago")
        # ECO_LOOP_RUN_ID lets the caller pin a stable id -- the realtime
        # daemon reuses one id per site per day so that re-running the day so
        # far replaces its own rows instead of piling up a new series every
        # time it refreshes.
        self.run_id = os.environ.get("ECO_LOOP_RUN_ID") or (
            f"{site}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        )
        # ECO_LOOP_MAX_HOUR clamps what reaches the dashboard to the hours
        # that have actually elapsed in the real world. The simulation still
        # runs the whole day (later hours cannot affect earlier ones, so
        # truncating the *display* is physically sound), but nothing beyond
        # the current wall-clock hour is published -- otherwise the "live"
        # chart would show the building's future.
        raw_max = os.environ.get("ECO_LOOP_MAX_HOUR", "")
        self.max_published_hour = int(raw_max) if raw_max.strip() else None
        self.hour_index = 0

        self.h = {}  # sensor/actuator handles, keyed by name

    def _init_handles(self, state):
        exch = self.api.exchange
        for zone in ZONES:
            self.h[f"temp_{zone}"] = exch.get_variable_handle(state, "Zone Mean Air Temperature", zone)
            self.h[f"pmv_{zone}"] = exch.get_variable_handle(
                state, "Zone Thermal Comfort Fanger Model PMV", f"{zone} PEOPLE 1"
            )
        self.h["outdoor_temp"] = exch.get_variable_handle(
            state, "Site Outdoor Air Drybulb Temperature", "Environment"
        )
        self.h["facility_demand"] = exch.get_variable_handle(
            state, "Facility Total Electricity Demand Rate", "Whole Building"
        )
        self.h["cooling_setpoint_sensor"] = exch.get_variable_handle(state, "Schedule Value", "Clg-SetP-Sch")
        self.h["heating_setpoint_sensor"] = exch.get_variable_handle(state, "Schedule Value", "Htg-SetP-Sch")

        self.h["cooling_actuator"] = exch.get_actuator_handle(
            state, "Schedule:Compact", "Schedule Value", "Clg-SetP-Sch"
        )
        self.h["heating_actuator"] = exch.get_actuator_handle(
            state, "Schedule:Compact", "Schedule Value", "Htg-SetP-Sch"
        )

        if any(v == -1 for v in self.h.values()):
            missing = [k for k, v in self.h.items() if v == -1]
            self.api.runtime.issue_severe(state, f"EcoLoopController: failed to resolve handles: {missing}")

        self.handles_initialized = True

    def _build_snapshot(self, state) -> dict:
        exch = self.api.exchange
        zones = {}
        for zone in ZONES:
            zones[zone] = {
                "temp_c": exch.get_variable_value(state, self.h[f"temp_{zone}"]),
                "pmv": exch.get_variable_value(state, self.h[f"pmv_{zone}"]),
            }
        hour = exch.hour(state)
        published = (
            self.max_published_hour is None or self.hour_index <= self.max_published_hour
        )
        return {
            # Omitting run_id is how a snapshot opts out of the live push:
            # agent/service.py only forwards rows that carry one.
            **({"run_id": self.run_id} if published else {}),
            "hour_index": self.hour_index,
            "sim_time": f"{exch.month(state):02d}-{exch.day_of_month(state):02d} {hour:02d}:00",
            "zones": zones,
            "outdoor_temp_c": exch.get_variable_value(state, self.h["outdoor_temp"]),
            "facility_demand_w": exch.get_variable_value(state, self.h["facility_demand"]),
            "cooling_setpoint_c": exch.get_variable_value(state, self.h["cooling_setpoint_sensor"]),
            "heating_setpoint_c": exch.get_variable_value(state, self.h["heating_setpoint_sensor"]),
            "carbon_intensity": _synthetic_carbon_intensity(hour),
        }

    def _call_agent(self, snapshot: dict) -> dict:
        data = json.dumps(snapshot).encode("utf-8")
        req = urllib.request.Request(
            AGENT_URL, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())

    def _apply_setpoints(self, state, cooling_c: float, heating_c: float):
        exch = self.api.exchange
        cooling_c, heating_c = _clamp_pair(cooling_c, heating_c, self.last_good_cooling_c, self.last_good_heating_c)
        exch.set_actuator_value(state, self.h["cooling_actuator"], cooling_c)
        exch.set_actuator_value(state, self.h["heating_actuator"], heating_c)
        self.last_good_cooling_c = cooling_c
        self.last_good_heating_c = heating_c

    def on_begin_timestep_before_predictor(self, state) -> int:
        if self.api.exchange.warmup_flag(state):
            return 0
        # EnergyPlus also runs internal sizing/load-component-report passes
        # (design days) outside the real weather-file RunPeriod; the agent
        # should only act on the actual comparison period, not sizing calcs.
        # KindOfSim == 3 is EnergyPlus's RunPeriodWeather.
        if self.api.exchange.kind_of_sim(state) != 3:
            return 0

        if not self.handles_initialized:
            self._init_handles(state)

        exch = self.api.exchange
        hour = exch.hour(state)
        day_key = (exch.day_of_month(state), hour)

        # Cadence gate: fire at most once per calendar hour (the first zone
        # timestep observed within that hour), matching DECISION_CADENCE_MINUTES.
        if day_key == self.last_decision_hour_key:
            if self.last_good_cooling_c is not None:
                self._apply_setpoints(state, self.last_good_cooling_c, self.last_good_heating_c)
            return 0
        self.last_decision_hour_key = day_key
        self.hour_index += 1

        snapshot = self._build_snapshot(state)

        try:
            result = self._call_agent(snapshot)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            self.api.runtime.issue_warning(state, f"EcoLoopController: agent call failed ({e}), holding last setpoints")
            result = {"ok": False}

        if result.get("ok"):
            self._apply_setpoints(state, result["cooling_setpoint_c"], result["heating_setpoint_c"])
        elif self.last_good_cooling_c is not None:
            self.api.runtime.issue_warning(state, "EcoLoopController: agent did not return a decision, holding last setpoints")
            self._apply_setpoints(state, self.last_good_cooling_c, self.last_good_heating_c)
        else:
            self.api.runtime.issue_warning(
                state, "EcoLoopController: no agent decision and no prior setpoint to hold; leaving schedule un-actuated"
            )

        return 0
