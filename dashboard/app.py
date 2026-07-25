"""Streamlit dashboard: baseline vs. AI closed-loop run comparison.

Reads the exported run summaries (runs/baseline/summary.csv and
runs/ai_closed_loop/summary.csv, produced by the Phase 1/2 run scripts) and
shows, side by side:
  - Total facility kWh, % reduction vs. baseline
  - Peak demand (kW) for each run
  - PMV comfort-band violation count for each run (must not regress vs.
    baseline, or any regression must be explicitly called out)
  - Time series: zone temp / PMV / kWh over the simulated period, baseline vs
    AI overlaid

TODO(Phase 4): implement once both comparison runs exist.
"""

raise NotImplementedError("TODO(Phase 4): build once baseline + AI closed-loop run summaries exist")
