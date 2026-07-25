"""Drives the LLM (Ollama, qwen2.5:7b-instruct) against the in-process MCP
tool session (agent/mcp_server.py) for one decision cycle.

Called by agent/service.py's HTTP handler once per decision request from the
EnergyPlus plugin (default: every simulated hour -- see
plugin/eco_loop_plugin.py). Responsibilities:

  - Narrow objective: minimize energy while keeping every zone's PMV in
    [-0.5, 0.5]; the system prompt also surfaces peak demand and a synthetic
    grid carbon-intensity signal so the model *can* reason about them, but
    comfort + energy is the primary objective per the phased build plan.
  - Give the model the MCP tools directly (get_zone_state, get_energy_metrics,
    get_comfort_index, set_hvac_setpoints) and let it decide what to call --
    no hardcoded control logic standing in for its reasoning.
  - Cap the conversation at MAX_TURNS. If the model never calls
    set_hvac_setpoints (malformed response, gets stuck chatting, etc.), that
    is treated as a failed decision cycle -- the caller (service.py) is
    responsible for holding the last known-good setpoint in that case.
  - The whole cycle is wrapped in an overall timeout by the caller; this
    module does not itself impose a wall-clock limit beyond MAX_TURNS.
"""

import json

import ollama

from agent.mcp_server import connected_session
from agent.shared_state import SimSnapshot, state

MODEL = "qwen2.5:7b-instruct"
MAX_TURNS = 4
MAX_DIAGNOSE_TURNS = 5

DIAGNOSE_SYSTEM_PROMPT = """You are diagnosing a failed EnergyPlus simulation run. You will be given the \
tail of its .err log, which names the object type, object name, and field that caused a fatal error. \
Call patch_idf_field with the exact object_type (as EnergyPlus IDD names it, e.g. "People", \
"Schedule:Compact"), the object_name, the field_name to fix (Python attribute style, e.g. \
"Number_of_People"), and a corrected new_value, saving to the given out_path. Make the smallest \
targeted fix that addresses the specific error -- do not rewrite unrelated parts of the model. \
You MUST call patch_idf_field to finish; do not just describe the fix."""

SYSTEM_PROMPT = """You are the control agent for a 5-zone commercial building's HVAC system, \
running inside a closed loop against a live EnergyPlus simulation.

Objective (in priority order):
1. Keep every zone's PMV (Predicted Mean Vote thermal comfort index) within [-0.5, 0.5].
2. Subject to (1), minimize facility electricity demand.
3. When comfort and energy are both already satisfied, prefer setpoints that reduce demand \
further during high grid carbon-intensity periods (carbon_intensity closer to 1.0) and during \
high electricity demand (to help peak shaving), as a secondary tie-breaker.

Call get_zone_state, get_energy_metrics, and/or get_comfort_index if you need more detail than \
the summary you were given. You MUST finish every decision cycle by calling set_hvac_setpoints \
with your chosen cooling and heating setpoints and a one-sentence reasoning. Do not just describe \
what you would do -- call the tool."""


def _snapshot_summary(snap: SimSnapshot) -> str:
    zones = {z: {"temp_c": round(r.temp_c, 2), "pmv": round(r.pmv, 2)} for z, r in snap.zones.items()}
    return json.dumps(
        {
            "sim_time": snap.sim_time,
            "zones": zones,
            "facility_demand_w": round(snap.facility_demand_w, 1),
            "current_cooling_setpoint_c": snap.cooling_setpoint_c,
            "current_heating_setpoint_c": snap.heating_setpoint_c,
            "carbon_intensity": round(snap.carbon_intensity, 2),
        }
    )


def _mcp_tool_to_ollama(tool) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }


def _tool_result_to_text(result) -> str:
    parts = []
    for block in result.content:
        if hasattr(block, "text"):
            parts.append(block.text)
        else:
            parts.append(str(block))
    return "\n".join(parts)


async def decide_async(snapshot: SimSnapshot) -> dict:
    """Runs one full agent decision cycle. Returns a dict describing the
    outcome: either a committed decision or a failure reason -- never
    raises for ordinary LLM/tool-calling failures, only for genuine bugs."""
    state.snapshot = snapshot
    state.reset_pending()

    async with connected_session() as session:
        tools_resp = await session.list_tools()
        ollama_tools = [_mcp_tool_to_ollama(t) for t in tools_resp.tools]

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _snapshot_summary(snapshot)},
        ]

        for turn in range(MAX_TURNS):
            response = ollama.chat(model=MODEL, messages=messages, tools=ollama_tools)
            msg = response["message"]
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg.get("tool_calls")})

            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                messages.append(
                    {
                        "role": "user",
                        "content": "You must call set_hvac_setpoints to commit a decision this cycle.",
                    }
                )
                continue

            for call in tool_calls:
                name = call["function"]["name"]
                args = call["function"]["arguments"]
                result = await session.call_tool(name, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": _tool_result_to_text(result),
                    }
                )

            if state.pending.committed:
                return {
                    "ok": True,
                    "cooling_setpoint_c": state.pending.cooling_setpoint_c,
                    "heating_setpoint_c": state.pending.heating_setpoint_c,
                    "reasoning": state.pending.reasoning,
                    "turns": turn + 1,
                }

        return {"ok": False, "error": f"model did not commit a decision within {MAX_TURNS} turns"}


async def diagnose_and_patch_async(idf_path: str, err_tail: str, out_path: str) -> dict:
    """Phase 3: self-healing error recovery. Gives the model the tail of a
    crashed run's .err log and lets it call patch_idf_field itself to
    produce a corrected IDF variant. Returns {"ok": True, "out_path": ...}
    or {"ok": False, "error": ...} -- never raises for ordinary
    diagnosis/tool-calling failures."""
    state.reset_pending_patch()

    async with connected_session() as session:
        tools_resp = await session.list_tools()
        ollama_tools = [_mcp_tool_to_ollama(t) for t in tools_resp.tools]

        messages = [
            {"role": "system", "content": DIAGNOSE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"idf_path": idf_path, "out_path": out_path, "err_log_tail": err_tail}
                ),
            },
        ]

        for turn in range(MAX_DIAGNOSE_TURNS):
            response = ollama.chat(model=MODEL, messages=messages, tools=ollama_tools)
            msg = response["message"]
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg.get("tool_calls")})

            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                messages.append({"role": "user", "content": "You must call patch_idf_field to apply your fix."})
                continue

            for call in tool_calls:
                name = call["function"]["name"]
                args = call["function"]["arguments"]
                result = await session.call_tool(name, args)
                messages.append({"role": "tool", "tool_name": name, "content": _tool_result_to_text(result)})

            if state.pending_patch.committed:
                return {"ok": True, "out_path": state.pending_patch.out_path, "turns": turn + 1}

        return {"ok": False, "error": f"model did not commit a patch within {MAX_DIAGNOSE_TURNS} turns"}
