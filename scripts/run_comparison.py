"""Phase 4: run baseline vs. AI closed-loop over the same period and export
a comparison summary (JSON + per-timestep CSVs) for the dashboard.

Requires the agent service (agent/service.py) to be running on :8765 before
invoking the AI closed-loop run.

Usage: python scripts/run_comparison.py [--location chicago|hyderabad]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from models import locations  # noqa: E402

EPLUS = "/Applications/EnergyPlus-26-1-0/energyplus"
PMV_LO, PMV_HI = -0.5, 0.5


def run(idf_path: Path, out_dir: Path, weather: Path, location_key: str):
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    # The plugin reads ECO_LOOP_LOCATION to tag its run_id, which is how the
    # hosted live view tells one site's run from another (see
    # plugin/eco_loop_plugin.py). It only affects live-view labelling --
    # nothing about the simulation itself depends on it.
    env = {**os.environ, "ECO_LOOP_LOCATION": location_key}
    subprocess.run(
        [EPLUS, "-w", str(weather), "-d", str(out_dir), "--readvars", str(idf_path)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def summarize(out_dir: Path) -> dict:
    df = pd.read_csv(out_dir / "eplusout.csv")
    df.columns = [c.strip() for c in df.columns]

    zones = ["SPACE1-1", "SPACE2-1", "SPACE3-1", "SPACE4-1", "SPACE5-1"]
    pmv_cols = [f"{z} PEOPLE 1:Zone Thermal Comfort Fanger Model PMV [](TimeStep)" for z in zones]
    demand_col = "Whole Building:Facility Total Electricity Demand Rate [W](TimeStep)"
    elec_col = "Electricity:Facility [J](TimeStep)"
    occupied_col = "OCCUPY-1:Schedule Value [](TimeStep)"

    total_kwh = df[elec_col].sum() / 3.6e6
    peak_demand_w = df[demand_col].max()

    # Comfort only matters when someone is actually there to feel it --
    # EnergyPlus reports a PMV value even at zero occupancy (driven by the
    # constant clothing/activity schedules), so unfiltered PMV counts would
    # score an empty building's temperature drift as a "comfort violation".
    occupied = df[occupied_col] > 0
    violations = 0
    total_readings = 0
    for c in pmv_cols:
        pmv = df.loc[occupied, c]
        violations += int(((pmv < PMV_LO) | (pmv > PMV_HI)).sum())
        total_readings += len(pmv)

    return {
        "total_kwh": round(total_kwh, 1),
        "peak_demand_w": round(peak_demand_w, 1),
        "pmv_violations": violations,
        "pmv_readings": total_readings,
        "pmv_violation_pct": round(100 * violations / total_readings, 2),
        # Relative to repo root, not absolute -- comparison_summary.json is
        # committed and read by the dashboard wherever it's deployed, not
        # just on the machine that produced it.
        "timeseries_csv": str((out_dir / "eplusout.csv").relative_to(REPO)),
    }


def main():
    parser = argparse.ArgumentParser(description="Baseline vs. AI closed-loop comparison run.")
    locations.add_location_arg(parser)
    args = parser.parse_args()
    loc = locations.get(args.location)

    for path, hint in [
        (loc.weather_file, f"python models/fetch_weather.py --location {loc.key}"),
        (loc.baseline_idf, f"python models/prepare_baseline.py --location {loc.key}"),
        (loc.ai_idf, f"python models/prepare_ai_closed_loop.py --location {loc.key}"),
    ]:
        if not path.exists():
            raise SystemExit(f"Missing {path}.\nRun: {hint}")

    baseline_out = loc.run_dir("baseline")
    ai_out = loc.run_dir("ai_closed_loop")
    (start_m, start_d), (end_m, end_d) = loc.run_period
    period = f"{start_m}/{start_d}-{end_m}/{end_d}" + (f"/{loc.run_year}" if loc.run_year else " (TMY)")
    print(f"=== Site: {loc.label} | period {period} | weather {loc.weather_file.name} ===")

    print("\n=== Running baseline (full week) ===")
    run(loc.baseline_idf, baseline_out, loc.weather_file, loc.key)
    baseline_summary = summarize(baseline_out)
    print(json.dumps(baseline_summary, indent=2))

    print("\n=== Running AI closed-loop (full week) ===")
    run(loc.ai_idf, ai_out, loc.weather_file, loc.key)
    ai_summary = summarize(ai_out)
    print(json.dumps(ai_summary, indent=2))

    pct_reduction = round(
        100 * (baseline_summary["total_kwh"] - ai_summary["total_kwh"]) / baseline_summary["total_kwh"], 2
    )

    comparison = {
        "location": {
            "key": loc.key,
            "label": loc.label,
            "period": period,
            "weather_source": loc.weather_source,
            "weather_file": loc.weather_file.name,
        },
        "baseline": baseline_summary,
        "ai_closed_loop": ai_summary,
        "kwh_reduction_pct": pct_reduction,
    }

    out_path = loc.summary_path
    out_path.write_text(json.dumps(comparison, indent=2))
    print(f"\n=== kWh reduction: {pct_reduction}% ===")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
