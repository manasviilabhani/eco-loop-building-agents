"""Pushes each hourly decision to Supabase so the hosted dashboard's live
view can display the simulation progressing in real time, in addition to
its normal job of returning a decision to the plugin.

Credentials come from environment variables (SUPABASE_URL, SUPABASE_ANON_KEY)
-- never hardcoded -- so this is a no-op (silently disabled) if they aren't
set, which keeps the core closed loop working with zero Supabase dependency
for anyone who doesn't want the live-view feature.
"""

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("agent.live_push")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
ENABLED = bool(SUPABASE_URL and SUPABASE_ANON_KEY)

if not ENABLED:
    log.warning("SUPABASE_URL/SUPABASE_ANON_KEY not set -- live push to the hosted dashboard is disabled")


def push_decision(run_id: str, hour_index: int, snapshot: dict, decision: dict) -> None:
    """Fire-and-forget insert of one row into the live_decisions table.
    Never raises -- a live-view outage must never affect the actual closed
    loop's decision cycle."""
    if not ENABLED:
        return

    row = {
        "run_id": run_id,
        "kind": "ai_closed_loop",
        "hour_index": hour_index,
        "sim_time": snapshot["sim_time"],
        "zone_temps": {z: v["temp_c"] for z, v in snapshot["zones"].items()},
        "pmv": {z: v["pmv"] for z, v in snapshot["zones"].items()},
        "facility_demand_w": snapshot["facility_demand_w"],
        "cooling_setpoint_c": decision.get("cooling_setpoint_c", snapshot["cooling_setpoint_c"]),
        "heating_setpoint_c": decision.get("heating_setpoint_c", snapshot["heating_setpoint_c"]),
        "reasoning": decision.get("reasoning", ""),
    }

    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/live_decisions",
            data=json.dumps(row).encode("utf-8"),
            method="POST",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )
        urllib.request.urlopen(req, timeout=5)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("live_push failed (non-fatal): %s", e)
