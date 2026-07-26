"""Streamlit dashboard: baseline vs. AI closed-loop run comparison.

Reads runs/comparison_summary{_location}.json (produced by
scripts/run_comparison.py) plus the two runs' raw eplusout.csv files for the
time-series view.

Multi-site: any location in models/locations.py that has a committed
comparison summary shows up in the site picker. Chicago runs on EnergyPlus's
bundled typical-meteorological-year file; Hyderabad runs on the real
observed weather of a specific week, rebuilt into an .epw by
models/fetch_weather.py.

Visual conventions live in dashboard/theme.py -- see that module for why the
palette is what it is and why outdoor temperature and humidity are two
charts rather than one with two y-axes.

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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from models import locations  # noqa: E402
import theme  # noqa: E402

ZONES = ["SPACE1-1", "SPACE2-1", "SPACE3-1", "SPACE4-1", "SPACE5-1"]

DEMAND_COL = "Whole Building:Facility Total Electricity Demand Rate [W](TimeStep)"
COOL_COL = "CLG-SETP-SCH:Schedule Value [](TimeStep)"
HEAT_COL = "HTG-SETP-SCH:Schedule Value [](TimeStep)"

st.set_page_config(page_title="Eco-Loop Building Agents", layout="wide", page_icon="🏢")

st.markdown(
    """
    <style>
      .block-container { padding-top: 2.5rem; max-width: 1180px; }
      [data-testid="stMetricValue"] { font-size: 1.75rem; }
      .hero { font-size: 3.4rem; line-height: 1; font-weight: 600; letter-spacing: -0.02em; }
      .hero-sub { color: #52514e; font-size: 0.95rem; margin-top: .35rem; }
      .rule { border-top: 1px solid #e8e6e1; margin: 1.6rem 0 1.2rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Eco-Loop Building Agents")
st.caption("A local LLM controlling HVAC setpoints inside a live EnergyPlus simulation, versus the building's own schedule.")

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
        help="The same 5-zone office building, run against each location's own weather.",
    )
else:
    loc = available[0]

summary = json.loads(loc.summary_path.read_text())
baseline = summary["baseline"]
ai = summary["ai_closed_loop"]

# "location" is absent in summaries produced before the multi-site change, so
# fall back to the registry rather than breaking on an older file.
meta = summary.get(
    "location",
    {"label": loc.label, "period": "7/1-7/7 (TMY)", "weather_source": loc.weather_source},
)
if meta["weather_source"] == "observed":
    st.caption(
        f"**{meta['label']}** · real observed weather for {meta['period']}, reconstructed "
        "from the Open-Meteo ERA5 archive — what the weather actually did on those dates, "
        "not a typical-year file."
    )
else:
    st.caption(f"**{meta['label']}** · EnergyPlus bundled TMY3 typical-meteorological-year file, {meta['period']}.")

st.markdown('<div class="rule"></div>', unsafe_allow_html=True)

# --- Headline. The story is one number, so it gets hero treatment rather
# --- than being one metric among six competing for attention.
kwh_delta = ai["total_kwh"] - baseline["total_kwh"]
peak_pct = 100 * (ai["peak_demand_w"] - baseline["peak_demand_w"]) / baseline["peak_demand_w"]
pmv_delta = ai["pmv_violation_pct"] - baseline["pmv_violation_pct"]

hero, tiles = st.columns([1, 2.1], gap="large")
with hero:
    sign = "−" if summary["kwh_reduction_pct"] > 0 else "+"
    color = theme.AI if summary["kwh_reduction_pct"] > 0 else theme.TEXT_PRIMARY
    st.markdown(
        f'<div class="hero" style="color:{color}">{sign}{abs(summary["kwh_reduction_pct"]):.2f}%</div>'
        f'<div class="hero-sub">electricity vs. baseline<br>'
        f'{baseline["total_kwh"]:.0f} → {ai["total_kwh"]:.0f} kWh ({kwh_delta:+.0f})</div>',
        unsafe_allow_html=True,
    )
with tiles:
    a, b, c = st.columns(3)
    a.metric("Peak demand", f"{ai['peak_demand_w']/1000:.1f} kW", delta=f"{peak_pct:+.2f}%", delta_color="inverse")
    a.caption(f"baseline {baseline['peak_demand_w']/1000:.1f} kW")
    b.metric("Comfort violations", f"{ai['pmv_violation_pct']:.2f}%", delta=f"{pmv_delta:+.2f} pts", delta_color="inverse")
    b.caption(f"baseline {baseline['pmv_violation_pct']:.2f}%")
    c.metric("Decision cadence", "hourly")
    c.caption(f"{ai['pmv_readings']:,} zone-timesteps scored")

st.caption(
    "Comfort violations = share of **occupied** zone-timesteps outside the Fanger PMV band "
    "[-0.5, 0.5]. Lower is better on both peak demand and comfort; a negative energy figure "
    "bought with a large comfort regression is not a win."
)

# --- Load time series ---
baseline_df = pd.read_csv(REPO / baseline["timeseries_csv"])
ai_df = pd.read_csv(REPO / ai["timeseries_csv"])
baseline_df.columns = [c.strip() for c in baseline_df.columns]
ai_df.columns = [c.strip() for c in ai_df.columns]

# Real dates on the x-axis beat an opaque timestep counter. TMY files have no
# meaningful year, so those fall back to the run year the registry declares.
year = loc.run_year or 2001
x = theme.to_datetime(baseline_df, year)
x_ai = theme.to_datetime(ai_df, year)


def ts_col(df, prefix):
    """Match the TimeStep-resolution series, not the Hourly one -- the stock
    IDF also requests outdoor drybulb hourly, and a plain prefix match would
    return a series a quarter the length of every other chart's."""
    return next((c for c in df.columns if c.startswith(prefix) and c.endswith("(TimeStep)")), None)


st.markdown('<div class="rule"></div>', unsafe_allow_html=True)
st.subheader("Electricity demand")

fig = go.Figure()
fig.add_trace(theme.line(None, x, baseline_df[DEMAND_COL] / 1000, "Baseline", theme.BASELINE))
fig.add_trace(theme.line(None, x_ai, ai_df[DEMAND_COL] / 1000, "AI closed-loop", theme.AI))
theme.style(fig, ylabel="Facility demand (kW)", height=360)
st.plotly_chart(fig, use_container_width=True)

# --- Outdoor conditions: two charts, never one with two y-axes. Temperature
# --- and humidity are different measures on different scales; overlaying them
# --- on twin axes manufactures a visual correlation the data doesn't assert.
temp_col = ts_col(baseline_df, "Environment:Site Outdoor Air Drybulb Temperature")
rh_col = ts_col(baseline_df, "Environment:Site Outdoor Air Relative Humidity")
if temp_col:
    st.subheader("Outdoor conditions driving the run")
    st.caption(
        "The weather the building is actually reacting to. In a monsoon climate the "
        "cooling load is latent — humidity matters as much as temperature, and a "
        "dry-bulb setpoint has little authority over it."
    )
    left, right = st.columns(2, gap="medium")
    with left:
        f = go.Figure()
        f.add_trace(theme.line(None, x, baseline_df[temp_col], "Outdoor drybulb", theme.WEATHER_TEMP))
        theme.style(f, ylabel="Drybulb temperature (°C)", height=260, legend=False)
        st.plotly_chart(f, use_container_width=True)
        st.caption(f"Range {baseline_df[temp_col].min():.1f} – {baseline_df[temp_col].max():.1f} °C")
    with right:
        if rh_col:
            f2 = go.Figure()
            f2.add_trace(theme.line(None, x, baseline_df[rh_col], "Relative humidity", theme.WEATHER_RH))
            f2.update_yaxes(range=[0, 100])
            theme.style(f2, ylabel="Relative humidity (%)", height=260, legend=False)
            st.plotly_chart(f2, use_container_width=True)
            st.caption(f"Range {baseline_df[rh_col].min():.0f} – {baseline_df[rh_col].max():.0f} %")

st.markdown('<div class="rule"></div>', unsafe_allow_html=True)
st.subheader("Thermal comfort (PMV)")
zone_choice = st.selectbox("Zone", ZONES, help="Fanger Predicted Mean Vote for the selected zone.")
pmv_col = f"{zone_choice} PEOPLE 1:Zone Thermal Comfort Fanger Model PMV [](TimeStep)"

fig2 = go.Figure()
fig2.add_hrect(y0=-0.5, y1=0.5, fillcolor=theme.BAND, opacity=0.10, line_width=0)
fig2.add_trace(theme.line(None, x, baseline_df[pmv_col], "Baseline", theme.BASELINE))
fig2.add_trace(theme.line(None, x_ai, ai_df[pmv_col], "AI closed-loop", theme.AI))
theme.style(fig2, ylabel="PMV", height=340)
fig2.add_annotation(
    x=1, y=0.5, xref="paper", yref="y", text="comfort band", showarrow=False,
    xanchor="right", yanchor="bottom", font=dict(size=11, color=theme.TEXT_MUTED),
)
st.plotly_chart(fig2, use_container_width=True)

st.subheader("HVAC setpoints the agent chose")
fig3 = go.Figure()
fig3.add_trace(theme.line(None, x_ai, ai_df[COOL_COL], "Cooling setpoint", theme.AI))
fig3.add_trace(theme.line(None, x, baseline_df[COOL_COL], "Baseline schedule", theme.BASELINE, dash="dot"))
theme.style(fig3, ylabel="Cooling setpoint (°C)", height=320)
theme.pad_y(fig3, pd.concat([ai_df[COOL_COL], baseline_df[COOL_COL]]))
st.plotly_chart(fig3, use_container_width=True)
st.caption(
    f"The agent moved the cooling setpoint across {ai_df[COOL_COL].nunique()} distinct values "
    f"({ai_df[COOL_COL].min():.1f}–{ai_df[COOL_COL].max():.1f} °C) against the baseline schedule's "
    f"{baseline_df[COOL_COL].nunique()}. Server-side clamps hold it inside [22, 26] °C and cap "
    "each change at 0.5 °C per decision cycle."
)

# --- Table view. Identity is never carried by colour alone, and every number
# --- plotted above is readable here.
st.markdown('<div class="rule"></div>', unsafe_allow_html=True)
with st.expander("Results as a table"):
    # Change is always AI minus baseline, so negative is the improvement in
    # every row -- kwh_reduction_pct is stored as a reduction and so is negated.
    st.dataframe(
        pd.DataFrame(
            [
                {"Metric": "Total electricity (kWh)", "Baseline": baseline["total_kwh"], "AI closed-loop": ai["total_kwh"], "Change": f"{-summary['kwh_reduction_pct']:+.2f}%"},
                {"Metric": "Peak demand (kW)", "Baseline": round(baseline["peak_demand_w"] / 1000, 2), "AI closed-loop": round(ai["peak_demand_w"] / 1000, 2), "Change": f"{peak_pct:+.2f}%"},
                {"Metric": "PMV violations (%)", "Baseline": baseline["pmv_violation_pct"], "AI closed-loop": ai["pmv_violation_pct"], "Change": f"{pmv_delta:+.2f} pts"},
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.caption("Change is AI minus baseline — negative is better in every row.")

if len(available) > 1:
    with st.expander("Compare all sites"):
        rows = []
        for other in available:
            s = json.loads(other.summary_path.read_text())
            m = s.get("location", {})
            (sm, sd), (em, ed) = other.run_period
            fallback_period = f"{sm}/{sd}-{em}/{ed}" + (f"/{other.run_year}" if other.run_year else " (TMY)")
            rows.append(
                {
                    "Site": m.get("label", other.label),
                    "Weather": m.get("weather_source", other.weather_source),
                    "Period": m.get("period", fallback_period),
                    "Baseline kWh": s["baseline"]["total_kwh"],
                    "AI kWh": s["ai_closed_loop"]["total_kwh"],
                    # Every delta column is signed the same way: change in the
                    # metric, so negative is always the improvement. The stored
                    # kwh_reduction_pct is a *reduction* (positive = less
                    # energy), so it is negated here -- reporting it raw next to
                    # a signed peak-demand change put two opposite sign
                    # conventions in adjacent columns of the same table.
                    "Energy Δ": f"{-s['kwh_reduction_pct']:+.2f}%",
                    "Peak Δ": f"{100 * (s['ai_closed_loop']['peak_demand_w'] - s['baseline']['peak_demand_w']) / s['baseline']['peak_demand_w']:+.2f}%",
                    "Comfort Δ": f"{s['ai_closed_loop']['pmv_violation_pct'] - s['baseline']['pmv_violation_pct']:+.2f} pts",
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption(
            "Δ columns are all AI minus baseline, so **negative is better in every one**. "
            "Absolute kWh is not comparable across sites — different weather means a different "
            "load to begin with; the Δ columns are the comparable ones."
        )
