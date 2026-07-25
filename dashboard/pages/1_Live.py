"""Live view: polls Supabase every few seconds and shows the most recent
simulation run's decisions as they happen, growing in real time.

Requires a local simulation to actually be running (agent/service.py with
SUPABASE_URL / SUPABASE_ANON_KEY set, driving an EnergyPlus run against
models/ai_closed_loop.idf) -- this page only displays what that local
machine is producing; it does not run anything itself. See
docs/ARCHITECTURE.md for why the simulation can't run on this hosting.

Credentials come from Streamlit secrets (st.secrets), configured in the
app's Settings -> Secrets on Streamlit Community Cloud -- never hardcoded.
"""

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

st.set_page_config(page_title="Eco-Loop: Live", layout="wide")
st.title("Live simulation view")

SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY", "")

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    st.error(
        "Live view isn't configured yet. Add SUPABASE_URL and SUPABASE_ANON_KEY "
        "in this app's Settings -> Secrets on Streamlit Community Cloud."
    )
    st.stop()

HEADERS = {"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"}


def fetch_latest_run_id() -> str | None:
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/live_decisions",
        headers=HEADERS,
        params={"select": "run_id", "kind": "eq.ai_closed_loop", "order": "created_at.desc", "limit": 1},
        timeout=10,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0]["run_id"] if rows else None


def fetch_run(run_id: str) -> pd.DataFrame:
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/live_decisions",
        headers=HEADERS,
        params={"run_id": f"eq.{run_id}", "order": "hour_index.asc"},
        timeout=10,
    )
    resp.raise_for_status()
    return pd.DataFrame(resp.json())


def fetch_baseline() -> pd.DataFrame:
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/live_decisions",
        headers=HEADERS,
        params={"run_id": "eq.baseline-reference", "order": "hour_index.asc"},
        timeout=10,
    )
    resp.raise_for_status()
    return pd.DataFrame(resp.json())


@st.fragment(run_every="3s")
def live_section():
    run_id = fetch_latest_run_id()
    baseline_df = fetch_baseline()

    if run_id is None:
        st.info(
            "No live run yet. Start one locally: set SUPABASE_URL / SUPABASE_ANON_KEY, "
            "run `python -m agent.service`, then run EnergyPlus against "
            "models/ai_closed_loop.idf. This page updates automatically every 3s."
        )
        return

    df = fetch_run(run_id)
    st.caption(f"run_id: `{run_id}` -- {len(df)} decision(s) so far, latest: {df['sim_time'].iloc[-1]}")
    if baseline_df.empty:
        st.caption(
            "No baseline reference loaded yet -- run `python scripts/push_baseline_live.py` "
            "locally to show the no-AI comparison line."
        )

    latest_kw = df["facility_demand_w"].iloc[-1] / 1000
    col1, col2, col3 = st.columns(3)
    col1.metric("Latest facility demand (AI)", f"{latest_kw:.2f} kW")
    col2.metric("Latest cooling setpoint", f"{df['cooling_setpoint_c'].iloc[-1]:.1f} C")
    col3.metric("Latest heating setpoint", f"{df['heating_setpoint_c'].iloc[-1]:.1f} C")

    fig = go.Figure()
    if not baseline_df.empty:
        fig.add_trace(
            go.Scatter(
                x=baseline_df["hour_index"],
                y=baseline_df["facility_demand_w"] / 1000,
                name="Baseline (no AI)",
                line=dict(color="#888"),
            )
        )
    fig.add_trace(
        go.Scatter(
            x=df["hour_index"],
            y=df["facility_demand_w"] / 1000,
            name="AI closed-loop (live)",
            line=dict(color="#2ca02c"),
        )
    )
    fig.update_layout(xaxis_title="Decision cycle (hour)", yaxis_title="kW", height=350)
    st.plotly_chart(fig, use_container_width=True)

    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(x=df["hour_index"], y=df["cooling_setpoint_c"], name="Cooling setpoint (AI)"))
    fig2.add_trace(go.Scatter(x=df["hour_index"], y=df["heating_setpoint_c"], name="Heating setpoint (AI)"))
    if not baseline_df.empty:
        fig2.add_trace(
            go.Scatter(
                x=baseline_df["hour_index"],
                y=baseline_df["cooling_setpoint_c"],
                name="Cooling setpoint (baseline)",
                line=dict(color="#888", dash="dot"),
            )
        )
    fig2.update_layout(xaxis_title="Decision cycle (hour)", yaxis_title="Setpoint (C)", height=350)
    st.plotly_chart(fig2, use_container_width=True)

    if df["reasoning"].iloc[-1]:
        st.caption(f"Latest reasoning: {df['reasoning'].iloc[-1]}")


live_section()
