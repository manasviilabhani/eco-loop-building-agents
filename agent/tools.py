"""MCP tools exposed to the LLM agent. Registered on the FastMCP app in
mcp_server.py. Each tool reads/writes agent/shared_state.py, which the HTTP
handler (agent/service.py) populates from the latest EnergyPlus telemetry
POST before starting an agent decision turn.

set_hvac_setpoints is the only tool that produces a real control action; it
is clamped server-side here as a safety backstop independent of whatever the
LLM requests (the brief explicitly grades "self-correction" and robustness).

The [22C, 26C] range is a deliberately narrow occupied-hours safety envelope,
not just a physical sanity bound -- an early version allowed the full
[20C, 28C] range and the agent (even with a per-cycle rate limit) could still
drift monotonically to an extreme over many cycles if its directional
judgment was even slightly biased, overshooting comfort. A real facility
engineer would similarly configure a tight envelope around the building's
known-reasonable setpoint (23.9C in the original schedule) rather than
trusting any single automation layer with the full range.
"""

from mcp.server.fastmcp import FastMCP

from agent.shared_state import state

SETPOINT_MIN_C = 22.0
SETPOINT_MAX_C = 26.0
MIN_DEADBAND_C = 2.0  # heating setpoint must stay at least this far below cooling setpoint
MAX_STEP_C = 0.5  # max change from the current setpoint allowed per decision cycle
PMV_COMFORT_BAND = (-0.5, 0.5)

mcp_app = FastMCP("eco-loop-tools")


def clamp_setpoint(value_c: float) -> tuple[float, bool]:
    clamped = max(SETPOINT_MIN_C, min(SETPOINT_MAX_C, value_c))
    return clamped, clamped != value_c


def _rate_limit(requested_c: float, current_c: float | None) -> float:
    """Caps the change from the current setpoint to MAX_STEP_C per decision
    cycle. An early prompt-only version of this constraint ("make small
    incremental adjustments") was not reliable enough on its own: the model
    still occasionally jumped several degrees in one cycle and overshot the
    comfort band the other way. Enforcing the step size server-side, not
    just describing it in the prompt, is what actually holds it."""
    if current_c is None:
        return requested_c
    delta = max(-MAX_STEP_C, min(MAX_STEP_C, requested_c - current_c))
    return current_c + delta


def clamp_setpoint_pair(cooling_c: float, heating_c: float) -> tuple[float, float, bool]:
    """Rate-limits each from the current setpoint, clamps each to the safe
    range, then enforces a minimum heating/cooling deadband -- EnergyPlus
    fatally errors (DualSetPointWithDeadBand) if the effective heating
    setpoint is ever >= the cooling setpoint, and an LLM is not guaranteed to
    respect that physical constraint on its own."""
    snap = state.snapshot
    current_cool = snap.cooling_setpoint_c if snap else None
    current_heat = snap.heating_setpoint_c if snap else None

    limited_cool = _rate_limit(cooling_c, current_cool)
    limited_heat = _rate_limit(heating_c, current_heat)

    cooling, cool_clamped = clamp_setpoint(limited_cool)
    heating, heat_clamped = clamp_setpoint(limited_heat)
    if heating > cooling - MIN_DEADBAND_C:
        heating = cooling - MIN_DEADBAND_C
        heat_clamped = True
    was_limited = (limited_cool != cooling_c) or (limited_heat != heating_c)
    return cooling, heating, (cool_clamped or heat_clamped or was_limited)


@mcp_app.tool()
def get_zone_state() -> dict:
    """Get the current air temperature (C) and PMV comfort index for every
    zone in the building, plus the current HVAC setpoints and outdoor grid
    carbon intensity (0-1, higher = dirtier)."""
    snap = state.snapshot
    if snap is None:
        return {"error": "no telemetry available yet"}
    return {
        "sim_time": snap.sim_time,
        "zones": {z: {"temp_c": r.temp_c, "pmv": r.pmv} for z, r in snap.zones.items()},
        "cooling_setpoint_c": snap.cooling_setpoint_c,
        "heating_setpoint_c": snap.heating_setpoint_c,
        "carbon_intensity": snap.carbon_intensity,
        "pmv_comfort_band": list(PMV_COMFORT_BAND),
    }


@mcp_app.tool()
def get_energy_metrics() -> dict:
    """Get the current facility electricity demand rate in watts."""
    snap = state.snapshot
    if snap is None:
        return {"error": "no telemetry available yet"}
    return {"facility_demand_w": snap.facility_demand_w}


@mcp_app.tool()
def get_comfort_index() -> dict:
    """Get per-zone PMV (Predicted Mean Vote) and whether each zone is
    within the target comfort band of [-0.5, 0.5]."""
    snap = state.snapshot
    if snap is None:
        return {"error": "no telemetry available yet"}
    lo, hi = PMV_COMFORT_BAND
    return {
        z: {"pmv": r.pmv, "in_band": lo <= r.pmv <= hi}
        for z, r in snap.zones.items()
    }


@mcp_app.tool()
def set_hvac_setpoints(cooling_setpoint_c: float, heating_setpoint_c: float, reasoning: str) -> dict:
    """Commit new building-wide HVAC setpoints (Celsius) for the next
    decision interval. Applies to the shared cooling/heating setpoint
    schedule used by all zones. Values are clamped to a safe range
    [20C, 28C] server-side regardless of what is requested. `reasoning`
    should briefly explain why, for the audit log."""
    applied_cool, applied_heat, was_clamped = clamp_setpoint_pair(cooling_setpoint_c, heating_setpoint_c)
    state.pending.cooling_setpoint_c = applied_cool
    state.pending.heating_setpoint_c = applied_heat
    state.pending.reasoning = reasoning
    state.pending.committed = True
    return {
        "applied_cooling_setpoint_c": applied_cool,
        "applied_heating_setpoint_c": applied_heat,
        "clamped": was_clamped,
    }


# --- Phase 3: self-healing error recovery tools ---


@mcp_app.tool()
def get_error_log(err_path: str, max_lines: int = 60) -> dict:
    """Read the tail of an EnergyPlus .err file for a failed run, to
    diagnose what went wrong."""
    try:
        with open(err_path) as f:
            lines = f.readlines()
        return {"tail": "".join(lines[-max_lines:])}
    except OSError as e:
        return {"error": str(e)}


@mcp_app.tool()
def list_schedule_names(idf_path: str) -> dict:
    """List the names of every Schedule:Compact and Schedule:Constant object
    already defined in the IDF. Use this before fixing a 'schedule not
    found' error, so you reference a real existing schedule instead of
    guessing a plausible-sounding name that may not exist."""
    from eppy.modeleditor import IDF

    if IDF.getiddname() is None:
        IDF.setiddname("/Applications/EnergyPlus-26-1-0/Energy+.idd")
    try:
        idf = IDF(idf_path)
        names = [o.Name for o in idf.idfobjects["SCHEDULE:COMPACT"]]
        names += [o.Name for o in idf.idfobjects["SCHEDULE:CONSTANT"]]
        return {"schedule_names": names}
    except Exception as e:
        return {"error": str(e)}


@mcp_app.tool()
def patch_idf_field(idf_path: str, object_type: str, object_name: str, field_name: str, new_value: str, out_path: str) -> dict:
    """Apply a targeted field edit to an IDF object (identified by object
    type + name) and save the result as a new IDF file at out_path. Used to
    autonomously fix a broken building model after a crashed run.
    `field_name` must be one of the object's real field names (see the
    error response for the valid list if you're unsure -- e.g. it is
    "Activity_Level_Schedule_Name", not "ActivityLevelScheduleName")."""
    from eppy.modeleditor import IDF

    if IDF.getiddname() is None:
        IDF.setiddname("/Applications/EnergyPlus-26-1-0/Energy+.idd")

    try:
        idf = IDF(idf_path)
        objs = idf.idfobjects[object_type.upper()]

        # Forgiving object_name match: EnergyPlus's own .err messages often
        # upper-case/truncate names (e.g. "SPACE2-1 PEOPLE" or "SPACE2-1"
        # for an object actually named "SPACE2-1 People"), and the model
        # tends to echo that imprecise form back rather than the exact
        # string. Try exact case-insensitive match first, then fall back to
        # "requested name is a prefix of the real name", which is enough to
        # disambiguate in a small building model.
        target = object_name.casefold()
        matches = [o for o in objs if str(getattr(o, "Name", "")).casefold() == target]
        if not matches:
            matches = [o for o in objs if str(getattr(o, "Name", "")).casefold().startswith(target)]
        if not matches:
            return {"error": f"no object of type {object_type} named {object_name} found"}
        obj = matches[0]

        # Forgiving field_name match: setattr on an eppy object silently
        # accepts ANY attribute name -- it does NOT validate against real
        # IDF fields, so a slightly-off field name (wrong case, missing
        # underscores) would silently no-op instead of erroring, leaving the
        # model with no signal that its fix didn't take effect. Normalize
        # both sides (strip underscores/spaces, lowercase) before matching,
        # rather than requiring the model to reproduce exact IDD casing.
        def _norm(s: str) -> str:
            return s.replace("_", "").replace(" ", "").casefold()

        field_lookup = {_norm(f): f for f in obj.fieldnames}
        real_field = field_lookup.get(_norm(field_name))
        if real_field is None:
            return {
                "error": f"'{field_name}' is not a valid field on {object_type}. Valid fields: {obj.fieldnames}"
            }
        setattr(obj, real_field, new_value)
        idf.saveas(out_path)
        state.pending_patch.out_path = out_path
        state.pending_patch.committed = True
        return {"status": "patched", "out_path": out_path}
    except Exception as e:
        return {"error": str(e)}
