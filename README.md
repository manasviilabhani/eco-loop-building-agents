# Eco-Loop Building Agents

Closed-loop building energy control: EnergyPlus simulates a building and
streams live telemetry; a local open-source LLM (Qwen2.5 7B via Ollama)
reasons over that telemetry through real MCP tool calls and writes control
actions (HVAC setpoints) back into the *running* simulation via the
EnergyPlus Python Plugin / Actuator API — true mid-simulation Forward
Injection, not an edit-and-rerun batch loop.

See `docs/ARCHITECTURE.md` for the full design writeup (process boundary,
tool-calling design, prompt/latency strategy, self-healing, honest
tradeoffs).

**Live dashboard:** https://eco-loop-building-agents-utkqsp2jfshzz9x9vxarer.streamlit.app/

## Results (full week, verified)

| | Baseline | AI closed-loop | Change |
|---|---|---|---|
| Total electricity | 920.2 kWh | 887.4 kWh | **-3.56%** |
| Peak demand | 18,333 W | 17,551 W | **-4.27%** |
| PMV comfort violations (occupied hrs) | 4.18% | 10.45% | +6.27 pts |

A real, modest energy/peak reduction at a real (not catastrophic) comfort
cost — see `docs/ARCHITECTURE.md`'s "Results" and "Debugging note" sections
for how the control policy got here (it failed in two different directions
before this) and all 3 self-healing scenarios verified working end-to-end.

### Hyderabad, 19–25 July 2026 (real observed monsoon week)

| | Baseline | AI closed-loop | Change |
|---|---|---|---|
| Total electricity | 1021.5 kWh | 995.4 kWh | **-2.56%** |
| Peak demand | 18,335 W | 18,569 W | **+1.28%** |
| PMV comfort violations (occupied hrs) | 5.64% | 6.55% | +0.91 pts |

The same agent, same policy, no code changes — and a visibly weaker result.
That is the honest and more interesting finding: **the energy saving carries
over to a new climate, but the peak-demand saving does not** (Chicago
-4.27% became Hyderabad +1.28%).

The weather explains it. Hyderabad's week is *milder* in dry-bulb terms than
Chicago's (22.5–30.6°C vs 11.7–32.8°C) yet uses **more** energy, because the
load is latent — 50–94% RH under 10/10 cloud cover, with no overnight
cool-down to coast on (22.5°C minimum vs Chicago's 11.7°C). The agent's only
lever is a dry-bulb setpoint, which has little authority over a
dehumidification load, so raising it sheds less demand than it does in a dry
climate while still costing comfort. Peak-shaving in particular depends on
the solar-driven afternoon peak the monsoon cloud cover flattens away.

## Sites

The building can be re-sited without touching any of the agent code — the
location is a parameter (`models/locations.py`), and the agent's control
policy is climate-agnostic (it reasons from live PMV and demand telemetry,
not from any assumption about the weather).

| Site | Weather | Period |
|---|---|---|
| `chicago` (default) | EnergyPlus's bundled TMY3 typical-year file | July 1–7 |
| `hyderabad` | **Real observed weather**, rebuilt into an `.epw` from the Open-Meteo ERA5 archive | July 19–25, 2026 |

EnergyPlus ships weather files for five US cities and nothing else, so
Hyderabad needs `models/fetch_weather.py`, which fetches the hour-by-hour
conditions the site actually had on those real dates and writes a valid
`.epw`. That also means the Hyderabad run is not a "typical July" — it is
that specific monsoon week, which is a genuinely different control problem
from Chicago's dry summer week: humid, heavily overcast, and latent-load
dominated rather than driven by solar gain.

The same script also derives ASHRAE-style design conditions from five years
of the site's own hourly history, because EnergyPlus autosizes HVAC
equipment from design days and does *not* read them from the weather file —
left alone, the Hyderabad building would have been sized for Chicago's
-17.3°C winter design day. Derived values for Hyderabad (1% cooling DB
37.7°C, MCWB 21.8°C, 99% heating DB 16.5°C, hottest month April) line up
closely with the published ASHRAE values for the station.

## Repo layout

- `models/` — baseline `.idf` + weather file, the AI closed-loop variant,
  broken variants for the self-healing demo, and the prep scripts that
  generate all of them from EnergyPlus's stock `5ZoneAirCooled.idf`
- `models/locations.py` — the site registry (lat/lon/timezone/elevation,
  run period, weather source) every other script resolves paths through
- `models/fetch_weather.py` — Open-Meteo ERA5 → `.epw` builder + design-day
  derivation, for sites EnergyPlus has no weather file for
- `plugin/eco_loop_plugin.py` — the EnergyPlus Python Plugin in-sim hook
  (stdlib-only; talks to the agent over HTTP, see architecture doc for why)
- `agent/` — MCP server (`mcp_server.py`, `tools.py`), the LLM decision loop
  (`agent_loop.py`), and the HTTP service (`service.py`) the plugin calls
- `scripts/` — `run_comparison.py` (baseline vs. AI full-week run + summary
  export), `self_heal_runner.py` (crash → diagnose → patch → retry loop)
- `dashboard/app.py` — Streamlit app comparing baseline vs. AI closed-loop
- `docs/ARCHITECTURE.md` — system architecture deliverable
- `runs/` — per-run EnergyPlus outputs and the comparison summary
  (gitignored bulk; `comparison_summary.json` is what the dashboard reads)

## Setup

```
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# EnergyPlus 26.1.0 (macOS arm64): install the .pkg from
# https://github.com/NatLabRockies/EnergyPlus/releases/tag/v26.1.0
# (default install path assumed by all scripts here: /Applications/EnergyPlus-26-1-0)

brew install ollama
ollama pull qwen2.5:7b-instruct
```

## Run

1. **Start the agent service** (must be running before any closed-loop or
   self-heal run):
   ```
   source .venv/bin/activate
   python -m agent.service   # listens on 127.0.0.1:8765
   ```

2. **Regenerate the models** (only needed after editing a `prepare_*.py` /
   `seed_broken_variants.py` script):
   ```
   python models/prepare_baseline.py
   python models/prepare_ai_closed_loop.py
   python models/seed_broken_variants.py
   ```

3. **Baseline vs. AI closed-loop comparison** (full week, produces
   `runs/comparison_summary.json` for the dashboard):
   ```
   python scripts/run_comparison.py
   ```

   For another site, pass `--location`. Hyderabad additionally needs its
   weather file built first (one-off; re-run to move to a different week):
   ```
   python models/fetch_weather.py          --location hyderabad
   python models/prepare_baseline.py       --location hyderabad
   python models/prepare_ai_closed_loop.py --location hyderabad
   python scripts/run_comparison.py        --location hyderabad
   ```

4. **Self-healing demo** (crash → diagnose → patch → rerun):
   ```
   python scripts/self_heal_runner.py models/broken_bad_people_count.idf
   python scripts/self_heal_runner.py models/broken_dangling_schedule.idf
   python scripts/self_heal_runner.py models/broken_invalid_comfort_model.idf
   ```

5. **Dashboard** (after step 3 has produced `comparison_summary.json`):
   ```
   streamlit run dashboard/app.py
   ```
   The site picker lists every location that has a comparison summary.

6. **Live view** (optional; needs `SUPABASE_URL` / `SUPABASE_ANON_KEY`).
   Push the no-AI reference line for a site, then start a run — the live
   page has its own site picker and shows the latest run for whichever site
   is selected:
   ```
   python scripts/push_baseline_live.py --location hyderabad
   python scripts/run_comparison.py    --location hyderabad
   ```
   Runs are tagged by site via an `ECO_LOOP_LOCATION` prefix on `run_id`,
   so no Supabase schema change was needed and rows written before
   multi-site support still read back correctly (as Chicago).

7. **Live "today" daemon** — continuously simulates the current day at a real
   site on freshly fetched weather, so the live view always has the agent
   working through today:
   ```
   source .env.local            # SUPABASE_URL / SUPABASE_ANON_KEY
   python -m agent.service &
   python scripts/live_daemon.py --site hyderabad
   ```
   Each cycle pulls today's weather from Open-Meteo's *forecast* endpoint
   (the hours already observed today, plus the forecast for the rest of it),
   rebuilds the model for today's date, runs the baseline, then runs the AI
   closed loop — streaming each hourly decision to the dashboard as it
   happens. Roughly 10 minutes per cycle (24 decisions), then it repeats with
   refreshed weather.

   What "live" honestly means here:

   - The **weather is real** — part observation, part forecast, refreshed by
     Open-Meteo about every 15 minutes. That, not the page's 3-second poll,
     is the rate at which genuinely new weather information exists.
   - The **building state legitimately changes much faster** than the
     weather: thermal mass, the occupancy schedule and HVAC cycling all
     evolve between weather points, and EnergyPlus interpolates hourly
     weather onto its 15-minute timestep natively (`Timestep, 4`).
   - **Simulated time is not wall-clock time.** A cycle replays a whole day
     in ~10 minutes, so the page shows "the agent working through today's
     weather", not "the building at this exact second".

## Notes

- Building: EnergyPlus's bundled `5ZoneAirCooled.idf` (5-zone packaged-AC
  office), trimmed to July 1-7 (Chicago, `USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw`)
  to keep iteration fast; see `docs/ARCHITECTURE.md` for why.
- The agent decides once per simulated hour, not every timestep — also
  covered in the architecture doc.
