"""Pushes the baseline (no-AI) run's hourly data to Supabase as a static
reference series for the live dashboard view (dashboard/pages/1_Live.py).

The baseline has no plugin/agent attached -- it's a plain schedule-driven
EnergyPlus run that finishes in seconds, so there's nothing to observe "live"
about it the way there is for the AI closed-loop (which pauses for a real LLM
call each simulated hour). Instead, this reads the baseline's completed
output and bulk-inserts one row per hour, tagged kind="baseline", so the live
page can plot it as a complete comparison line sitting next to the AI run's
line as it grows in real time.

Requires SUPABASE_URL / SUPABASE_ANON_KEY (same as agent/live_push.py) and a
baseline run already completed for the chosen site (produced by
scripts/run_comparison.py --location <site>).

Each site gets its own reference series, tagged run_id
"baseline-reference-<site>", so the live view can pair the right baseline
with whichever site's AI run is streaming.

Usage: python scripts/push_baseline_live.py [--location chicago|hyderabad]
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from models import locations  # noqa: E402

ZONES = ["SPACE1-1", "SPACE2-1", "SPACE3-1", "SPACE4-1", "SPACE5-1"]

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")


def main():
    parser = argparse.ArgumentParser(description="Push a site's baseline reference series to Supabase.")
    locations.add_location_arg(parser)
    args = parser.parse_args()
    loc = locations.get(args.location)
    baseline_csv = loc.run_dir("baseline") / "eplusout.csv"
    run_id = f"baseline-reference-{loc.key}"

    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        print("Set SUPABASE_URL and SUPABASE_ANON_KEY first.")
        sys.exit(1)
    if not baseline_csv.exists():
        print(
            f"No baseline data at {baseline_csv} -- run "
            f"scripts/run_comparison.py --location {loc.key} first."
        )
        sys.exit(1)

    df = pd.read_csv(baseline_csv)
    df.columns = [c.strip() for c in df.columns]

    # One row per hour (every 4th 15-min timestep), matching the AI run's
    # hourly decision cadence so the two series line up on the same x-axis.
    hourly = df.iloc[3::4].reset_index(drop=True)

    demand_col = "Whole Building:Facility Total Electricity Demand Rate [W](TimeStep)"
    cool_col = "CLG-SETP-SCH:Schedule Value [](TimeStep)"
    heat_col = "HTG-SETP-SCH:Schedule Value [](TimeStep)"

    rows = []
    for i, r in hourly.iterrows():
        rows.append(
            {
                "run_id": run_id,
                "kind": "baseline",
                "hour_index": i + 1,
                "sim_time": "",
                "zone_temps": {z: r[f"{z}:Zone Mean Air Temperature [C](TimeStep)"] for z in ZONES},
                "pmv": {z: r[f"{z} PEOPLE 1:Zone Thermal Comfort Fanger Model PMV [](TimeStep)"] for z in ZONES},
                "facility_demand_w": r[demand_col],
                "cooling_setpoint_c": r[cool_col],
                "heating_setpoint_c": r[heat_col],
                "reasoning": "",
            }
        )

    # Clear any previous baseline-reference push before inserting fresh rows.
    del_req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/live_decisions?run_id=eq.{run_id}",
        method="DELETE",
        headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"},
    )
    urllib.request.urlopen(del_req, timeout=10)

    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/live_decisions",
        data=json.dumps(rows).encode("utf-8"),
        method="POST",
        headers={
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    urllib.request.urlopen(req, timeout=15)
    print(f"Pushed {len(rows)} baseline reference rows for {loc.label} (run_id={run_id})")


if __name__ == "__main__":
    main()
