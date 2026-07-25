"""MCP tools exposed to the LLM agent. Registered on the FastMCP app in
mcp_server.py. Each tool reads/writes agent/shared_state.py, which the HTTP
handler (agent/service.py) populates from the latest EnergyPlus telemetry
POST before starting an agent decision turn.

set_hvac_setpoints is the only tool that produces a real control action; it
is clamped server-side here as a safety backstop independent of whatever the
LLM requests (the brief explicitly grades "self-correction" and robustness).
"""

from mcp.server.fastmcp import FastMCP

from agent.shared_state import state

SETPOINT_MIN_C = 20.0
SETPOINT_MAX_C = 28.0
MIN_DEADBAND_C = 2.0  # heating setpoint must stay at least this far below cooling setpoint
PMV_COMFORT_BAND = (-0.5, 0.5)

mcp_app = FastMCP("eco-loop-tools")


def clamp_setpoint(value_c: float) -> tuple[float, bool]:
    clamped = max(SETPOINT_MIN_C, min(SETPOINT_MAX_C, value_c))
    return clamped, clamped != value_c


def clamp_setpoint_pair(cooling_c: float, heating_c: float) -> tuple[float, float, bool]:
    """Clamps each to the safe range, then enforces a minimum heating/cooling
    deadband -- EnergyPlus fatally errors (DualSetPointWithDeadBand) if the
    effective heating setpoint is ever >= the cooling setpoint, and an LLM is
    not guaranteed to respect that physical constraint on its own."""
    cooling, cool_clamped = clamp_setpoint(cooling_c)
    heating, heat_clamped = clamp_setpoint(heating_c)
    if heating > cooling - MIN_DEADBAND_C:
        heating = cooling - MIN_DEADBAND_C
        heat_clamped = True
    return cooling, heating, (cool_clamped or heat_clamped)


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
def patch_idf_field(idf_path: str, object_type: str, object_name: str, field_name: str, new_value: str, out_path: str) -> dict:
    """Apply a targeted field edit to an IDF object (identified by object
    type + name) and save the result as a new IDF file at out_path. Used to
    autonomously fix a broken building model after a crashed run."""
    from eppy.modeleditor import IDF

    if IDF.getiddname() is None:
        IDF.setiddname("/Applications/EnergyPlus-26-1-0/Energy+.idd")

    try:
        idf = IDF(idf_path)
        matches = [
            o for o in idf.idfobjects[object_type.upper()]
            if getattr(o, "Name", None) == object_name
        ]
        if not matches:
            return {"error": f"no object of type {object_type} named {object_name} found"}
        setattr(matches[0], field_name, new_value)
        idf.saveas(out_path)
        state.pending_patch.out_path = out_path
        state.pending_patch.committed = True
        return {"status": "patched", "out_path": out_path}
    except Exception as e:
        return {"error": str(e)}
