"""Drives the LLM (Ollama, qwen2.5:7b-instruct) against the MCP tool server.

Called by plugin/eco_loop_plugin.py once per decision cycle (default: every
simulated hour). Responsibilities:

  - Build a compact JSON telemetry snapshot (NOT raw EnergyPlus logs) as the
    prompt payload -- see docs/ARCHITECTURE.md "Prompt & Latency Strategy"
    once written.
  - Short-circuit: if telemetry hasn't materially changed since the last
    cycle (e.g. zone temp/PMV/energy delta below a small threshold), skip the
    LLM call and hold the previous decision, to bound latency and call count.
  - Call the model with tool-calling enabled against the MCP tool set from
    mcp_server.py.
  - On timeout/error/malformed tool response: fall back to holding the last
    known-good setpoint (never let a bad LLM response propagate to the
    actuator or crash the sim) and log the failure for the architecture doc.
  - Narrow objective first (minimize energy s.t. PMV in [-0.5, 0.5]); peak
    demand + grid carbon-intensity awareness layered in only after that loop
    is verified working end-to-end (Phase 2 done -> optional stretch).

TODO(Phase 2): implement once mcp_server.py tools are live and Ollama
tool-calling has been sanity-checked (Phase 0).
"""

DECISION_CADENCE_MINUTES = 60
UNCHANGED_THRESHOLD = {
    "zone_temp_c": 0.2,
    "pmv": 0.05,
    "facility_kwh": 0.05,
}
LLM_TIMEOUT_SECONDS = 15

raise NotImplementedError("TODO(Phase 2): implement agent loop once MCP server + Ollama sanity checks pass")
