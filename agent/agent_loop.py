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
MAX_DIAGNOSE_TURNS = 6

DIAGNOSE_SYSTEM_PROMPT = """You are diagnosing a failed EnergyPlus simulation run. You will be given the \
tail of its .err log, which names the object type, object name, and field that caused a fatal error.

If the error is that a referenced schedule (or other named object) was not found, call \
list_schedule_names first and reuse one of the REAL names it returns -- do not invent a \
plausible-sounding name, it will not exist in the file and the fix will fail again just as badly.

Call patch_idf_field with the exact object_type (as EnergyPlus IDD names it, e.g. "People", \
"Schedule:Compact"), the object_name, the field_name to fix, and a corrected new_value, saving to \
the given out_path. field_name must be the exact real IDF field name in Python-attribute style with \
underscores (e.g. "Activity_Level_Schedule_Name", "Number_of_People") -- if patch_idf_field returns \
an error listing valid fields, use the exact string from that list, do not guess a variation of it. \
Make the smallest targeted fix that addresses the specific error -- do not rewrite unrelated parts \
of the model. If the error says a value "Failed to match against any enum values" for a choice-type \
field, do not guess variations of the value that was already tried -- that exact wording means the \
field only accepts one of a small fixed set of valid keywords. For \
"Thermal Comfort Model N Type" fields specifically, the valid values are exactly: Fanger, Pierce, \
KSU, AdaptiveASH55, AdaptiveCEN15251, CoolingEffectASH55, AnkleDraftASH55 -- nothing else. \
You MUST call patch_idf_field to finish; do not just describe the fix."""

SYSTEM_PROMPT = """You are the control agent for a 5-zone commercial building's HVAC system, \
running inside a closed loop against a live EnergyPlus simulation.

PMV (Predicted Mean Vote) is a TWO-SIDED comfort scale: negative means too COLD, positive means \
too HOT, and 0 is neutral. A PMV of -0.8 is JUST AS MUCH a violation as +0.8 -- overcooling a zone \
is not "safe" or "extra comfortable", it wastes energy AND makes comfort worse at the same time. \
Do not default to aggressive cooling out of caution.

How setpoints affect PMV: raising the cooling setpoint (a higher number, e.g. 23.0 -> 24.0) means \
LESS cooling is applied, which raises zone temperature and moves PMV in the positive (warmer) \
direction. Lowering the cooling setpoint means MORE cooling, which lowers zone temperature and \
moves PMV in the negative (colder) direction. The heating setpoint works the same way in reverse.

You do NOT need to pick the perfect setpoint in one shot -- you get a new reading and another chance \
to adjust every simulated hour. Make SMALL, INCREMENTAL adjustments from the CURRENT setpoint \
(given to you each cycle) rather than jumping to a very different value. As a hard rule: change the \
cooling setpoint by at most 0.5C per decision cycle, in whichever direction is indicated below. \
Large jumps overshoot and cause the opposite comfort problem one cycle later -- small steps let you \
converge smoothly and correct course if you overshoot.

Objective (in priority order):
1. Keep every zone's PMV within [-0.5, 0.5]. Specifically, looking at the zone currently furthest \
from the band:
   - If its PMV is below -0.3 (too cold), nudge the cooling setpoint UP by 0.5C from its current \
value. This both saves energy (less cooling load) and moves comfort back toward the band.
   - If its PMV is above +0.3 (too hot), nudge the cooling setpoint DOWN by 0.5C from its current \
value to add more cooling.
   - If every zone's PMV is already within [-0.3, 0.3], leave the cooling setpoint unchanged (or \
nudge it up by at most 0.5C toward the least-cooling value that has kept comfort in band so far) -- \
do not keep pushing setpoints to their extremes once comfort is satisfied.
2. Subject to (1), minimize facility electricity demand -- do not run tighter/colder setpoints than \
comfort requires.
3. When comfort and energy are both already satisfied, prefer setpoints that reduce demand \
further during high grid carbon-intensity periods (carbon_intensity closer to 1.0) and during \
high electricity demand (to help peak shaving), as a secondary tie-breaker -- still limited to \
0.5C steps.

Note: all 5 zones share one building-wide cooling setpoint and one heating setpoint -- you cannot \
set them per zone. Base your adjustment on the zone(s) currently furthest from the comfort band.

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
                result_text = _tool_result_to_text(result)
                print(f"[diagnose] tool={name} args={args} -> {result_text}", flush=True)
                messages.append({"role": "tool", "tool_name": name, "content": result_text})

            if state.pending_patch.committed:
                return {"ok": True, "out_path": state.pending_patch.out_path, "turns": turn + 1}

        return {"ok": False, "error": f"model did not commit a patch within {MAX_DIAGNOSE_TURNS} turns"}
