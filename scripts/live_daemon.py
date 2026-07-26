"""Continuously simulate *today* at a real site, streaming each hourly agent
decision to the hosted dashboard's live view.

What this actually does, once per cycle:

  1. Pull today's weather for the site from Open-Meteo's forecast endpoint --
     the hours already observed today, plus the forecast for the rest of it --
     and write it as an .epw.
  2. Rebuild the baseline and AI-closed-loop IDFs for today's date.
  3. Run the baseline (no LLM, finishes in under a second) and push it as the
     reference line.
  4. Run the AI closed loop. The plugin calls the agent once per simulated
     hour, and each decision is pushed to Supabase as it happens, so the live
     page fills in across the day in real time.
  5. Sleep, then do it all again with freshly fetched weather.

Honest framing of what "live" means here, because it is easy to overclaim:

  - The *weather* is real. Hours that have already happened today are
    observations; the rest of the day is forecast. Open-Meteo refreshes it
    about every 15 minutes -- so that, not the page's 3-second poll, is the
    rate at which genuinely new weather information exists.

  - The *building* is simulated, and its state legitimately changes far
    faster than the weather does: thermal mass, the occupancy schedule and
    HVAC cycling all evolve between weather points. EnergyPlus interpolates
    the hourly weather onto its own sub-hourly timestep natively (Timestep,4
    in the IDF = 15 minutes), which is standard practice, not a trick.

  - Simulated time is NOT wall-clock time. One cycle replays a whole day in
    roughly ten minutes, so the dashboard shows "the agent working through
    today's weather", not "the building at this exact second".

Requires: SUPABASE_URL / SUPABASE_ANON_KEY exported, the agent service
running (python -m agent.service), and Ollama up.

Usage:
    python scripts/live_daemon.py --site hyderabad
    python scripts/live_daemon.py --site hyderabad,chicago    # alternate sites
    python scripts/live_daemon.py --site hyderabad --once
    python scripts/live_daemon.py --site hyderabad --interval 900
"""

import argparse
import os
import subprocess
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from models import fetch_weather, locations, prepare_ai_closed_loop, prepare_baseline  # noqa: E402

EPLUS = "/Applications/EnergyPlus-26-1-0/energyplus"
DEFAULT_INTERVAL_S = 600


def log(msg: str):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def run_eplus(idf: Path, out_dir: Path, weather: Path, location_key: str):
    import shutil

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    env = {**os.environ, "ECO_LOOP_LOCATION": location_key}
    proc = subprocess.run(
        [EPLUS, "-w", str(weather), "-d", str(out_dir), "--readvars", str(idf)],
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        err = out_dir / "eplusout.err"
        tail = err.read_text()[-1500:] if err.exists() else proc.stderr[-1500:]
        raise RuntimeError(f"EnergyPlus failed ({proc.returncode}):\n{tail}")


def cycle(site: str, push_baseline: bool) -> None:
    """One full pass: fetch today's weather, rebuild, run, stream."""
    loc = locations.live_variant(site)  # pinned to today, so this rolls over at midnight
    (m, d), _ = loc.run_period
    log(f"--- cycle for {loc.label}: {loc.run_year}-{m:02d}-{d:02d} ---")

    fetch_weather.build_epw(loc)
    prepare_baseline.build(loc)
    prepare_ai_closed_loop.build(loc)

    log("running baseline (no LLM)...")
    baseline_dir = loc.run_dir("baseline")
    run_eplus(loc.baseline_idf, baseline_dir, loc.weather_file, loc.key)

    if push_baseline:
        # The reference line only changes when the day's weather does, so it
        # is pushed once per cycle rather than once per decision.
        subprocess.run(
            [sys.executable, str(REPO / "scripts" / "push_baseline_live.py"), "--location", loc.key],
            check=False,
        )

    log("running AI closed loop -- decisions stream to the dashboard as they happen...")
    run_eplus(loc.ai_idf, loc.run_dir("ai_closed_loop"), loc.weather_file, loc.key)
    log("cycle complete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    # Sites are cycled one after another in a single process rather than run
    # as parallel daemons: they would otherwise contend for the one local
    # Ollama instance, and two simultaneous runs make each other slower
    # without either finishing sooner.
    parser.add_argument(
        "--site", default="hyderabad",
        help="base site key, or a comma-separated list to alternate between "
             "(e.g. hyderabad,chicago). Default: hyderabad",
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL_S,
        help=f"seconds to wait between cycles (default: {DEFAULT_INTERVAL_S})",
    )
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument(
        "--no-baseline-push", action="store_true",
        help="skip re-pushing the baseline reference line each cycle",
    )
    args = parser.parse_args()

    if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_ANON_KEY"):
        raise SystemExit(
            "SUPABASE_URL / SUPABASE_ANON_KEY are not set, so nothing would reach the "
            "dashboard.\nRun:  source .env.local"
        )

    sites = [s.strip() for s in args.site.split(",") if s.strip()]
    for s in sites:
        if s not in locations.LOCATIONS:
            raise SystemExit(f"Unknown site {s!r}. Available: {', '.join(sorted(locations.LOCATIONS))}")

    log(f"live daemon starting: sites={sites} interval={args.interval}s once={args.once}")
    while True:
        for site in sites:
            try:
                cycle(site, push_baseline=not args.no_baseline_push)
            except Exception:
                # A daemon that dies on one bad cycle is worse than useless --
                # a transient Open-Meteo timeout or a stopped agent service
                # should cost one cycle, not the whole stream.
                log(f"cycle FAILED for {site}:\n" + traceback.format_exc())
        if args.once:
            return
        log(f"sleeping {args.interval}s")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
