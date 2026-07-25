"""Long-running local HTTP service wrapping the LLM agent.

Why a separate process at all: EnergyPlus Python Plugins run inside
EnergyPlus's own embedded CPython 3.12 interpreter (see
docs/ARCHITECTURE.md), which has no access to this project's venv or its
`ollama`/`mcp` packages. So the agent (Ollama + the in-process MCP session)
runs here, in a normal venv Python process, and the EnergyPlus plugin talks
to it over a small stdlib-only HTTP bridge (urllib, no third-party deps
required on the plugin side).

Endpoints:
  POST /decide  -- body: telemetry snapshot JSON: see SimSnapshot fields.
                   response: {"ok": true, "cooling_setpoint_c", ...}
                             or {"ok": false, "error": "..."} on any failure
                             (LLM timeout, model never committed a decision,
                             etc.) -- the caller must hold its last known-good
                             setpoint when ok is false, so the simulation
                             never crashes because the agent was slow/wrong.

Run: python -m agent.service  (binds 127.0.0.1:8765)
"""

import asyncio
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent.agent_loop import decide_async, diagnose_and_patch_async
from agent.live_push import push_decision
from agent.shared_state import SimSnapshot, ZoneReading

HOST = "127.0.0.1"
PORT = 8765
LLM_TIMEOUT_SECONDS = 25
DIAGNOSE_TIMEOUT_SECONDS = 60

UNCHANGED_THRESHOLD = {
    "zone_temp_c": 0.2,
    "pmv": 0.05,
    "facility_demand_w_rel": 0.05,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [agent.service] %(message)s")
log = logging.getLogger("agent.service")

_last_snapshot: SimSnapshot | None = None
_last_decision: dict | None = None


def _parse_snapshot(body: dict) -> SimSnapshot:
    return SimSnapshot(
        sim_time=body["sim_time"],
        zones={z: ZoneReading(temp_c=v["temp_c"], pmv=v["pmv"]) for z, v in body["zones"].items()},
        facility_demand_w=body["facility_demand_w"],
        cooling_setpoint_c=body["cooling_setpoint_c"],
        heating_setpoint_c=body["heating_setpoint_c"],
        carbon_intensity=body.get("carbon_intensity", 0.5),
    )


def _telemetry_materially_unchanged(prev: SimSnapshot, cur: SimSnapshot) -> bool:
    for zone, cur_r in cur.zones.items():
        prev_r = prev.zones.get(zone)
        if prev_r is None:
            return False
        if abs(cur_r.temp_c - prev_r.temp_c) > UNCHANGED_THRESHOLD["zone_temp_c"]:
            return False
        if abs(cur_r.pmv - prev_r.pmv) > UNCHANGED_THRESHOLD["pmv"]:
            return False
    denom = max(abs(prev.facility_demand_w), 1.0)
    if abs(cur.facility_demand_w - prev.facility_demand_w) / denom > UNCHANGED_THRESHOLD["facility_demand_w_rel"]:
        return False
    return True


async def _handle_decide(body: dict) -> dict:
    global _last_snapshot, _last_decision

    snapshot = _parse_snapshot(body)

    if _last_snapshot is not None and _last_decision is not None and _last_decision.get("ok"):
        if _telemetry_materially_unchanged(_last_snapshot, snapshot):
            log.info("telemetry unchanged since last cycle, short-circuiting (no LLM call)")
            result = dict(_last_decision)
            result["short_circuited"] = True
            _last_snapshot = snapshot
            _push_live(body, result)
            return result

    try:
        result = await asyncio.wait_for(decide_async(snapshot), timeout=LLM_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        log.warning("LLM decision cycle timed out after %ss", LLM_TIMEOUT_SECONDS)
        result = {"ok": False, "error": "timeout"}
    except Exception as e:
        log.exception("agent decision cycle failed")
        result = {"ok": False, "error": str(e)}

    _last_snapshot = snapshot
    if result.get("ok"):
        _last_decision = result
    _push_live(body, result)
    return result


def _push_live(body: dict, result: dict) -> None:
    """Best-effort push to the live dashboard view -- only fires if the
    plugin included run_id/hour_index (added specifically for the live
    demo) and only if a decision was actually made (holding last-known-good
    on failure isn't a new data point worth showing live)."""
    if "run_id" not in body or "hour_index" not in body or not result.get("ok"):
        return
    push_decision(body["run_id"], body["hour_index"], body, result)


async def _handle_diagnose(body: dict) -> dict:
    try:
        return await asyncio.wait_for(
            diagnose_and_patch_async(body["idf_path"], body["err_log_tail"], body["out_path"]),
            timeout=DIAGNOSE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        log.exception("diagnose cycle failed")
        return {"ok": False, "error": str(e)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def do_POST(self):
        if self.path not in ("/decide", "/diagnose"):
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
            if self.path == "/decide":
                result = asyncio.run(_handle_decide(body))
            else:
                result = asyncio.run(_handle_diagnose(body))
            status = 200
        except Exception as e:
            log.exception("request handling failed")
            result = {"ok": False, "error": str(e)}
            status = 500

        payload = json.dumps(result).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("Eco-Loop agent service listening on http://%s:%s", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
