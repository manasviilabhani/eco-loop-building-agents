"""Generate `dashboard/live.html` -- a standalone, dependency-free live page.

The page shows the AI closed-loop's effect on electricity demand across the
day, revealing the curve only as far as the site's current local time, over
today's real weather pulled live from Open-Meteo in the browser.

Why a generator rather than a hand-written page: the energy numbers are real
EnergyPlus output, so they have to come from `runs/`. Re-run this after any
new `scripts/run_comparison.py` and the page picks the new results up. The
output is a single self-contained file with no CDN, no build step and no
server -- open it directly, or host it anywhere static.

What is live and what is not (the page says this too, in its footer):
  - Weather IS live: fetched per-site from Open-Meteo's forecast endpoint on
    load and every 15 minutes after, which is roughly the rate at which new
    weather information actually exists.
  - The clock IS live, in the *site's* timezone, and drives how much of every
    curve is drawn.
  - The energy curves are measured EnergyPlus results from the weekday runs
    in `runs/`, averaged per hour of day -- replayed against the clock, not
    recomputed for today. Claiming otherwise would need EnergyPlus and the
    LLM running, which a static page cannot do.

Usage:
    python dashboard/build_live_page.py
"""

import csv
import json
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "live.html"

DEMAND_COL = "Whole Building:Facility Total Electricity Demand Rate [W](TimeStep)"
DRYBULB_COL = "Environment:Site Outdoor Air Drybulb Temperature [C](TimeStep)"

# (baseline run dir, AI run dir) per site, plus the metadata the browser needs
# to fetch that site's live weather and tell its local time.
SITES = [
    {
        "key": "hyderabad",
        "label": "Hyderabad, Telangana (India)",
        "short": "Hyderabad",
        "lat": 17.385,
        "lon": 78.4867,
        "tz": "Asia/Kolkata",
        "baseline": "runs/baseline_full_hyderabad",
        "ai": "runs/ai_closed_loop_full_hyderabad",
        "period": "19-25 July 2026 (real observed monsoon week)",
    },
    {
        "key": "chicago",
        "label": "Chicago, IL (USA)",
        "short": "Chicago",
        "lat": 41.78,
        "lon": -87.75,
        "tz": "America/Chicago",
        "baseline": "runs/baseline_full",
        "ai": "runs/ai_closed_loop_full",
        "period": "1-7 July (TMY3 typical year)",
    },
]


def read_run(path: Path):
    """-> ({(mm, dd): {hour: mean W}}, same shape for outdoor drybulb).

    EnergyPlus writes 15-minute timesteps stamped at the *end* of the
    interval, so '24:00:00' is the last timestep of hour 23 rather than the
    first of the following day -- without folding it back, every day grows a
    phantom 25th hour and hour 0 loses a quarter of its samples.
    """
    demand = defaultdict(lambda: defaultdict(list))
    drybulb = defaultdict(lambda: defaultdict(list))
    with open(path / "eplusout.csv") as f:
        for row in csv.DictReader(f):
            date_s, time_s = row["Date/Time"].strip().split()
            mm, dd = (int(x) for x in date_s.split("/"))
            hh = int(time_s.split(":")[0])
            hh = 23 if hh == 24 else hh
            demand[(mm, dd)][hh].append(float(row[DEMAND_COL]))
            # Only the baseline run reports drybulb at timestep resolution.
            if DRYBULB_COL in row:
                drybulb[(mm, dd)][hh].append(float(row[DRYBULB_COL]))
    return demand, drybulb


def occupied_days(demand):
    """The weekdays the building is actually in use.

    Weekend days sit in HVAC setback and peak around 1.4 kW against 15-18 kW
    on occupied days, so the two cluster far apart; splitting at the midpoint
    of the observed range separates them without a hard-coded threshold that
    would break on differently-sized equipment. Averaging setback days into
    the profile would drag the AI-vs-baseline difference toward zero and
    understate what the agent does.
    """
    peaks = {d: max(sum(v) / len(v) for v in day.values()) for d, day in demand.items()}
    cutoff = (min(peaks.values()) + max(peaks.values())) / 2
    return sorted(d for d, p in peaks.items() if p > cutoff)


def hourly_profile(series, days, digits=1):
    out = []
    for hh in range(24):
        vals = [sum(series[d][hh]) / len(series[d][hh]) for d in days if hh in series[d]]
        out.append(round(sum(vals) / len(vals), digits) if vals else None)
    return out


def build_payload():
    sites = []
    for site in SITES:
        base_demand, base_temp = read_run(REPO / site["baseline"])
        ai_demand, _ = read_run(REPO / site["ai"])
        days = occupied_days(base_demand)
        sites.append(
            {
                **{k: site[k] for k in ("key", "label", "short", "lat", "lon", "tz", "period")},
                "baselineKw": [round(v / 1000, 3) for v in hourly_profile(base_demand, days)],
                "aiKw": [round(v / 1000, 3) for v in hourly_profile(ai_demand, days)],
                "simDrybulbC": hourly_profile(base_temp, days),
                "daysAveraged": len(days),
                "dates": [f"{m:02d}/{d:02d}" for m, d in days],
            }
        )
    return sites


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Eco-Loop — Live</title>
<style>
  /* Palette: the project's validated categorical pair (dashboard/theme.py).
     Light #2a78d6/#eb6834 clears every gate (worst-pair dE 24.7 protan, 33.6
     normal, both >=3:1 on the surface). Dark mode is re-stepped against the
     dark surface and re-validated -- #4a90e2/#dd6b2e, dE 24.8 protan, 29.3
     normal -- not an automatic flip of the light values. Colour follows the
     entity: baseline is always blue, AI always orange, on every chart. */
  :root {
    --surface: #fcfcfb;
    --panel: #ffffff;
    --text: #0b0b0b;
    --text-2: #52514e;
    --muted: #8a8880;
    --grid: #e8e6e1;
    --hairline: #e2e0da;
    --baseline: #2a78d6;
    --ai: #eb6834;
    --weather: #256abf;
    --good: #167a56;
    --shadow: 0 1px 2px rgba(11,11,11,.05), 0 8px 24px rgba(11,11,11,.05);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --surface: #16151a; --panel: #1e1d23; --text: #f4f3f0; --text-2: #b9b7b1;
      --muted: #8a8880; --grid: #302e37; --hairline: #35333d;
      --baseline: #4a90e2; --ai: #dd6b2e; --weather: #6aa6ee; --good: #35b98a;
      --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.3);
    }
  }
  :root[data-theme="light"] {
    --surface: #fcfcfb; --panel: #ffffff; --text: #0b0b0b; --text-2: #52514e;
    --muted: #8a8880; --grid: #e8e6e1; --hairline: #e2e0da;
    --baseline: #2a78d6; --ai: #eb6834; --weather: #256abf; --good: #167a56;
    --shadow: 0 1px 2px rgba(11,11,11,.05), 0 8px 24px rgba(11,11,11,.05);
  }
  :root[data-theme="dark"] {
    --surface: #16151a; --panel: #1e1d23; --text: #f4f3f0; --text-2: #b9b7b1;
    --muted: #8a8880; --grid: #302e37; --hairline: #35333d;
    --baseline: #4a90e2; --ai: #dd6b2e; --weather: #6aa6ee; --good: #35b98a;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.3);
  }

  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--surface); color: var(--text);
    font: 15px/1.55 Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1080px; margin: 0 auto; padding: 34px 22px 64px; }

  header { display: flex; flex-wrap: wrap; gap: 18px; align-items: flex-start; justify-content: space-between; }
  h1 { font-size: 26px; letter-spacing: -.02em; margin: 0 0 4px; font-weight: 650; }
  .sub { color: var(--text-2); font-size: 14px; margin: 0; max-width: 62ch; }

  .clock { text-align: right; flex-shrink: 0; }
  .clock .t { font-size: 30px; font-weight: 620; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
  .clock .z { font-size: 12px; color: var(--muted); }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--good);
         margin-right: 7px; vertical-align: middle; animation: pulse 2s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
  @media (prefers-reduced-motion: reduce) { .dot { animation: none; } }

  .controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 22px 0 6px; }
  select, button {
    font: inherit; font-size: 14px; color: var(--text); background: var(--panel);
    border: 1px solid var(--hairline); border-radius: 8px; padding: 7px 11px; cursor: pointer;
  }
  select:focus-visible, button:focus-visible { outline: 2px solid var(--baseline); outline-offset: 2px; }

  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(178px, 1fr)); gap: 12px; margin: 20px 0 8px; }
  .tile { background: var(--panel); border: 1px solid var(--hairline); border-radius: 12px; padding: 14px 16px; box-shadow: var(--shadow); }
  .tile .k { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
  .tile .v { font-size: 25px; font-weight: 620; letter-spacing: -.02em; margin-top: 3px; font-variant-numeric: tabular-nums; }
  .tile .n { font-size: 12px; color: var(--text-2); margin-top: 2px; }

  .card { background: var(--panel); border: 1px solid var(--hairline); border-radius: 12px;
          padding: 18px 18px 12px; margin-top: 18px; box-shadow: var(--shadow); }
  .card h2 { font-size: 15px; font-weight: 620; margin: 0 0 2px; letter-spacing: -.01em; }
  .card .cap { font-size: 13px; color: var(--text-2); margin: 0 0 12px; max-width: 74ch; }

  .legend { display: flex; gap: 16px; flex-wrap: wrap; margin: 0 0 8px; font-size: 13px; color: var(--text-2); }
  .legend span { display: inline-flex; align-items: center; gap: 7px; }
  .swatch { width: 14px; height: 3px; border-radius: 2px; display: inline-block; }

  .plot { position: relative; width: 100%; }
  canvas { display: block; width: 100%; touch-action: none; }
  .tip {
    position: absolute; pointer-events: none; opacity: 0; transition: opacity .1s;
    background: var(--panel); border: 1px solid var(--hairline); border-radius: 8px;
    padding: 8px 10px; font-size: 12.5px; box-shadow: var(--shadow); white-space: nowrap; z-index: 5;
    color: var(--text);
  }
  .tip .th { color: var(--muted); font-size: 11.5px; margin-bottom: 3px; }
  .tip .tr { display: flex; align-items: center; gap: 7px; font-variant-numeric: tabular-nums; }

  details { margin-top: 14px; }
  summary { cursor: pointer; font-size: 13px; color: var(--text-2); }
  table { border-collapse: collapse; margin-top: 10px; font-size: 13px; width: 100%; }
  th, td { text-align: right; padding: 5px 9px; border-bottom: 1px solid var(--grid); font-variant-numeric: tabular-nums; }
  th:first-child, td:first-child { text-align: left; }
  th { color: var(--muted); font-weight: 550; }
  .scroll { overflow-x: auto; }

  footer { margin-top: 26px; font-size: 13px; color: var(--text-2); line-height: 1.6; }
  footer b { color: var(--text); font-weight: 600; }
  .err { color: var(--ai); }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <h1>Eco-Loop — live</h1>
      <p class="sub">What the AI closed-loop controller does to building electricity demand across the day. The curve is drawn only as far as the clock has actually reached at the site.</p>
    </div>
    <div class="clock">
      <div class="t"><span class="dot"></span><span id="clock">--:--:--</span></div>
      <div class="z" id="clockzone">&nbsp;</div>
    </div>
  </header>

  <div class="controls">
    <label for="site" style="font-size:14px;color:var(--text-2)">Site</label>
    <select id="site"></select>
    <button id="theme" type="button" title="Switch light / dark">Theme</button>
  </div>

  <div class="tiles">
    <div class="tile"><div class="k">Difference now</div><div class="v" id="t-delta">—</div><div class="n" id="t-delta-n">AI vs baseline</div></div>
    <div class="tile"><div class="k">Saved so far today</div><div class="v" id="t-kwh">—</div><div class="n" id="t-kwh-n">midnight → now</div></div>
    <div class="tile"><div class="k">Outdoor now</div><div class="v" id="t-temp">—</div><div class="n" id="t-temp-n">live forecast</div></div>
    <div class="tile"><div class="k">Demand now</div><div class="v" id="t-dem">—</div><div class="n" id="t-dem-n">AI closed-loop</div></div>
  </div>

  <div class="card">
    <h2>Difference: AI closed-loop minus baseline</h2>
    <p class="cap">Below zero means the agent is drawing less power than the unmodified building would at this hour. The highlighted dot is the current moment.</p>
    <div class="plot"><canvas id="c-delta"></canvas><div class="tip" id="tip-delta"></div></div>
  </div>

  <div class="card">
    <h2>Electricity demand</h2>
    <div class="legend">
      <span><i class="swatch" style="background:var(--baseline)"></i>Baseline</span>
      <span><i class="swatch" style="background:var(--ai)"></i>AI closed-loop</span>
    </div>
    <div class="plot"><canvas id="c-demand"></canvas><div class="tip" id="tip-demand"></div></div>
  </div>

  <div class="card">
    <h2>Outdoor temperature today</h2>
    <p class="cap" id="wxcap">Live from Open-Meteo for this site — the hours already observed today, refreshed every 15 minutes.</p>
    <div class="plot"><canvas id="c-weather"></canvas><div class="tip" id="tip-weather"></div></div>
  </div>

  <details>
    <summary>Show the numbers as a table</summary>
    <div class="scroll"><table id="tbl"><thead><tr>
      <th>Hour</th><th>Baseline (kW)</th><th>AI (kW)</th><th>Difference (kW)</th><th>Outdoor (°C)</th>
    </tr></thead><tbody></tbody></table></div>
  </details>

  <footer id="foot"></footer>
</div>

<script>
"use strict";
const SITES = __SITES__;
const WEATHER_REFRESH_MS = 15 * 60 * 1000;  // matches Open-Meteo's own update rate
const REDRAW_MS = 1000;

let site = SITES[0];
let weather = { hours: [], error: null, fetchedAt: null };

/* ---------- time ---------------------------------------------------------
   "Now" is the *site's* local time, not the viewer's: a building in
   Hyderabad is midway through its afternoon regardless of where the page is
   being read from. Intl gives us that without shipping a timezone table. */
function siteNow(tz) {
  const p = new Intl.DateTimeFormat("en-GB", {
    timeZone: tz, hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date());
  const get = (t) => +p.find((x) => x.type === t).value;
  // Intl renders midnight as hour 24 in some engines; fold it back to 0.
  return { h: get("hour") % 24, m: get("minute"), s: get("second") };
}
const nowHours = (tz) => { const t = siteNow(tz); return t.h + t.m / 60 + t.s / 3600; };
const pad2 = (n) => String(n).padStart(2, "0");

/* Value of an hourly series at a fractional hour, linearly interpolated, so
   the line ends exactly at the current minute instead of stepping once an
   hour. Beyond the last sample it clamps rather than extrapolating. */
function at(series, t) {
  if (!series || !series.length) return null;
  const i = Math.floor(t), f = t - i;
  const a = series[Math.min(i, series.length - 1)];
  const b = series[Math.min(i + 1, series.length - 1)];
  if (a == null || b == null) return a == null ? b : a;
  return a + (b - a) * f;
}

/* Samples from 00:00 up to `tMax` only. This is the whole point of the page:
   nothing past "now" is drawn -- no flat run-out, no gap, no forecast. */
function revealed(series, tMax, step = 0.05) {
  const pts = [];
  if (!series || !series.length) return pts;
  for (let t = 0; t <= tMax; t = +(t + step).toFixed(4)) {
    const v = at(series, t);
    if (v != null) pts.push([t, v]);
  }
  const last = at(series, tMax);
  if (last != null && (!pts.length || pts[pts.length - 1][0] < tMax)) pts.push([tMax, last]);
  return pts;
}

/* ---------- canvas chart -------------------------------------------------
   Hand-rolled so the page stays a single file with no CDN and no build step.
   One y-axis per chart, always: outdoor temperature and kW are different
   measures, so they get their own charts rather than a second axis that
   would invite the reader to see a correlation the geometry invented. */
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const FONT = '12px Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif';

function niceTicks(lo, hi, count) {
  const span = hi - lo || 1;
  const raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}

function drawChart(cv, cfg) {
  const dpr = window.devicePixelRatio || 1;
  const W = cv.parentElement.clientWidth;
  const H = cfg.height || 250;
  cv.width = W * dpr; cv.height = H * dpr;
  cv.style.height = H + "px";
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);

  const all = cfg.series.flatMap((s) => (s.data || []).filter((v) => v != null));
  if (!all.length) {
    g.fillStyle = css("--muted"); g.font = FONT; g.textAlign = "center";
    g.fillText(cfg.empty || "No data", W / 2, H / 2);
    return null;
  }
  let lo = Math.min(...all), hi = Math.max(...all);
  if (cfg.includeZero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  const padY = (hi - lo) * 0.12 || 0.5;
  lo -= padY; hi += padY;

  const yTicks = niceTicks(lo, hi, 4);
  g.font = FONT;
  const labW = Math.max(...yTicks.map((t) => g.measureText(cfg.fmt(t)).width));
  const M = { l: labW + 14, r: 14, t: 10, b: 30 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b;
  const X = (t) => M.l + (t / 24) * pw;
  const Y = (v) => M.t + ph - ((v - lo) / (hi - lo)) * ph;

  // Grid: recessive solid hairlines, never dashed.
  g.strokeStyle = css("--grid"); g.lineWidth = 1;
  g.fillStyle = css("--muted"); g.textBaseline = "middle";
  for (const t of yTicks) {
    const y = Math.round(Y(t)) + 0.5;
    g.beginPath(); g.moveTo(M.l, y); g.lineTo(M.l + pw, y); g.stroke();
    g.textAlign = "right"; g.fillText(cfg.fmt(t), M.l - 8, y);
  }
  g.textBaseline = "top"; g.textAlign = "center";
  for (let h = 0; h <= 24; h += 3) {
    const x = Math.round(X(h)) + 0.5;
    g.beginPath(); g.moveTo(x, M.t); g.lineTo(x, M.t + ph); g.stroke();
    g.fillText(h === 24 ? "24" : pad2(h), x, M.t + ph + 9);
  }

  // Zero reference, drawn a step darker than the grid so "no change" reads.
  if (cfg.zeroLine && lo < 0 && hi > 0) {
    const y = Math.round(Y(0)) + 0.5;
    g.strokeStyle = css("--hairline"); g.lineWidth = 1.5;
    g.beginPath(); g.moveTo(M.l, y); g.lineTo(M.l + pw, y); g.stroke();
  }

  const tMax = cfg.revealTo;
  const labels = [];
  for (const s of cfg.series) {
    const pts = revealed(s.data, tMax);
    if (pts.length < 2) continue;

    if (s.fill && cfg.zeroLine) {
      // A soft fill between the line and zero -- an accent that shows the sign
      // at a glance, kept translucent so it never becomes a saturated block.
      g.save(); g.globalAlpha = 0.13; g.fillStyle = s.color;
      g.beginPath(); g.moveTo(X(pts[0][0]), Y(0));
      for (const [t, v] of pts) g.lineTo(X(t), Y(v));
      g.lineTo(X(pts[pts.length - 1][0]), Y(0)); g.closePath(); g.fill(); g.restore();
    }

    g.strokeStyle = s.color; g.lineWidth = 2;
    g.lineJoin = "round"; g.lineCap = "round";
    g.beginPath();
    pts.forEach(([t, v], i) => (i ? g.lineTo(X(t), Y(v)) : g.moveTo(X(t), Y(v))));
    g.stroke();

    // The live end of the line: a 2px surface ring keeps it legible where it
    // overlaps the other series.
    const [lt, lv] = pts[pts.length - 1];
    g.beginPath(); g.arc(X(lt), Y(lv), 5, 0, Math.PI * 2);
    g.fillStyle = s.color; g.fill();
    g.lineWidth = 2; g.strokeStyle = css("--panel"); g.stroke();

    // Sit the label just clear of the end-dot; collisions are resolved below.
    if (s.label) labels.push({ text: s.label, x: X(lt), y: Y(lv) - 9 });
  }

  // Direct labels last, with collision resolution. Baseline and AI converge to
  // within a few tenths of a kW in the afternoon, which stacked the two labels
  // on top of each other (and ran "AI" straight through the baseline's dot)
  // when each was drawn at its own series' height. Push them apart to a legible
  // gap, preserving vertical order so each still reads against the right line.
  const GAP = 15;
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) {
    if (labels[i].y - labels[i - 1].y < GAP) labels[i].y = labels[i - 1].y + GAP;
  }
  // Keep the resolved stack inside the plot, shifting it as a block so the
  // separation just established survives the clamp.
  if (labels.length) {
    const over = labels[labels.length - 1].y - (M.t + ph - 4);
    const under = M.t + 8 - labels[0].y;
    const shift = over > 0 ? -over : under > 0 ? under : 0;
    for (const l of labels) l.y += shift;
  }

  g.font = FONT; g.textBaseline = "middle"; g.fillStyle = css("--text-2");
  for (const l of labels) {
    const right = l.x > M.l + pw - 60;
    g.textAlign = right ? "right" : "left";
    g.fillText(l.text, l.x + (right ? -12 : 12), l.y);
  }
  return { X, Y, M, pw, ph, lo, hi };
}

/* ---------- hover --------------------------------------------------------
   Crosshair + tooltip on every chart; the pointer only reads hours that have
   actually happened, so hovering the empty future shows nothing. */
function attachHover(cv, tipEl, getCfg) {
  let geom = null;
  const move = (ev) => {
    const cfg = getCfg();
    geom = cfg.geom;
    if (!geom) return;
    const r = cv.getBoundingClientRect();
    const x = ev.clientX - r.left;
    let t = ((x - geom.M.l) / geom.pw) * 24;
    if (t < 0 || t > cfg.revealTo || x > geom.M.l + geom.pw) { tipEl.style.opacity = 0; return; }
    const hh = Math.round(t * 4) / 4;
    const rows = cfg.series
      .map((s) => ({ name: s.name, color: s.color, v: at(s.data, Math.min(hh, cfg.revealTo)) }))
      .filter((r) => r.v != null);
    if (!rows.length) { tipEl.style.opacity = 0; return; }
    const hI = Math.floor(hh), mI = Math.round((hh - hI) * 60);
    tipEl.innerHTML =
      `<div class="th">${pad2(hI)}:${pad2(mI)}</div>` +
      rows.map((r) =>
        `<div class="tr"><i class="swatch" style="background:${r.color}"></i>` +
        `${r.name} <b>${cfg.fmt(r.v)}</b></div>`).join("");
    tipEl.style.opacity = 1;
    const tw = tipEl.offsetWidth;
    tipEl.style.left = Math.min(Math.max(geom.X(hh) + 12, 4), cv.clientWidth - tw - 4) + "px";
    tipEl.style.top = Math.max(geom.M.t, geom.Y(rows[0].v) - tipEl.offsetHeight - 12) + "px";
  };
  cv.addEventListener("pointermove", move);
  cv.addEventListener("pointerleave", () => (tipEl.style.opacity = 0));
}

/* ---------- weather ------------------------------------------------------ */
async function loadWeather() {
  const u = `https://api.open-meteo.com/v1/forecast?latitude=${site.lat}&longitude=${site.lon}`
          + `&hourly=temperature_2m&timezone=${encodeURIComponent(site.tz)}&forecast_days=1`;
  try {
    const r = await fetch(u);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    weather = { hours: j.hourly.temperature_2m.slice(0, 24), error: null, fetchedAt: new Date() };
  } catch (e) {
    weather = { hours: [], error: e.message, fetchedAt: null };
  }
  render();
}

/* ---------- render ------------------------------------------------------- */
const cfgs = {};
const fmtKw = (v) => v.toFixed(2) + " kW";
// Round to zero *before* signing, so an overnight setback hour reads "0.00 kW"
// rather than "-0.00 kW" -- a sign on a zero reads as a real direction.
const fmtDelta = (v) => {
  const z = Math.abs(v) < 0.005 ? 0 : v;
  return (z > 0 ? "+" : z < 0 ? "" : "") + z.toFixed(2) + " kW";
};
const fmtC = (v) => v.toFixed(1) + "°C";

function render() {
  const t = nowHours(site.tz);
  const clk = siteNow(site.tz);
  document.getElementById("clock").textContent = `${pad2(clk.h)}:${pad2(clk.m)}:${pad2(clk.s)}`;
  document.getElementById("clockzone").textContent = `${site.short} local · ${site.tz}`;

  const delta = site.baselineKw.map((b, i) => +(site.aiKw[i] - b).toFixed(3));
  const dNow = at(delta, t), bNow = at(site.baselineKw, t), aNow = at(site.aiKw, t);

  // Energy saved since midnight: the difference integrated over elapsed time
  // (trapezoid over the same fine samples the line is drawn from).
  let kwh = 0;
  const pts = revealed(delta, t);
  for (let i = 1; i < pts.length; i++) kwh += -((pts[i][1] + pts[i - 1][1]) / 2) * (pts[i][0] - pts[i - 1][0]);

  document.getElementById("t-delta").textContent = fmtDelta(dNow);
  document.getElementById("t-delta-n").textContent =
    dNow < -0.005 ? "AI drawing less" : dNow > 0.005 ? "AI drawing more" : "no difference (setback)";
  document.getElementById("t-kwh").textContent =
    (Math.abs(kwh) < 0.005 ? 0 : kwh).toFixed(2) + " kWh";
  document.getElementById("t-dem").textContent = aNow.toFixed(2) + " kW";
  document.getElementById("t-dem-n").textContent = `baseline ${bNow.toFixed(2)} kW`;

  const wNow = weather.hours.length ? at(weather.hours, t) : null;
  document.getElementById("t-temp").textContent = wNow == null ? "—" : fmtC(wNow);
  document.getElementById("t-temp-n").textContent =
    weather.error ? "forecast unavailable" : weather.hours.length ? "live forecast" : "loading…";

  cfgs.delta = {
    series: [{ name: "AI − baseline", data: delta, color: css("--ai"), fill: true }],
    revealTo: t, fmt: fmtDelta, height: 250, zeroLine: true, includeZero: true,
  };
  cfgs.demand = {
    series: [
      { name: "Baseline", data: site.baselineKw, color: css("--baseline"), label: "Baseline" },
      { name: "AI closed-loop", data: site.aiKw, color: css("--ai"), label: "AI" },
    ],
    revealTo: t, fmt: fmtKw, height: 260,
  };
  cfgs.weather = {
    series: [{ name: "Outdoor", data: weather.hours, color: css("--weather") }],
    revealTo: t, fmt: fmtC, height: 220,
    empty: weather.error ? "Live weather unavailable (" + weather.error + ")" : "Loading live weather…",
  };
  for (const k of ["delta", "demand", "weather"]) {
    cfgs[k].geom = drawChart(document.getElementById("c-" + k), cfgs[k]);
  }

  const wxcap = document.getElementById("wxcap");
  wxcap.className = "cap" + (weather.error ? " err" : "");
  wxcap.textContent = weather.error
    ? "Could not reach Open-Meteo (" + weather.error + "). The energy charts above are unaffected — they do not depend on it."
    : `Live from Open-Meteo for ${site.short} — only the hours already elapsed today are drawn. Refreshed every 15 minutes`
      + (weather.fetchedAt ? `, last at ${weather.fetchedAt.toLocaleTimeString()}.` : ".");

  // Table view: the same numbers, for screen readers and for anyone who wants
  // the values rather than the shape.
  const tb = document.querySelector("#tbl tbody");
  tb.innerHTML = "";
  for (let h = 0; h <= Math.floor(t); h++) {
    const w = weather.hours[h];
    tb.insertAdjacentHTML("beforeend",
      `<tr><td>${pad2(h)}:00</td><td>${site.baselineKw[h].toFixed(2)}</td>` +
      `<td>${site.aiKw[h].toFixed(2)}</td><td>${(delta[h] > 0 ? "+" : "") + delta[h].toFixed(2)}</td>` +
      `<td>${w == null ? "—" : w.toFixed(1)}</td></tr>`);
  }

  document.getElementById("foot").innerHTML =
    `<b>What is live here.</b> The clock and the outdoor temperature are: the time is ${site.short}'s own, ` +
    `and the weather is today's real forecast for that site, pulled from Open-Meteo in your browser and ` +
    `refreshed every 15 minutes. Every curve is drawn only as far as that clock has reached.<br><br>` +
    `<b>What is not.</b> The two energy curves are measured EnergyPlus output — the ${site.daysAveraged} occupied ` +
    `weekdays of the ${site.period} run (${site.dates.join(", ")}), averaged per hour of day — replayed against ` +
    `the clock. They are real results from the real closed-loop agent, but they were not recomputed for today's ` +
    `weather: that needs EnergyPlus and the local LLM, which a static page cannot run. Regenerate with ` +
    `<code>python dashboard/build_live_page.py</code> after a new comparison run.`;
}

/* ---------- wiring ------------------------------------------------------- */
const sel = document.getElementById("site");
SITES.forEach((s, i) => sel.add(new Option(s.label, String(i))));
sel.addEventListener("change", () => {
  site = SITES[+sel.value];
  weather = { hours: [], error: null, fetchedAt: null };
  render();
  loadWeather();
});

document.getElementById("theme").addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme")
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.setAttribute("data-theme", cur === "dark" ? "light" : "dark");
  render();
});

for (const k of ["delta", "demand", "weather"]) {
  attachHover(document.getElementById("c-" + k), document.getElementById("tip-" + k), () => cfgs[k]);
}
addEventListener("resize", render);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);

render();
loadWeather();
setInterval(render, REDRAW_MS);
setInterval(loadWeather, WEATHER_REFRESH_MS);
</script>
</body>
</html>
"""


def main():
    payload = build_payload()
    html = TEMPLATE.replace("__SITES__", json.dumps(payload, separators=(",", ":")))
    OUT.write_text(html)
    print(f"wrote {OUT.relative_to(REPO)}  ({len(html):,} bytes)")
    for s in payload:
        delta = [round(s["aiKw"][i] - s["baselineKw"][i], 2) for i in range(24)]
        print(
            f"  {s['key']:10s} {s['daysAveraged']} weekdays {s['dates']}  "
            f"peak saving {min(delta):.2f} kW"
        )


if __name__ == "__main__":
    main()
