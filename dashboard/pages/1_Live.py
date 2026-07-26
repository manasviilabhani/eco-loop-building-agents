"""Live view: polls Supabase every few seconds and shows the most recent
simulation run's decisions as they happen, growing in real time.

Requires a local simulation to actually be running (agent/service.py with
SUPABASE_URL / SUPABASE_ANON_KEY set, driving an EnergyPlus run against the
chosen site's ai_closed_loop IDF) -- this page only displays what that local
machine is producing; it does not run anything itself. See
docs/ARCHITECTURE.md for why the simulation can't run on this hosting.

Site awareness: the simulation's site is encoded as a prefix on run_id
("hyderabad-20260726-...") rather than as its own column, so this works
against the already-deployed Supabase table with no schema migration. Rows
written before sites existed carry no prefix and are read back as the
default site -- see `location_of()`.

Credentials come from Streamlit secrets (st.secrets), configured in the
app's Settings -> Secrets on Streamlit Community Cloud -- never hardcoded.
"""

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from models import locations  # noqa: E402
import theme  # noqa: E402

st.set_page_config(page_title="Eco-Loop: Live", layout="wide", page_icon="🏢")
# st.html rather than st.markdown(unsafe_allow_html=True) -- see app.py.
st.html(
    """<style>
      .block-container { padding-top: 2.5rem; max-width: 1180px; }
      [data-testid="stMetricValue"] { font-size: 1.6rem; }
    </style>"""
)
st.title("Live simulation view")
st.caption(
    "Streams each hourly decision from a simulation running on a local machine. "
    "This page displays; it does not run anything itself."
)

def _secret(name: str) -> str:
    """st.secrets.get() still raises StreamlitSecretNotFoundError when there
    is no secrets.toml anywhere -- the default is only honoured once a
    secrets file exists. Without this guard a checkout with no secrets (any
    fresh clone, and any local `streamlit run`) shows a raw traceback
    instead of the configuration message below."""
    try:
        return st.secrets.get(name, "")
    except Exception:
        return ""


SUPABASE_URL = _secret("SUPABASE_URL")
SUPABASE_ANON_KEY = _secret("SUPABASE_ANON_KEY")

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    st.info(
        "**Live view isn't configured.** It streams a simulation running on a local "
        "machine, so it needs a Supabase project to relay through.\n\n"
        "Add `SUPABASE_URL` and `SUPABASE_ANON_KEY` under Settings → Secrets on "
        "Streamlit Community Cloud (or in `.streamlit/secrets.toml` when running locally), "
        "then start a run with `python scripts/run_comparison.py --location <site>`."
    )
    st.stop()

HEADERS = {"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"}


def location_of(run_id: str) -> str:
    """Which site a run_id belongs to. Anything without a known site prefix
    predates multi-site support and came from a Chicago run, so it maps to
    the default rather than being dropped from the view."""
    for key in locations.LOCATIONS:
        if run_id.startswith(f"{key}-"):
            return key
    return locations.DEFAULT_LOCATION


def fetch_latest_run_id(location_key: str) -> str | None:
    """Most recent AI run for this site.

    Filtered server-side by run_id prefix. An earlier version pulled a window
    of the most recent rows and filtered in Python, which quietly broke as
    soon as one busy site filled the window: a second site's runs fell off
    the end and its page reported "no live run yet" while its data sat in the
    table."""
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/live_decisions",
        headers=HEADERS,
        params={
            "select": "run_id",
            "kind": "eq.ai_closed_loop",
            "run_id": f"like.{location_key}-*",
            "order": "created_at.desc",
            "limit": 1,
        },
        timeout=10,
    )
    resp.raise_for_status()
    rows = resp.json()
    if rows:
        return rows[0]["run_id"]

    # Runs written before sites existed carry no prefix at all. Only the
    # default site should adopt them, and only if it has nothing of its own.
    if location_key != locations.DEFAULT_LOCATION:
        return None
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/live_decisions",
        headers=HEADERS,
        params={
            "select": "run_id",
            "kind": "eq.ai_closed_loop",
            "order": "created_at.desc",
            "limit": 500,
        },
        timeout=10,
    )
    resp.raise_for_status()
    for row in resp.json():
        if location_of(row["run_id"]) == location_key:
            return row["run_id"]
    return None


def fetch_run(run_id: str) -> pd.DataFrame:
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/live_decisions",
        headers=HEADERS,
        params={"run_id": f"eq.{run_id}", "order": "hour_index.asc"},
        timeout=10,
    )
    resp.raise_for_status()
    return pd.DataFrame(resp.json())


def fetch_baseline(location_key: str) -> pd.DataFrame:
    """Per-site baseline reference. Falls back to the unsuffixed run_id for
    the default site, which is what the original single-site push wrote."""
    candidates = [f"baseline-reference-{location_key}"]
    if location_key == locations.DEFAULT_LOCATION:
        candidates.append("baseline-reference")
    for run_id in candidates:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/live_decisions",
            headers=HEADERS,
            params={"run_id": f"eq.{run_id}", "order": "hour_index.asc"},
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
        if rows:
            return pd.DataFrame(rows)
    return pd.DataFrame()


site = st.selectbox(
    "Site",
    list(locations.LOCATIONS.values()),
    format_func=lambda l: l.label,
    help="Shows the most recent live run for this site. Runs are tagged by "
    "site automatically when started via scripts/run_comparison.py --location.",
)


@st.fragment(run_every="3s")
def live_section():
    run_id = fetch_latest_run_id(site.key)
    baseline_df = fetch_baseline(site.key)

    if run_id is None:
        st.info(
            f"No live run yet for **{site.label}**. Start one locally: set "
            "SUPABASE_URL / SUPABASE_ANON_KEY, run `python -m agent.service`, then "
            f"`python scripts/run_comparison.py --location {site.key}`. "
            "This page updates automatically every 3s."
        )
        return

    df = fetch_run(run_id)

    # Outdoor drybulb travels inside the zone_temps JSON under a reserved
    # "_outdoor_c" key (see agent/live_push.py) -- older rows predate it.
    def _outdoor(v):
        return v.get("_outdoor_c") if isinstance(v, dict) else None

    df["outdoor_c"] = df["zone_temps"].map(_outdoor) if "zone_temps" in df else None
    has_outdoor = "outdoor_c" in df and df["outdoor_c"].notna().any()

    st.caption(
        f"run_id: `{run_id}` — {len(df)} hour(s) simulated so far, latest: {df['sim_time'].iloc[-1]}"
    )
    if baseline_df.empty:
        st.caption(
            f"No baseline reference loaded yet for {site.label} -- run "
            f"`python scripts/push_baseline_live.py --location {site.key}` "
            "locally to show the no-AI comparison line."
        )

    latest_kw = df["facility_demand_w"].iloc[-1] / 1000
    col1, col2, col3, col4 = st.columns(4)
    if has_outdoor:
        col1.metric("Outdoor temperature", f"{df['outdoor_c'].iloc[-1]:.1f} °C")
    else:
        col1.metric("Outdoor temperature", "—")
    col2.metric("Facility demand (AI)", f"{latest_kw:.2f} kW")
    col3.metric("Cooling setpoint", f"{df['cooling_setpoint_c'].iloc[-1]:.1f} °C")
    col4.metric("Heating setpoint", f"{df['heating_setpoint_c'].iloc[-1]:.1f} °C")

    if has_outdoor:
        st.divider()
        st.subheader("Outdoor temperature through the day")
        figt = go.Figure()
        figt.add_trace(
            theme.line(None, df["hour_index"], df["outdoor_c"], "Outdoor drybulb", theme.WEATHER_TEMP)
        )
        theme.style(
            figt, xlabel="Hour of day", ylabel="Outdoor drybulb (°C)", height=300, legend=False
        )
        figt.update_xaxes(range=[0, 24], dtick=3)
        st.plotly_chart(figt, width="stretch")
        st.caption(
            f"Latest simulated hour: **{df['sim_time'].iloc[-1]}** at "
            f"**{df['outdoor_c'].iloc[-1]:.1f} °C**. The curve stops at the hour that has "
            "actually elapsed and extends by one point per real hour."
        )

    st.divider()
    st.subheader("Electricity demand")
    fig = go.Figure()
    if not baseline_df.empty:
        fig.add_trace(
            theme.line(None, baseline_df["hour_index"], baseline_df["facility_demand_w"] / 1000,
                       "Baseline", theme.BASELINE)
        )
    fig.add_trace(
        theme.line(None, df["hour_index"], df["facility_demand_w"] / 1000,
                   "AI closed-loop (live)", theme.AI)
    )
    theme.style(fig, xlabel="Hour of day", ylabel="Facility demand (kW)", height=340)
    fig.update_xaxes(range=[0, 24], dtick=3)
    st.plotly_chart(fig, width="stretch")

    st.subheader("Setpoints")
    fig2 = go.Figure()
    if not baseline_df.empty:
        fig2.add_trace(
            theme.line(None, baseline_df["hour_index"], baseline_df["cooling_setpoint_c"],
                       "Baseline schedule", theme.BASELINE, dash="dot")
        )
    fig2.add_trace(
        theme.line(None, df["hour_index"], df["cooling_setpoint_c"], "Cooling (AI)", theme.AI)
    )
    theme.style(fig2, xlabel="Hour of day", ylabel="Cooling setpoint (°C)", height=320)
    fig2.update_xaxes(range=[0, 24], dtick=3)
    st.plotly_chart(fig2, width="stretch")

    if df["reasoning"].iloc[-1]:
        st.info(f"**Latest reasoning** — {df['reasoning'].iloc[-1]}")


live_section()
