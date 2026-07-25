"""Tool implementations exposed to the LLM agent, wrapped by mcp_server.py.

Each tool has an explicit schema (inputs, outputs, units, valid ranges) per
the brief's requirement that the LLM call real tools rather than being fed
hardcoded control logic. Actuator writes are clamped server-side here as a
safety backstop, independent of whatever the model requests.

Tool inventory:
    get_zone_state(zone_id)      -> temp_c, pmv, rh_pct
    get_energy_metrics()         -> facility_kwh_last_interval, peak_kwh_today
    get_comfort_index(zone_id)   -> pmv (Fanger model), in_band: bool
    set_zone_setpoint(zone_id, setpoint_c) -> applied_c (post-clamp), clamped: bool
    apply_ecm(ecm_name, params)  -> status
    get_error_log(run_id)        -> last N lines of eplusout.err (Phase 3, self-healing)
    patch_idf_field(object_type, field_path, new_value) -> status (Phase 3, self-healing)
"""

from dataclasses import dataclass

SETPOINT_MIN_C = 18.0
SETPOINT_MAX_C = 28.0
PMV_COMFORT_BAND = (-0.5, 0.5)


@dataclass
class ToolResult:
    ok: bool
    data: dict
    error: str | None = None


def clamp_setpoint(setpoint_c: float) -> tuple[float, bool]:
    clamped_value = max(SETPOINT_MIN_C, min(SETPOINT_MAX_C, setpoint_c))
    was_clamped = clamped_value != setpoint_c
    return clamped_value, was_clamped


def get_zone_state(zone_id: str) -> ToolResult:
    raise NotImplementedError("TODO(Phase 2): read from the live EcoLoopPlugin sensor snapshot")


def get_energy_metrics() -> ToolResult:
    raise NotImplementedError("TODO(Phase 2): read facility electricity meter from plugin snapshot")


def get_comfort_index(zone_id: str) -> ToolResult:
    raise NotImplementedError("TODO(Phase 2): read PMV from plugin snapshot, report in/out of band")


def set_zone_setpoint(zone_id: str, setpoint_c: float) -> ToolResult:
    raise NotImplementedError("TODO(Phase 2): clamp via clamp_setpoint(), queue for plugin to apply")


def apply_ecm(ecm_name: str, params: dict) -> ToolResult:
    raise NotImplementedError("TODO(Phase 2, optional/stretch): named ECM presets on top of raw setpoints")


def get_error_log(run_id: str) -> ToolResult:
    raise NotImplementedError("TODO(Phase 3): tail eplusout.err for the given run")


def patch_idf_field(object_type: str, field_path: str, new_value: str) -> ToolResult:
    raise NotImplementedError("TODO(Phase 3): apply a targeted field edit via eppy, save as new .idf variant")
