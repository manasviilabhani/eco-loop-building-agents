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
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from models import fetch_weather, locations, prepare_ai_closed_loop, prepare_baseline  # noqa: E402

EPLUS = "/Applications/EnergyPlus-26-1-0/energyplus"
DEFAULT_INTERVAL_S = 600


def log(msg: str):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def run_eplus(idf: Path, out_dir: Path, weather: Path, location_key: str, extra_env: dict | None = None):
    import shutil

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    env = {**os.environ, "ECO_LOOP_LOCATION": location_key, **(extra_env or {})}
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


def resolve_day(spec: str) -> date:
    """`today`, `tomorrow`, `+N`, or an ISO date.

    Worth having because the building's occupancy schedule is weekday-only:
    on a weekend the HVAC never leaves setback, so the agent has nothing to
    control and a live view of "today" is a flat line. Pointing the daemon at
    the next weekday shows the loop actually working, on real forecast
    weather rather than invented conditions."""
    spec = spec.strip().lower()
    if spec == "today":
        return date.today()
    if spec == "tomorrow":
        return date.today() + timedelta(days=1)
    if spec.startswith("+"):
        return date.today() + timedelta(days=int(spec[1:]))
    return date.fromisoformat(spec)


def clear_run(run_id: str) -> None:
    """Drop a run's rows so the next pass replaces them rather than appending
    a second copy. Realtime mode reuses one run_id per site per day."""
    import urllib.request

    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_ANON_KEY"]
    req = urllib.request.Request(
        f"{url}/rest/v1/live_decisions?run_id=eq.{run_id}",
        method="DELETE",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # non-fatal: a stale duplicate is better than a dead loop
        log(f"  (could not clear {run_id}: {e})")


def cycle(site: str, push_baseline: bool, day: date | None = None, realtime: bool = False) -> None:
    """One full pass: fetch the day's weather, rebuild, run, stream.

    In realtime mode the simulation still covers the whole day, but only the
    hours that have already elapsed in the real world are published, and the
    run keeps a stable per-day id so each pass replaces its own rows. The
    chart therefore shows exactly the hours that have happened and grows by
    one point per real hour -- rather than replaying a whole day every few
    minutes."""
    loc = locations.live_variant(site, day=day)
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

    extra_env = {}
    if realtime:
        now = datetime.now()
        # EnergyPlus hour_index counts 1..24, so the hour that has just
        # finished is the current clock hour (00:xx -> hour 1).
        max_hour = now.hour + 1
        run_id = f"{loc.key}-{loc.run_year}{loc.run_period[0][0]:02d}{loc.run_period[0][1]:02d}"
        extra_env = {"ECO_LOOP_RUN_ID": run_id, "ECO_LOOP_MAX_HOUR": str(max_hour)}
        log(f"realtime: publishing hours 1-{max_hour} (local time {now:%H:%M}), run_id={run_id}")
        clear_run(run_id)
    else:
        log("running AI closed loop -- decisions stream to the dashboard as they happen...")

    run_eplus(loc.ai_idf, loc.run_dir("ai_closed_loop"), loc.weather_file, loc.key, extra_env)
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
    parser.add_argument(
        "--day", default="today",
        help="which day to simulate: today | tomorrow | +N | YYYY-MM-DD. "
             "Weekends are unoccupied, so the HVAC never runs and the agent "
             "has nothing to do -- point this at a weekday for a live view "
             "that actually moves. Default: today",
    )
    parser.add_argument(
        "--realtime", action="store_true",
        help="track wall-clock time: publish only the hours of the day that "
             "have actually elapsed, adding one point per real hour, instead "
             "of replaying the whole day each cycle",
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

    day = resolve_day(args.day)
    if day.weekday() >= 5:
        log(f"NOTE: {day} is a {day.strftime('%A')} -- the building is unoccupied, so the "
            "HVAC stays in setback and the agent has nothing to control. Use --day tomorrow "
            "or --day <a weekday> for a live view with actual activity.")

    log(f"live daemon starting: sites={sites} day={day} ({day.strftime('%A')}) "
        f"interval={args.interval}s once={args.once}")
    while True:
        for site in sites:
            try:
                cycle(
                    site,
                    push_baseline=not args.no_baseline_push,
                    day=resolve_day(args.day),
                    realtime=args.realtime,
                )
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
