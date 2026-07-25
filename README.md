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

## Repo layout

- `models/` — baseline `.idf` + weather file, the AI closed-loop variant,
  broken variants for the self-healing demo, and the prep scripts that
  generate all of them from EnergyPlus's stock `5ZoneAirCooled.idf`
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

## Notes

- Building: EnergyPlus's bundled `5ZoneAirCooled.idf` (5-zone packaged-AC
  office), trimmed to July 1-7 (Chicago, `USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw`)
  to keep iteration fast; see `docs/ARCHITECTURE.md` for why.
- The agent decides once per simulated hour, not every timestep — also
  covered in the architecture doc.
