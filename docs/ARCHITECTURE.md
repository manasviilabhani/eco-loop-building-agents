# Eco-Loop Building Agents — System Architecture

> Status: scaffold. This doc gets filled in incrementally as each phase lands
> (see repo root plan), and is finalized in Phase 5 as the required
> deliverable #4.

## Sections to complete

1. **Overview diagram** — EnergyPlus <-> Plugin <-> MCP server <-> LLM, with
   the decision cadence and Forward Injection path labeled.
2. **Tool-calling architecture** — MCP tool schemas from `agent/tools.py`,
   why each exists, and the server-side clamping backstop.
3. **Prompt & latency strategy** — telemetry summarization format, the
   unchanged-telemetry short-circuit, decision cadence rationale (why hourly,
   not per-zone-timestep), and the LLM-timeout fallback (hold last setpoint).
4. **Self-healing error recovery** — how `.err` logs are parsed, how
   `patch_idf_field` is scoped (safe, targeted edits only), retry cap, and a
   worked example transcript from Phase 3 testing.
5. **Results** — baseline vs. AI closed-loop % kWh reduction, comfort-band
   adherence, with links to the dashboard.
6. **Honest tradeoffs** — what was cut for time, what would be hardened for
   a production system (e.g. multi-day/full-year runs, more zones, real grid
   carbon-intensity API instead of a synthetic curve).
