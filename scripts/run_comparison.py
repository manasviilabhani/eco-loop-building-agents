"""Phase 4: run baseline vs. AI closed-loop over the same period and export
a comparison summary (JSON + per-timestep CSVs) for the dashboard.

Requires the agent service (agent/service.py) to be running on :8765 before
invoking the AI closed-loop run.

Usage: python scripts/run_comparison.py
"""

import json
import shutil
import subprocess
from pathlib import Path

import pandas as pd

REPO = Path(__file__).parent.parent
EPLUS = "/Applications/EnergyPlus-26-1-0/energyplus"
WEATHER = "/Applications/EnergyPlus-26-1-0/WeatherData/USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
PMV_LO, PMV_HI = -0.5, 0.5


def run(idf_path: Path, out_dir: Path):
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    subprocess.run(
        [EPLUS, "-w", WEATHER, "-d", str(out_dir), "--readvars", str(idf_path)],
        check=True,
        capture_output=True,
        text=True,
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
        "timeseries_csv": str(out_dir / "eplusout.csv"),
    }


def main():
    baseline_out = REPO / "runs" / "baseline_full"
    ai_out = REPO / "runs" / "ai_closed_loop_full"

    print("=== Running baseline (full week) ===")
    run(REPO / "models" / "baseline.idf", baseline_out)
    baseline_summary = summarize(baseline_out)
    print(json.dumps(baseline_summary, indent=2))

    print("\n=== Running AI closed-loop (full week) ===")
    run(REPO / "models" / "ai_closed_loop.idf", ai_out)
    ai_summary = summarize(ai_out)
    print(json.dumps(ai_summary, indent=2))

    pct_reduction = round(
        100 * (baseline_summary["total_kwh"] - ai_summary["total_kwh"]) / baseline_summary["total_kwh"], 2
    )

    comparison = {
        "baseline": baseline_summary,
        "ai_closed_loop": ai_summary,
        "kwh_reduction_pct": pct_reduction,
    }

    out_path = REPO / "runs" / "comparison_summary.json"
    out_path.write_text(json.dumps(comparison, indent=2))
    print(f"\n=== kWh reduction: {pct_reduction}% ===")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
