# Presentation content outline

Draft content to paste into the provided slide template (I don't have access
to the actual template file — you'll need to open it and map these onto its
slides). Numbers below get filled in once `runs/comparison_summary.json`
exists (Phase 4).

## 1. Problem / Objective
Eco-Loop Building Agents: close the loop between EnergyPlus and a
self-hosted LLM agent so a building actively self-corrects in real time,
instead of running on fixed rule-based schedules.

## 2. Architecture (use the diagram in docs/ARCHITECTURE.md)
- EnergyPlus (native ARM, Python Plugin) <-> HTTP bridge <-> agent service
  (MCP server + Ollama/qwen2.5:7b) <-> real actuator injection mid-simulation
- Key point: this is a genuine embedded-interpreter process boundary (verified
  by direct testing), not an architectural choice made for its own sake — and
  it happens to mirror how a real BMS-to-cloud-agent deployment would look.

## 3. What makes this different from a typical submission
- True mid-simulation actuation (EMS/Actuator API), not edit-IDF-and-rerun
- Real MCP server (FastMCP + in-memory transport), genuine tool-calling loop
- Self-healing: agent diagnoses and patches its own broken building models
  from raw .err logs, no human intervention
- Defense-in-depth safety clamping (caught and fixed a real EnergyPlus fatal
  crash during development — see below, strong demo moment)

## 4. Live demo moment: the deadband bug
During testing, the agent picked a heating setpoint above the cooling
setpoint — physically invalid, and EnergyPlus fatally crashes on it
(`DualSetPointWithDeadBand`). Fixed with a 2°C minimum deadband enforced in
two independent layers (MCP tool + plugin actuation backstop). This is a
genuine "self-correction" story, not a hypothetical.

## 5. Results (fill in from runs/comparison_summary.json)
- Baseline total kWh: ___
- AI closed-loop total kWh: ___
- % reduction: ___
- Peak demand: baseline ___ kW vs AI ___ kW
- PMV comfort violations: baseline ___% vs AI ___% (must not regress badly)

## 6. Self-healing demo
Show: broken_bad_people_count.idf fails with a clear Number_of_People range
error -> agent reads the .err tail -> calls patch_idf_field -> rerun succeeds
on the very next attempt (verified: 2 attempts, 5 tool-calling turns).

## 7. Honest tradeoffs (see docs/ARCHITECTURE.md bottom section)
- Synthetic carbon-intensity curve, not a live grid API
- One-week comparison period, not a full year (time-boxed for the build
  window; mechanism generalizes without code changes)
- Building-wide setpoints, not per-zone (matches the original model's shared
  schedule design)

## 8. Deliverables checklist (for the closing slide)
- [x] GitHub repo, unified Python codebase
- [x] Baseline + runtime-modified building models
- [ ] Quantitative savings dashboard (Streamlit, screenshot once run finishes)
- [x] System architecture document
- [ ] Demo video (needs to be screen-recorded by you)
