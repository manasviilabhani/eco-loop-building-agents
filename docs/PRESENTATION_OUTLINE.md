# Presentation content outline

Draft content to paste into the provided slide template (I don't have access
to the actual template file — you'll need to open it and map these onto its
slides). Results below are final, from a verified full-week run.

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

## 5. Results (full week, Chicago summer, verified in runs/comparison_summary.json)
- Baseline total: 920.2 kWh | AI closed-loop: 887.4 kWh | **-3.56% energy**
- Peak demand: baseline 18,333 W vs AI 17,551 W | **-4.27% peak**
- PMV comfort violations (occupied hours only): baseline 4.18% vs AI 10.45%
- Honest framing: real, modest energy + peak savings, at a real (not
  catastrophic) comfort cost -- be upfront about this rather than
  overclaiming. Worth narrating HOW you got here (see #4b below) since it's
  a much stronger story than a suspiciously perfect number would be.

## 4b. Live demo moment: iterating the control policy itself
Not just one bug -- the control *policy* itself failed twice before working:
1. First version systematically overcooled (avg PMV -0.2 to -0.37, colder
   than target) -- wasted energy AND hurt comfort at the same time, because
   the prompt didn't make clear that PMV is a two-sided scale (too cold is
   as much a violation as too hot).
2. Fixing that overcorrected: the agent then drifted to setpoints as high as
   27.5C picking absolute targets each cycle with no real feedback control,
   causing the opposite (hot) violations.
3. Fixed with two server-side (not just prompt-level) guardrails: a 0.5C/cycle
   rate limiter and a tightened occupied-hours safety envelope [22C, 26C].
This is a genuine, demonstrable self-correction narrative -- strong evidence
for "Agentic Autonomy" and honest engineering, not a hidden failure.

## 6. Self-healing demo
All 3 seeded broken-variant scenarios verified working end-to-end:
negative occupant count, dangling schedule reference, invalid comfort-model
enum value -- each: EnergyPlus fails with a clear error -> agent reads the
.err tail -> calls patch_idf_field (with list_schedule_names to look up real
values instead of inventing them) -> rerun succeeds, no human touches the
file. Also worth showing: fixing patch_idf_field's silent-no-op bug (wrong
field-name casing was accepted without error, so a "successful" patch
sometimes changed nothing) -- another concrete self-correction story.

## 7. Honest tradeoffs (see docs/ARCHITECTURE.md bottom section)
- Synthetic carbon-intensity curve, not a live grid API
- One-week comparison period, not a full year (time-boxed for the build
  window; mechanism generalizes without code changes)
- Building-wide setpoints, not per-zone (matches the original model's shared
  schedule design)

## 8. Deliverables checklist (for the closing slide)
- [x] GitHub repo, unified Python codebase — https://github.com/manasviilabhani/eco-loop-building-agents
- [x] Baseline + runtime-modified building models
- [x] Quantitative savings dashboard — live at https://eco-loop-building-agents-utkqsp2jfshzz9x9vxarer.streamlit.app/
- [x] System architecture document
- [ ] Demo video (needs to be screen-recorded by you)
- [ ] Submit via portal (GitHub URL above + PDF/zip upload per the brief's instructions)
