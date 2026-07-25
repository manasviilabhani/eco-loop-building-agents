# Eco-Loop Building Agents

Closed-loop building energy control: EnergyPlus simulates a building and
streams live telemetry; a local open-source LLM (Qwen2.5 7B via Ollama)
reasons over that telemetry through MCP tool calls and writes control
actions (zone setpoints) back into the *running* simulation via the
EnergyPlus Python Plugin / Actuator API — true mid-simulation Forward
Injection, not an edit-and-rerun batch loop.

## Status

Scaffolding stage. See `docs/ARCHITECTURE.md` for the design doc (filled in
incrementally) and the repo's plan for the phased build order:

0. Environment setup — EnergyPlus, Ollama, Python 3.11+
1. Baseline simulation (unmodified schedule-driven run)
2. Closed loop — plugin + MCP server + agent, real actuator injection
3. Self-healing error recovery — agent diagnoses & patches broken IDFs
4. Savings dashboard — baseline vs. AI-driven % kWh reduction
5. Docs, demo video, submission

## Repo layout

- `models/` — baseline `.idf` + weather file, plus runtime-modified/broken
  variants used to demo self-healing
- `plugin/` — `eco_loop_plugin.py`, the EnergyPlus Python Plugin in-sim hook
- `agent/` — MCP server (`mcp_server.py`), tool implementations
  (`tools.py`), and the LLM decision loop (`agent_loop.py`)
- `runs/` — per-run EnergyPlus outputs and summaries (gitignored bulk)
- `dashboard/` — Streamlit app comparing baseline vs. AI closed-loop runs
- `docs/ARCHITECTURE.md` — system architecture deliverable

## Setup

```
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# EnergyPlus: install the macOS ARM64 .pkg from
# https://github.com/NREL/EnergyPlus/releases (see setup notes, Phase 0)

# Ollama:
brew install ollama
ollama pull qwen2.5:7b-instruct
```

## Run

TODO: filled in as each phase's run script lands.
