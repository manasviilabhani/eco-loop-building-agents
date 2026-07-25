"""EnergyPlus Python Plugin: the in-simulation hook for the Eco-Loop closed loop.

Runs inside the live EnergyPlus process. On a fixed decision cadence (default:
once per simulated hour, not every zone timestep) it:
  1. Reads sensor state via the Exchange API (zone mean air temp, PMV, facility
     electricity, outdoor conditions).
  2. Hands a compact JSON snapshot to the agent (agent/agent_loop.py).
  3. Writes the agent's returned setpoint back via the Actuator API
     (Forward Injection) -- server-side clamped to a safe range regardless of
     what the agent returns.
  4. If the agent call fails/times out, holds the last known-good setpoint so
     the simulation never stalls or crashes because the model was slow.

This file is intentionally thin: all reasoning lives in agent/agent_loop.py so
the plugin stays a dumb, reliable I/O boundary.
"""

from pyenergyplus.plugin import EnergyPlusPlugin

DECISION_CADENCE_MINUTES = 60  # call the agent once per simulated hour
SETPOINT_MIN_C = 18.0
SETPOINT_MAX_C = 28.0


class EcoLoopPlugin(EnergyPlusPlugin):
    def __init__(self):
        super().__init__()
        self.handles_initialized = False
        self.last_decision_minute = None
        self.last_good_setpoint = None
        # Sensor/actuator handles resolved lazily on first callback, once the
        # API guarantees the data exchange is ready.
        self.h_zone_temp = None
        self.h_pmv = None
        self.h_facility_electricity = None
        self.h_setpoint_actuator = None

    def _init_handles(self, state):
        raise NotImplementedError(
            "TODO(Phase 2): resolve handles via self.api.exchange.get_variable_handle / "
            "get_actuator_handle once the target IDF's exact object/zone names are known "
            "(depends on final baseline.idf chosen in Phase 1)."
        )

    def on_end_of_zone_timestep_after_zone_reporting(self, state) -> int:
        if not self.handles_initialized:
            self._init_handles(state)
            self.handles_initialized = True

        raise NotImplementedError(
            "TODO(Phase 2): read sensors, gate on DECISION_CADENCE_MINUTES, call the "
            "agent, clamp+apply the returned setpoint via set_actuator_value, and fall "
            "back to self.last_good_setpoint on any agent error/timeout."
        )
