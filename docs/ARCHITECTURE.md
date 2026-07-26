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
Both run over the same trimmed period with the same weather file, so the
comparison isolates the effect of the agent's control decisions.

## Siting: making the location a parameter

The building is run at two sites (`models/locations.py`): Chicago, on the
TMY3 file EnergyPlus bundles, and Hyderabad, on the weather that site
actually had during 19–25 July 2026.

Nothing in the agent changed to support this, which is the point worth
making: the control policy reasons from live PMV and demand telemetry it is
handed each cycle, not from any embedded assumption about the climate, so
re-siting the building is a data change rather than a code change. The
`[22C, 26C]` setpoint envelope and the 0.5C-per-cycle rate limit are
properties of the *building*, not of Chicago, and carry over unchanged.

Three things did have to be handled, and they are the parts that would
silently produce plausible-looking nonsense if skipped:

- **There is no Indian weather file.** EnergyPlus ships five US cities.
  `models/fetch_weather.py` pulls hourly ERA5 reanalysis from the Open-Meteo
  archive and writes a conforming `.epw`. Fields ERA5 does not carry are
  written as EPW missing-value sentinels rather than invented — notably
  horizontal infrared, which EnergyPlus then correctly derives from the
  opaque sky cover we *do* provide.

- **Design days are not read from the weather file.** EnergyPlus autosizes
  the HVAC equipment from `SizingPeriod:DesignDay` objects in the IDF. The
  stock file carries Chicago's (-17.3C winter, 31.5C summer). Left in place,
  the Hyderabad building would have been sized for a winter that never
  happens and a cooling coil with dry-climate latent capacity — the run
  would have completed successfully and reported meaningless numbers. They
  are instead derived from five years of the site's own hourly record
  (1% cooling DB 37.7C, MCWB 21.8C, 99% heating DB 16.5C), which lands close
  to the published ASHRAE values for the station.

- **The real calendar matters.** The occupancy schedule is weekday-only, so
  the run period pins the actual year and lets EnergyPlus derive true
  weekdays, rather than inheriting the stock file's hardcoded "start on
  Tuesday".

The chosen week is peak south-west monsoon: humid and heavily overcast
rather than hot. That makes it a harder and more interesting control problem
than Chicago's dry summer week — the cooling load is latent rather than
solar-driven, so the lever the agent has (a dry-bulb setpoint) has less
authority over the comfort metric it is being scored on.

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
  the only tool that produces a control action.
- `list_schedule_names`, `get_error_log`, `patch_idf_field` — Phase 3
  self-healing tools (see below).

**`set_hvac_setpoints` is clamped server-side three separate ways**
(`agent/tools.py::clamp_setpoint_pair`), independent of what the model
requests, each one added in response to a real failure observed during
testing rather than speculatively:

1. **Range clamp to `[22°C, 26°C]`.** Not just a physical sanity bound — a
   deliberately narrow occupied-hours safety envelope. An earlier version
   allowed the full `[20°C, 28°C]` physical range; even with a per-cycle rate
   limit (below), the agent could still drift monotonically to an extreme
   over many cycles and overshoot comfort the other way. A real facility
   engineer would similarly configure a tight envelope around the building's
   known-reasonable setpoint (23.9°C in the original schedule) rather than
   trusting any single automation layer with the full range.
2. **Minimum 2°C heating/cooling deadband.** An early test run crashed
   EnergyPlus with `DualSetPointWithDeadBand: Effective heating set-point
   higher than effective cooling set-point` — the LLM had picked
   heating=22.75°C, cooling=22.00°C, physically invalid for a dual-setpoint
   thermostat.
3. **Rate limit of 0.5°C change per decision cycle** (`agent/tools.py::
   _rate_limit`), relative to the setpoint EnergyPlus is currently reporting.
   A prompt-only version of "make small incremental adjustments" was not
   reliable enough by itself — the model still occasionally jumped several
   degrees in one cycle.

Each of these is enforced **twice**, independently: once in the MCP tool
(`agent/tools.py`) and again in the plugin itself
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
with realistic, distinct faults, **all three verified working end-to-end**:
- `broken_bad_people_count.idf` — negative occupant count (IDD range
  violation). Fixed on the first patch attempt.
- `broken_dangling_schedule.idf` — a People object references a schedule
  name that doesn't exist. Fixed on the first patch attempt once the model
  had a `list_schedule_names` tool to look up a real name instead of
  inventing one (see below).
- `broken_invalid_comfort_model.idf` — an invalid enum value on the Fanger
  comfort model field. Fixed on the first patch attempt once the prompt
  named the exact valid enum values (see below).

Two real robustness gaps surfaced and got fixed during testing, not just in
the abstract:

- **Case/format mismatches broke object and field lookups.** EnergyPlus's
  own `.err` messages upper-case object names (`SPACE2-1 PEOPLE` for an
  object actually named `SPACE2-1 People`), and the model would echo that
  back or use inconsistent field-name casing (`ActivityLevelScheduleName`
  vs. the real `Activity_Level_Schedule_Name`). Worse, calling `setattr` on
  an eppy object silently accepts *any* attribute name without validating it
  against real IDF fields — a wrong field name was a silent no-op, not an
  error, so the model got no signal its "fix" hadn't actually done anything,
  and the exact same crash repeated on the next attempt. Fixed with
  case/format-insensitive matching on both object and field names, and by
  making `patch_idf_field` explicitly validate `field_name` against the
  object's real fields and return the valid list on mismatch.
- **The model invented plausible-sounding names that didn't exist.** For the
  dangling-schedule fault, its first "fix" set the reference to a new
  hallucinated schedule name (`"Standard Schedule"`) that was just as
  broken. Fixed by adding a `list_schedule_names` tool and instructing the
  model to look up a real name rather than guess one.

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
This fix changes how *both* runs are scored equally; it does not favor
either one.

## Debugging note: the stock building model already had People objects

A second, more consequential bug: `models/prepare_baseline.py` was adding a
*second* `People` object to each zone (e.g. `"SPACE1-1 People"` alongside the
stock file's own `"SPACE1-1 People 1"`), because an earlier grep search for
existing `People,` objects in `5ZoneAirCooled.idf` used a whitespace pattern
that missed them (the stock file indents with two spaces; the search assumed
none). This silently double-counted internal heat gains in every run
(baseline and AI both), and separately made the self-healing demo flaky: with
two `"SPACE2-1 People*"` objects, `patch_idf_field`'s object-name lookup
sometimes matched the wrong one and appeared to "succeed" while never
touching the actually-broken object, so the identical crash repeated on the
next attempt.

Fixed by extending the zones' existing People objects with the Fanger
comfort-model fields instead of creating new ones. This changed the
baseline's own numbers (958.9 kWh / 2.55% -> 920.2 kWh / 4.18% PMV
violations at occupied hours) -- expected, since removing double-counted
internal gains means less cooling load and a more realistic (if now
slightly less "safe") comfort baseline. Caught by directly inspecting the
generated `.idf` for duplicate object names after the self-healing demo kept
failing identically across retries, rather than assuming the LLM was simply
bad at the task.

## Results (full week, baseline vs. AI closed-loop)

| Metric | Baseline | AI closed-loop | Change |
|---|---|---|---|
| Total facility electricity | 920.2 kWh | 887.4 kWh | **-3.56%** |
| Peak demand | 18,333 W | 17,551 W | **-4.27%** |
| PMV comfort violations (occupied hours) | 4.18% | 10.45% | +6.27 pts |

**Honest characterization:** the closed loop delivers a real, modest energy
and peak-demand reduction, at a real (not catastrophic) comfort cost. Getting
here took several iterations of genuine debugging, not just prompt tuning in
the abstract -- earlier versions of the control policy scored *worse* than
baseline on both energy and comfort simultaneously (see the two debugging
notes above and in "Prompt & latency strategy"): first from systematically
overcooling (the two-sided nature of PMV wasn't explicit enough in the
prompt), then from overcorrecting and drifting to the opposite extreme
(absolute-target setpoint picks with no true feedback control). The current
result is a genuine, working, bounded closed loop with a real net benefit --
not a perfectly-tuned controller. With more time, a next step would be
tightening the comfort/energy tradeoff further, e.g. a smaller rate-limit
step, a narrower safety envelope, or per-zone (not building-wide) setpoints
so a single mis-read zone can't push the whole building off comfort target.

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
