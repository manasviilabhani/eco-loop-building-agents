# Eco-Loop Building Agents — System Architecture

## Overview

```
                  ┌──────────────────────────┐
                  │   EnergyPlus (native ARM) │
                  │   ai_closed_loop.idf       │
                  │   + EcoLoopController      │
                  │   Python Plugin            │
                  └─────────────┬─────────────┘
             sensor snapshot    │   ▲  actuator setpoint
             (zone temp, PMV,   │   │  (Forward Injection via
             demand, setpoints) │   │  Schedule:Compact actuator)
                                ▼   │
                  HTTP  POST /decide  (stdlib urllib, no deps)
                                │
                  ┌──────────────────────────┐
                  │  agent/service.py          │
                  │  (venv Python process)     │
                  │  - unchanged-telemetry     │
                  │    short-circuit           │
                  │  - LLM_TIMEOUT_SECONDS     │
                  │    wrapper + fallback      │
                  └─────────────┬─────────────┘
                                │
                  ┌──────────────────────────┐
                  │   MCP Server (in-process,  │
                  │   in-memory transport)     │
                  │   tools: get_zone_state,   │
                  │   get_energy_metrics,      │
                  │   get_comfort_index,       │
                  │   set_hvac_setpoints,      │
                  │   get_error_log,           │
                  │   patch_idf_field          │
                  └─────────────┬─────────────┘
                                │  tool calls
                                ▼
                  ┌──────────────────────────┐
                  │  Ollama (local LLM)        │
                  │  qwen2.5:7b-instruct       │
                  └──────────────────────────┘
```

Two full simulation runs are compared: **baseline** (`models/baseline.idf`,
unmodified schedule-driven setpoints) vs. **AI closed-loop**
(`models/ai_closed_loop.idf`, same building, agent-controlled setpoints).
Both run over the same trimmed period (July 1-7, Chicago) with the same
weather file, so the comparison isolates the effect of the agent's control
decisions.

## Process boundary: why there's an HTTP bridge at all

The original design assumed the EnergyPlus Python Plugin could directly
import and drive the MCP/Ollama client code. That's wrong: EnergyPlus bundles
its **own embedded CPython 3.12 interpreter** (`EnergyPlus-26-1-0/python_lib/`),
completely separate from this project's venv (Python 3.11). Verified directly
(see `.setup/sanity_run/import_probe.py`): `import ollama` and `import mcp`
both fail inside a running plugin with `ModuleNotFoundError`, and even a
`sys.path` hack wouldn't help because the interpreter's ABI (3.12 vs 3.11)
means compiled extensions (e.g. `pydantic-core`, a `mcp`/`ollama` transitive
dependency) aren't binary-compatible across the boundary.

So the system is two processes:
- **The EnergyPlus process**, running `plugin/eco_loop_plugin.py`, stdlib-only
  (`urllib`, `json`) — it has no access to any third-party package.
- **The agent service** (`agent/service.py`), an ordinary venv process running
  a long-lived local HTTP server (`http.server.ThreadingHTTPServer`, no
  framework dependency) that owns the MCP server, the Ollama client, and all
  agent reasoning.

This mirrors how a real deployment would look anyway — the BMS and the AI
service are naturally separate systems talking over a network — so the
process boundary ended up being a reasonable architectural choice, not just a
workaround.

## Tool-calling architecture

The MCP server (`agent/mcp_server.py`) is real: built with `FastMCP`
(`agent/tools.py`), exposing typed tools with JSON-schema parameters that the
LLM must call itself — there is no hardcoded control path standing in for the
model's reasoning. It runs **in-process** with the agent service, connected
to an `mcp.ClientSession` over an **in-memory transport**
(`mcp.shared.memory.create_connected_server_and_client_session`) rather than
a subprocess+stdio transport: same protocol, same client/server dispatch,
same tool schemas as a "real" MCP server, without the process-spawn latency
of starting a fresh subprocess on every hourly decision.

Tools:
- `get_zone_state`, `get_energy_metrics`, `get_comfort_index` — read-only,
  let the model drill into detail beyond the compact snapshot it's given.
- `set_hvac_setpoints(cooling_setpoint_c, heating_setpoint_c, reasoning)` —
  the only tool that produces a control action. **Clamped server-side**
  (`agent/tools.py::clamp_setpoint_pair`) to `[20°C, 28°C]` and to a minimum
  2°C heating/cooling deadband, independent of what the model requests.
- `get_error_log`, `patch_idf_field` — Phase 3 self-healing tools (see below).

**Defense in depth on the deadband constraint specifically:** an early test
run crashed EnergyPlus with `DualSetPointWithDeadBand: Effective heating
set-point higher than effective cooling set-point` — the LLM had picked
heating=22.75°C, cooling=22.00°C, which is physically invalid for a
dual-setpoint thermostat. The fix is enforced **twice**, independently: once
in the MCP tool (`agent/tools.py`) and again in the plugin itself
(`plugin/eco_loop_plugin.py::_clamp_pair`) right before actuation — the
plugin does not trust the agent service's HTTP response at face value, on the
theory that a component this close to a fatal-crash boundary should not
depend on a single validation layer.

## Prompt & latency strategy

- **Decision cadence: once per simulated hour**, not every zone timestep
  (which would be every 15 minutes here). Gated in the plugin by tracking
  `(day_of_month, hour)` and only calling the agent when it changes.
- **Sizing/design-day passes are excluded.** EnergyPlus runs internal
  zone/system sizing and load-component-report passes (on design days,
  outside the real weather-file `RunPeriod`) before the actual simulation;
  the plugin gates on `exchange.kind_of_sim(state) == 3` (RunPeriodWeather)
  so the agent never wastes decision cycles "optimizing" a sizing calculation
  that doesn't count toward the comparison.
- **Telemetry is summarized into a compact JSON snapshot** before it ever
  reaches the model (`agent_loop.py::_snapshot_summary`) — five zone
  temp/PMV pairs, one facility demand figure, current setpoints, and a
  synthetic carbon-intensity value. Raw EnergyPlus logs/CSVs are never piped
  to the LLM.
- **Unchanged-telemetry short-circuit** (`agent/service.py`): if every zone's
  temp/PMV and the facility demand are within a small threshold of the last
  cycle's values, the service replays the last decision without calling the
  model at all. This measurably cut LLM calls during steady overnight
  periods in testing.
- **Timeout + fallback:** each decision cycle is wrapped in
  `asyncio.wait_for(..., timeout=LLM_TIMEOUT_SECONDS)`. On timeout, tool
  malformation, or the model simply never calling `set_hvac_setpoints` within
  `MAX_TURNS`, the service returns `{"ok": false}` and **the plugin holds the
  last known-good setpoint** rather than actuating anything — the simulation
  never crashes or stalls because the agent was slow or wrong.

## Self-healing error recovery

`scripts/self_heal_runner.py` drives the loop: run EnergyPlus → on failure,
tail the `.err` file's severe/fatal lines → POST to the agent's `/diagnose`
endpoint → the LLM calls `patch_idf_field` itself (object type, object name,
field, corrected value) → the harness reruns the patched IDF, up to
`MAX_RETRIES` times.

Three broken variants (`models/seed_broken_variants.py`) demonstrate this
with realistic, distinct faults:
- `broken_bad_people_count.idf` — negative occupant count (IDD range
  violation).
- `broken_dangling_schedule.idf` — a People object references a schedule
  name that doesn't exist.
- `broken_invalid_comfort_model.idf` — an invalid enum value on the Fanger
  comfort model field.

Verified worked end-to-end on `broken_bad_people_count.idf`: EnergyPlus fails
with `Number_of_People = "-5" - Expected number greater than or equal to
0.000000`, the agent reads that, calls `patch_idf_field` to set it to a valid
value, and the patched IDF runs cleanly on the very next attempt (2 attempts
total, 5 tool-calling turns).

## Debugging note: occupancy-gated comfort scoring

An early full-week comparison run showed the AI closed-loop doing *worse*
than baseline on both energy (+4.1%) and comfort (31.8% vs 27.1% PMV
violations). Root cause: `models/baseline.idf`'s People objects originally
used a constant "always occupied" schedule. EnergyPlus computes PMV from the
clothing/activity schedules regardless of actual headcount, so PMV gets
reported 24/7 even in an empty building overnight -- and with a constant
occupancy schedule, the added internal heat gain kept zones artificially
warm overnight, masking how bad the unfiltered PMV metric actually was.
Switching to the source file's own realistic `OCCUPY-1` schedule (weekday
8am-7pm) initially made the *measured* violation rate look worse (50.3%),
because now overnight zones properly cooled per the setback schedule while
PMV was still being scored against a hypothetical occupant who isn't there.

The actual fix was scoring comfort only during occupied hours
(`scripts/run_comparison.py::summarize`, filtered on a new `OCCUPY-1`
`Output:Variable`) -- comfort is only meaningful when someone can feel it.
That took the baseline's occupied-hours violation rate to a realistic 2.5%,
which is the number the AI closed-loop is actually being compared against.
This fix changes how *both* runs are scored equally; it does not favor
either one.

## Honest tradeoffs

- The grid carbon-intensity signal is **synthetic** (`plugin/eco_loop_plugin.py::
  _synthetic_carbon_intensity`, a fixed time-of-day curve), not a real feed —
  a production system would pull from a real grid API (WattTime,
  electricityMap). It's surfaced to the model as a secondary tie-breaker, per
  the phased build plan (comfort and energy are primary).
- The comparison period is trimmed to one representative summer week, not a
  full year, to keep iteration fast within the build window. The mechanism
  (hourly decisions, actuator injection, self-healing) generalizes to a full
  annual run without code changes, just more wall-clock time.
- All 5 zones share one cooling and one heating setpoint schedule (the
  building's original design), so the agent's action space is one
  building-wide setpoint pair per cycle, not per-zone control. Per-zone
  actuation would need per-zone thermostat schedules added to the model.
- `qwen2.5:7b-instruct` occasionally needs 2+ turns to commit a decision
  (it sometimes calls a read tool before `set_hvac_setpoints`); this is
  handled by `MAX_TURNS` rather than assuming single-shot tool calls.
