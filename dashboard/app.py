"""Streamlit dashboard: baseline vs. AI closed-loop run comparison.

Reads runs/comparison_summary{_location}.json (produced by
scripts/run_comparison.py) plus the two runs' raw eplusout.csv files for the
time-series view.

Multi-site: any location in models/locations.py that has a committed
comparison summary shows up in the site picker. Chicago runs on EnergyPlus's
bundled typical-meteorological-year file; Hyderabad runs on the real
observed weather of a specific week, rebuilt into an .epw by
models/fetch_weather.py.

Run: streamlit run dashboard/app.py
"""

import json
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from models import locations  # noqa: E402

ZONES = ["SPACE1-1", "SPACE2-1", "SPACE3-1", "SPACE4-1", "SPACE5-1"]

st.set_page_config(page_title="Eco-Loop Building Agents", layout="wide")
st.title("Eco-Loop Building Agents: Baseline vs. AI Closed-Loop")

available = [loc for loc in locations.LOCATIONS.values() if loc.summary_path.exists()]
if not available:
    st.error(
        "No comparison results yet. Run `python scripts/run_comparison.py` first.\n\n"
        f"Expected one of: {', '.join(str(l.summary_path) for l in locations.LOCATIONS.values())}"
    )
    st.stop()

if len(available) > 1:
    loc = st.selectbox(
        "Site",
        available,
        format_func=lambda l: l.label,
        help="Each site is the same 5-zone office building run against that "
        "location's own weather, so the comparison shows how the agent's "
        "control strategy holds up in a different climate.",
    )
else:
    loc = available[0]

summary = json.loads(loc.summary_path.read_text())
baseline = summary["baseline"]
ai = summary["ai_closed_loop"]

# "location" is absent in summaries produced before the multi-site change
# (the committed Chicago result), so fall back to the registry rather than
# breaking on an older file.
meta = summary.get(
    "location",
    {"label": loc.label, "period": "7/1-7/7 (TMY)", "weather_source": loc.weather_source},
)
if meta["weather_source"] == "observed":
    st.caption(
        f"**{meta['label']}** — real observed weather for {meta['period']}, "
        "reconstructed from the Open-Meteo ERA5 archive (not a typical-year file). "
        "This is what the weather actually did at this site on these dates."
    )
else:
    st.caption(
        f"**{meta['label']}** — EnergyPlus's bundled TMY3 typical-meteorological-year "
        f"weather file, {meta['period']}."
    )

col1, col2, col3 = st.columns(3)
col1.metric("Energy reduction", f"{summary['kwh_reduction_pct']}%", delta=f"{ai['total_kwh'] - baseline['total_kwh']:.0f} kWh")
col2.metric("Baseline total kWh", f"{baseline['total_kwh']:.0f}")
col3.metric("AI closed-loop total kWh", f"{ai['total_kwh']:.0f}")

peak_pct = 100 * (baseline["peak_demand_w"] - ai["peak_demand_w"]) / baseline["peak_demand_w"]
col4, col5, col6 = st.columns(3)
col4.metric("Baseline peak demand", f"{baseline['peak_demand_w']/1000:.1f} kW")
col5.metric("AI peak demand", f"{ai['peak_demand_w']/1000:.1f} kW", delta=f"{-peak_pct:.2f}%", delta_color="inverse")
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

# Cross-site view: only meaningful once more than one site has been run.
if len(available) > 1:
    with st.expander("Compare all sites"):
        rows = []
        for other in available:
            s = json.loads(other.summary_path.read_text())
            m = s.get("location", {})
            rows.append(
                {
                    "Site": m.get("label", other.label),
                    "Weather": m.get("weather_source", other.weather_source),
                    "Period": m.get("period", "—"),
                    "Baseline kWh": s["baseline"]["total_kwh"],
                    "AI kWh": s["ai_closed_loop"]["total_kwh"],
                    "Energy reduction %": s["kwh_reduction_pct"],
                    "Baseline PMV viol. %": s["baseline"]["pmv_violation_pct"],
                    "AI PMV viol. %": s["ai_closed_loop"]["pmv_violation_pct"],
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption(
            "Absolute kWh is not comparable across sites — different weather means a "
            "different load to begin with. The reduction percentage is the comparable number."
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

# Outdoor conditions: the driver behind everything else, and the clearest
# way to see that Hyderabad's monsoon week is a genuinely different problem
# from Chicago's dry summer week.
def _timestep_col(df, prefix):
    """Match the TimeStep-resolution series, not the Hourly one. The stock
    IDF already requests outdoor drybulb hourly, so a plain prefix match
    returns a series a quarter the length of every other chart's, which
    plots against the 15-minute x-axis badly misaligned."""
    return next(
        (c for c in df.columns if c.startswith(prefix) and c.endswith("(TimeStep)")), None
    )


temp_col = _timestep_col(baseline_df, "Environment:Site Outdoor Air Drybulb Temperature")
rh_col = _timestep_col(baseline_df, "Environment:Site Outdoor Air Relative Humidity")
if temp_col:
    st.subheader("Outdoor conditions driving the run")
    figw = go.Figure()
    figw.add_trace(
        go.Scatter(y=baseline_df[temp_col], name="Drybulb temp (C)", line=dict(color="#ff7f0e"))
    )
    if rh_col:
        # Humidity on its own axis: in a monsoon climate the latent load is
        # what the cooling system is actually fighting, and it is invisible
        # if you only look at temperature.
        figw.add_trace(
            go.Scatter(
                y=baseline_df[rh_col],
                name="Relative humidity (%)",
                line=dict(color="#1f77b4", dash="dot"),
                yaxis="y2",
            )
        )
        figw.update_layout(
            yaxis2=dict(title="Relative humidity (%)", overlaying="y", side="right", range=[0, 100])
        )
    figw.update_layout(
        xaxis_title="Timestep (15 min)", yaxis_title="Outdoor air temp (C)", height=340
    )
    st.plotly_chart(figw, use_container_width=True)

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
