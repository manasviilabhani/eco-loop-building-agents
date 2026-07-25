"""Streamlit dashboard: baseline vs. AI closed-loop run comparison.

Reads runs/comparison_summary.json (produced by scripts/run_comparison.py)
plus the two runs' raw eplusout.csv files for the time-series view.

Run: streamlit run dashboard/app.py
"""

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = Path(__file__).parent.parent
SUMMARY_PATH = REPO / "runs" / "comparison_summary.json"
ZONES = ["SPACE1-1", "SPACE2-1", "SPACE3-1", "SPACE4-1", "SPACE5-1"]

st.set_page_config(page_title="Eco-Loop Building Agents", layout="wide")
st.title("Eco-Loop Building Agents: Baseline vs. AI Closed-Loop")

if not SUMMARY_PATH.exists():
    st.error(f"No comparison results yet. Run `python scripts/run_comparison.py` first.\nExpected: {SUMMARY_PATH}")
    st.stop()

summary = json.loads(SUMMARY_PATH.read_text())
baseline = summary["baseline"]
ai = summary["ai_closed_loop"]

col1, col2, col3 = st.columns(3)
col1.metric("Energy reduction", f"{summary['kwh_reduction_pct']}%", delta=f"{ai['total_kwh'] - baseline['total_kwh']:.0f} kWh")
col2.metric("Baseline total kWh", f"{baseline['total_kwh']:.0f}")
col3.metric("AI closed-loop total kWh", f"{ai['total_kwh']:.0f}")

col4, col5, col6 = st.columns(3)
col4.metric("Baseline peak demand", f"{baseline['peak_demand_w']/1000:.1f} kW")
col5.metric("AI peak demand", f"{ai['peak_demand_w']/1000:.1f} kW")
col6.metric(
    "PMV comfort violations",
    f"{ai['pmv_violation_pct']}%",
    delta=f"{ai['pmv_violation_pct'] - baseline['pmv_violation_pct']:.2f} pts vs baseline",
    delta_color="inverse",
)

st.caption(
    "PMV comfort violations = fraction of zone-timesteps outside the target [-0.5, 0.5] Fanger PMV band. "
    "Lower is better; the AI run should not regress meaningfully on comfort even as it cuts energy use."
)

st.divider()
st.subheader("Time series: energy demand")

baseline_df = pd.read_csv(REPO / baseline["timeseries_csv"])
ai_df = pd.read_csv(REPO / ai["timeseries_csv"])
baseline_df.columns = [c.strip() for c in baseline_df.columns]
ai_df.columns = [c.strip() for c in ai_df.columns]

demand_col = "Whole Building:Facility Total Electricity Demand Rate [W](TimeStep)"
fig = go.Figure()
fig.add_trace(go.Scatter(y=baseline_df[demand_col] / 1000, name="Baseline", line=dict(color="#888")))
fig.add_trace(go.Scatter(y=ai_df[demand_col] / 1000, name="AI Closed-Loop", line=dict(color="#2ca02c")))
fig.update_layout(xaxis_title="Timestep (15 min)", yaxis_title="Facility demand (kW)", height=400)
st.plotly_chart(fig, use_container_width=True)

st.subheader("Time series: zone comfort (PMV)")
zone_choice = st.selectbox("Zone", ZONES)
pmv_col = f"{zone_choice} PEOPLE 1:Zone Thermal Comfort Fanger Model PMV [](TimeStep)"
fig2 = go.Figure()
fig2.add_trace(go.Scatter(y=baseline_df[pmv_col], name="Baseline", line=dict(color="#888")))
fig2.add_trace(go.Scatter(y=ai_df[pmv_col], name="AI Closed-Loop", line=dict(color="#2ca02c")))
fig2.add_hrect(y0=-0.5, y1=0.5, fillcolor="green", opacity=0.08, line_width=0, annotation_text="comfort band")
fig2.update_layout(xaxis_title="Timestep (15 min)", yaxis_title="PMV", height=400)
st.plotly_chart(fig2, use_container_width=True)

st.subheader("Time series: HVAC setpoints (AI run)")
cool_col = "CLG-SETP-SCH:Schedule Value [](TimeStep)"
heat_col = "HTG-SETP-SCH:Schedule Value [](TimeStep)"
fig3 = go.Figure()
fig3.add_trace(go.Scatter(y=ai_df[cool_col], name="Cooling setpoint", line=dict(color="#1f77b4")))
fig3.add_trace(go.Scatter(y=ai_df[heat_col], name="Heating setpoint", line=dict(color="#d62728")))
fig3.update_layout(xaxis_title="Timestep (15 min)", yaxis_title="Setpoint (C)", height=400)
st.plotly_chart(fig3, use_container_width=True)
