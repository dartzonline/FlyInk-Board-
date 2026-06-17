import json
import os
import logging
import platform
import shutil
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from src.config import CONTROL_PORT, LOGO_DIR, HOME_LAT, HOME_LON
from src.display import INKY_AVAILABLE, get_net, cpu_temp
from src.tracking import TRACK, TRACK_LOCK, normalize_query

log = logging.getLogger(__name__)
SERVER_STARTED_AT = time.time()

STATE_LOCK = threading.Lock()
STATE = {
    "nearby":     [],
    "current":    None,
    "weather":    {},
    "updated_at": None,
}

# tiny ring buffer so the stats tab has something to chew on
_seen_history = []          # list of (timestamp, callsign, airline, alt_ft, spd_kt)
_history_lock = threading.Lock()
MAX_HISTORY   = 500


def record_nearby(flights):
    """Call this each poll cycle so we build up historical stats."""
    ts = time.time()
    with _history_lock:
        for f in flights:
            cs = f.get("callsign") or ""
            if cs:
                _seen_history.append((ts, cs, f.get("airline"), f.get("alt_ft"), f.get("spd_kt")))
        # keep the buffer lean
        if len(_seen_history) > MAX_HISTORY:
            del _seen_history[:len(_seen_history) - MAX_HISTORY]


def _stats_snapshot():
    cutoff = time.time() - 3600   # last hour
    with _history_lock:
        recent = [r for r in _seen_history if r[0] >= cutoff]
    if not recent:
        return {}
    callsigns = [r[1] for r in recent]
    airlines  = [r[2] for r in recent if r[2]]
    alts      = [r[3] for r in recent if r[3]]
    speeds    = [r[4] for r in recent if r[4]]
    top_airline = Counter(airlines).most_common(1)
    return {
        "seen_1h":      len(set(callsigns)),
        "top_airline":  top_airline[0][0] if top_airline else None,
        "top_count":    top_airline[0][1] if top_airline else 0,
        "max_alt_ft":   max(alts)  if alts   else None,
        "max_spd_kt":   max(speeds) if speeds else None,
        "total_logged": len(_seen_history),
    }


# --- logo serving -----------------------------------------------------------

def _logo_bytes(icao_code):
    if not icao_code:
        return None
    path = os.path.join(LOGO_DIR, f"{icao_code.upper()}.png")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


# --- track state helpers ----------------------------------------------------

def _track_payload(track_q):
    if not track_q:
        return None
    from src import tracking as trk
    ctx = trk.track_context()
    if not ctx:
        return None

    sched    = ctx.get("sched") or {}
    state    = ctx.get("state")
    frac     = 0.0
    eta_line = ""

    if state:
        from src.flights import haversine, fetch_route, _airport_obj
        cs  = (state[1] or "").strip()
        fr  = fetch_route(cs)
        o   = _airport_obj(fr["origin"])      if fr else None
        dst = _airport_obj(fr["destination"]) if fr else None
        lat, lon = state[6], state[5]
        if o and dst and o.get("lat") and dst.get("lat") and lat:
            tot = haversine(o["lat"], o["lon"], dst["lat"], dst["lon"])
            if tot > 1:
                frac = haversine(o["lat"], o["lon"], lat, lon) / tot
        if ctx["mode"] == "track" and dst and dst.get("lat") and state[9] and state[9] > 30:
            rem  = haversine(lat, lon, dst["lat"], dst["lon"])
            mins = rem / (state[9] * 3.6) * 60
            eta  = (datetime.now() + timedelta(minutes=mins)).strftime("%I:%M %p").lstrip("0")
            eta_line = f"ETA ~{eta}  ·  {int(mins)} min left"

    return {
        "query":    track_q,
        "mode":     ctx["mode"],
        "sched":    sched,
        "frac":     round(frac, 3),
        "eta_line": eta_line,
    }


# --- HTML -------------------------------------------------------------------

_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>✈ FlyInk Board</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin=""/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<style>
  :root {
    --red:#c0392b; --red2:#e74c3c; --dark:#0d0d0d; --mid:#181818;
    --card:#202020; --border:#2a2a2a; --text:#e8e8e8; --muted:#666;
    --green:#27ae60; --blue:#2980b9; --yellow:#f39c12; --orange:#e67e22;
  }
  * { box-sizing:border-box; margin:0; padding:0 }
  body { background:var(--dark); color:var(--text);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         min-height:100vh; overflow-x:hidden }

  /* ---- Header ---- */
  header {
    background:linear-gradient(135deg,#1a0000 0%,#2d0a0a 60%,#1a0000 100%);
    border-bottom:2px solid var(--red);
    padding:.85rem 1.5rem;
    display:flex; align-items:center; gap:.75rem;
    position:sticky; top:0; z-index:100;
  }
  header h1 { font-size:1.3rem; letter-spacing:.08em; font-weight:700 }
  .header-plane { font-size:1.5rem; animation:drift 4s ease-in-out infinite }
  @keyframes drift { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-4px)} }
  #weather-pill {
    margin-left:auto; display:flex; align-items:center; gap:.6rem;
    font-size:.8rem; color:rgba(255,255,255,.75);
    background:rgba(255,255,255,.07); border-radius:20px;
    padding:.3rem .8rem; border:1px solid rgba(255,255,255,.1);
  }
  #refresh-ring {
    width:22px; height:22px; position:relative; flex-shrink:0; margin-left:.5rem;
  }
  #refresh-ring svg { width:22px; height:22px; transform:rotate(-90deg) }
  #ring-fill { stroke-dasharray:63; stroke-dashoffset:63; transition:stroke-dashoffset 1s linear }
  #updated-time { font-size:.75rem; color:rgba(255,255,255,.5) }

  /* ---- Nav ---- */
  nav { display:flex; background:var(--mid); border-bottom:1px solid var(--border) }
  nav button {
    flex:1; padding:.7rem .5rem; background:none; border:none;
    color:var(--muted); font-size:.875rem; cursor:pointer;
    border-bottom:2px solid transparent; transition:color .15s,border-color .15s;
    letter-spacing:.03em;
  }
  nav button.active { color:var(--text); border-color:var(--red) }
  nav button:hover:not(.active) { color:#bbb }
  nav .kb { font-size:.65rem; color:#444; margin-left:.3rem }

  /* ---- Tabs ---- */
  .tab { display:none; padding:1.25rem; max-width:1000px; margin:0 auto }
  .tab.active { display:block }

  /* ---- Stats bar ---- */
  #stats-bar {
    display:flex; gap:.75rem; flex-wrap:wrap; margin-bottom:1rem;
    padding:.75rem; background:var(--card); border-radius:8px;
    border:1px solid var(--border);
  }
  .stat { display:flex; flex-direction:column; gap:.15rem; min-width:90px }
  .stat label { font-size:.65rem; color:var(--muted); text-transform:uppercase; letter-spacing:.06em }
  .stat span  { font-size:1rem; font-weight:700; color:var(--text) }

  /* ---- Filter bar ---- */
  #filter-wrap { margin-bottom:.75rem; display:flex; gap:.6rem }
  #filter-input {
    flex:1; padding:.5rem .8rem; background:var(--mid);
    border:1px solid var(--border); border-radius:7px;
    color:var(--text); font-size:.875rem; outline:none;
  }
  #filter-input:focus { border-color:var(--red) }
  #filter-count { font-size:.8rem; color:var(--muted); align-self:center }

  /* ---- Nearby table ---- */
  table { width:100%; border-collapse:collapse; font-size:.875rem }
  thead th {
    text-align:left; padding:.5rem .6rem;
    color:var(--muted); font-size:.7rem; letter-spacing:.07em;
    text-transform:uppercase; border-bottom:1px solid var(--border);
    font-weight:600; cursor:pointer; user-select:none;
  }
  thead th:hover { color:#aaa }
  thead th .sort-arrow { opacity:.4; margin-left:.2rem }
  thead th.sorted .sort-arrow { opacity:1; color:var(--red) }
  td { padding:.5rem .6rem; border-bottom:1px solid rgba(255,255,255,.04) }
  tr.current-row td { background:rgba(192,57,43,.12) }
  tr:hover td { background:rgba(255,255,255,.04) }
  .logo-cell { width:48px }
  .logo-cell img { height:26px; object-fit:contain; filter:brightness(1.1); vertical-align:middle }
  .logo-placeholder { width:32px; height:18px; display:inline-block }
  .cs { font-weight:700; font-family:monospace; font-size:.9rem; letter-spacing:.03em }
  .type-cell { color:var(--muted); font-size:.8rem }
  @keyframes rowIn { from{opacity:0;transform:translateY(-4px)} to{opacity:1;transform:none} }
  tbody tr { animation:rowIn .18s ease }

  /* ---- Phase badges ---- */
  .badge { display:inline-flex; align-items:center; gap:.25rem;
           padding:.15rem .45rem; border-radius:4px;
           font-size:.7rem; font-weight:700; letter-spacing:.05em }
  .badge-climb   { background:#0e3460; color:#5dade2 }
  .badge-cruise  { background:#0e3a1a; color:#58d68d }
  .badge-descend { background:#4a2c0a; color:#f0b27a }
  .badge-gnd     { background:#2a2a2a; color:#999 }

  /* ---- Radar Wrap and Screen ---- */
  #radar-wrap {
    margin-bottom:1rem; background:var(--card);
    border:1px solid var(--border); border-radius:8px;
    padding:.75rem; overflow:hidden;
  }
  #radar-title { font-size:.7rem; color:var(--muted); text-transform:uppercase;
                 letter-spacing:.07em; margin-bottom:.5rem }
  .radar-screen-container {
    position: relative;
    width: 320px;
    height: 320px;
    margin: 0 auto;
    border-radius: 50%;
    overflow: hidden;
    border: 2px solid #2a2a2a;
    box-shadow: 0 0 20px rgba(0,0,0,0.5), inset 0 0 20px rgba(0,0,0,0.8);
    background: #0b0b0b;
  }
  #radar-map {
    width: 100%;
    height: 100%;
    border-radius: 50%;
  }
  #radar-svg {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    z-index: 90;
  }
  .radar-sweep {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: conic-gradient(from 0deg, rgba(39, 174, 96, 0.15) 0deg, rgba(39, 174, 96, 0.03) 90deg, rgba(39, 174, 96, 0) 180deg);
    border-radius: 50%;
    pointer-events: none;
    z-index: 100;
    animation: radar-sweep 5s linear infinite;
  }
  @keyframes radar-sweep {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }
  .plane-marker-icon {
    background: none !important;
    border: none !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
  }
  
  /* ---- Leaflet popup dark mode override ---- */
  .leaflet-popup-content-wrapper, .leaflet-popup-tip {
    background: #181818 !important;
    color: #e8e8e8 !important;
    border: 1px solid #2a2a2a;
    box-shadow: 0 3px 14px rgba(0,0,0,0.6) !important;
    border-radius: 6px !important;
  }
  .leaflet-popup-content {
    margin: 8px 10px !important;
    font-size: 11px !important;
    line-height: 1.35 !important;
  }
  .leaflet-popup-close-button {
    color: #888 !important;
    padding: 4px 8px 0 0 !important;
  }

  /* ---- Now Showing card ---- */
  .card {
    background:var(--card); border:1px solid var(--border);
    border-radius:10px; padding:1.4rem; margin-bottom:1rem;
  }
  .card-header { display:flex; align-items:flex-start; gap:1rem; margin-bottom:.75rem }
  .card-logo { flex-shrink:0 }
  .card-logo img { height:48px; object-fit:contain; filter:brightness(1.1) }
  .flight-num { font-size:1.1rem; color:var(--red); font-weight:800; letter-spacing:.04em }
  .airline-name { font-size:.875rem; color:var(--muted); margin-top:.1rem }
  .reg-badge {
    display:inline-block; font-size:.7rem; color:#aaa;
    background:var(--mid); border:1px solid var(--border);
    border-radius:4px; padding:.1rem .4rem; margin-top:.25rem;
    font-family:monospace; letter-spacing:.04em;
  }
  .route {
    display:flex; align-items:center; gap:.75rem;
    font-size:2.6rem; font-weight:800; margin:.5rem 0 .2rem;
    letter-spacing:.04em;
  }
  .route .arrow { font-size:1.3rem; color:var(--muted) }
  .city-row { font-size:.8rem; color:var(--muted); margin-bottom:1rem }
  .meta { display:grid; grid-template-columns:repeat(auto-fit,minmax(100px,1fr)); gap:.6rem }
  .meta-item label { display:block; font-size:.65rem; color:var(--muted);
                     text-transform:uppercase; letter-spacing:.06em; margin-bottom:.15rem }
  .meta-item span  { font-size:1rem; font-weight:700 }

  /* ---- Track form ---- */
  .track-form { display:flex; gap:.6rem; margin-bottom:1.25rem; flex-wrap:wrap }
  .track-form input {
    flex:1; min-width:200px; padding:.6rem .9rem;
    background:var(--mid); border:1px solid var(--border);
    border-radius:8px; color:var(--text); font-size:1rem; outline:none;
    text-transform:uppercase; letter-spacing:.05em;
    transition:border-color .15s;
  }
  .track-form input:focus { border-color:var(--red) }
  .track-form button {
    padding:.6rem 1.3rem; border:none; border-radius:8px;
    font-size:.9rem; cursor:pointer; font-weight:700; letter-spacing:.04em;
    transition:background .15s, transform .1s;
  }
  .track-form button:active { transform:scale(.96) }
  .btn-track { background:var(--red); color:#fff }
  .btn-track:hover { background:#a93226 }
  .btn-stop  { background:#333; color:#ccc }
  .btn-stop:hover { background:#444 }

  /* ---- Track status box ---- */
  .status-box {
    background:var(--card); border:1px solid var(--border);
    border-radius:10px; padding:1.2rem;
  }
  .track-header { display:flex; align-items:center; gap:.75rem; margin-bottom:.6rem; flex-wrap:wrap }
  .track-flight { font-size:1.1rem; font-weight:800; letter-spacing:.04em }
  .status-label { font-size:.75rem; font-weight:700; letter-spacing:.07em;
                  padding:.2rem .6rem; border-radius:4px;
                  background:rgba(255,255,255,.08); color:#bbb }
  .status-enroute  { background:rgba(39,174,96,.2);  color:#58d68d }
  .status-landed   { background:rgba(52,152,219,.2); color:#5dade2 }
  .status-awaiting { background:rgba(255,255,255,.08); color:#bbb }
  .delay-badge {
    font-size:.78rem; font-weight:700; padding:.2rem .65rem;
    border-radius:20px; letter-spacing:.03em;
  }
  .delay-pos  { background:rgba(231,76,60,.2);  color:#e74c3c }
  .delay-neg  { background:rgba(39,174,96,.2);  color:#2ecc71 }
  .delay-zero { background:rgba(52,152,219,.2); color:#3498db }

  .route-line {
    display:flex; align-items:center; gap:1.5rem;
    font-size:1.5rem; font-weight:800; margin:.4rem 0 1rem; letter-spacing:.04em;
  }
  .route-line .arrow { color:var(--muted); font-size:1rem }

  .progress-wrap { position:relative; padding:1.2rem 0 .5rem; margin-bottom:.5rem }
  .progress-track {
    height:3px; background:#2a2a2a; border-radius:2px;
    position:relative; overflow:visible;
  }
  .progress-fill {
    height:3px; background:var(--red); border-radius:2px;
    transition:width .8s cubic-bezier(.4,0,.2,1);
    box-shadow:0 0 6px rgba(192,57,43,.6);
  }
  .progress-plane {
    position:absolute; top:50%; font-size:1.3rem;
    transform:translate(-50%, -50%);
    transition:left .8s cubic-bezier(.4,0,.2,1);
    filter:drop-shadow(0 0 4px rgba(255,100,80,.5));
  }
  .progress-airport {
    position:absolute; top:50%; font-size:.75rem; color:var(--muted);
    transform:translateY(-50%); font-family:monospace; font-weight:700;
  }
  .progress-airport.dep { left:0; transform:translate(-50%,-50%) }
  .progress-airport.arr { right:0; transform:translate(50%,-50%) }
  .times {
    display:grid; grid-template-columns:1fr 1fr; gap:.75rem; margin-top:.75rem;
    padding-top:.75rem; border-top:1px solid var(--border);
  }
  .time-col label { font-size:.65rem; color:var(--muted); text-transform:uppercase;
                    letter-spacing:.06em; display:block; margin-bottom:.2rem }
  .time-col .t { font-size:1.25rem; font-weight:700 }
  .time-col .revised { color:var(--red); font-size:.82rem; margin-top:.15rem }
  .time-col.right { text-align:right }
  .eta-line { font-size:.82rem; color:var(--muted); margin-top:.6rem; text-align:center }
  #no-track { color:var(--muted); text-align:center; padding:2.5rem; font-size:.9rem }

  /* ---- Toast ---- */
  #toast {
    position:fixed; bottom:1.5rem; right:1.5rem;
    background:#222; border:1px solid #444; border-radius:8px;
    padding:.7rem 1rem; font-size:.85rem; color:#ddd;
    box-shadow:0 4px 20px rgba(0,0,0,.5);
    transform:translateY(100px); opacity:0;
    transition:transform .3s,opacity .3s;
    z-index:999; max-width:280px;
  }
  #toast.show { transform:none; opacity:1 }

  /* ---- Weather tab ---- */
  .wx-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:.75rem }
  .wx-card {
    background:var(--card); border:1px solid var(--border);
    border-radius:8px; padding:1rem; text-align:center;
  }
  .wx-val { font-size:2rem; font-weight:700; margin:.4rem 0 }
  .wx-label { font-size:.7rem; color:var(--muted); text-transform:uppercase; letter-spacing:.06em }
  /* ---- System status ---- */
  .sys-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:.75rem; margin-bottom:1rem }
  .sys-card {
    background:var(--card); border:1px solid var(--border);
    border-radius:8px; padding:1rem;
  }
  .sys-label { font-size:.7rem; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; margin-bottom:.25rem }
  .sys-val { font-size:1.15rem; font-weight:700 }
  .sys-sub { font-size:.8rem; color:var(--muted); margin-top:.2rem }

  @media(max-width:600px) {
    .route { font-size:1.8rem }
    header h1 { font-size:1rem }
    #weather-pill { display:none }
  }
</style>
</head>
<body>

<header>
  <span class="header-plane">✈</span>
  <h1>FlyInk Board</h1>
  <div id="weather-pill">
    <span id="wx-temp"></span>
    <span id="wx-cond"></span>
    <span id="wx-wind"></span>
    <div id="refresh-ring">
      <svg viewBox="0 0 22 22">
        <circle cx="11" cy="11" r="9" fill="none" stroke="#333" stroke-width="2.5"/>
        <circle id="ring-fill" cx="11" cy="11" r="9" fill="none" stroke="#c0392b"
                stroke-width="2.5" stroke-linecap="round"/>
      </svg>
    </div>
    <span id="updated-time">--:--</span>
  </div>
</header>

<nav>
  <button class="active" onclick="showTab('nearby',this)">
    Nearby <small class="kb">[1]</small>
  </button>
  <button onclick="showTab('showing',this)">
    Now Showing <small class="kb">[2]</small>
  </button>
  <button onclick="showTab('track',this)">
    Track <small class="kb">[3]</small>
  </button>
  <button onclick="showTab('weather',this)">
    Weather <small class="kb">[4]</small>
  </button>
</nav>

<!-- TAB 1: Nearby -->
<div class="tab active" id="tab-nearby">
  <div id="stats-bar">
    <div class="stat"><label>Nearby now</label><span id="st-count">--</span></div>
    <div class="stat"><label>Seen (1h)</label><span id="st-seen">--</span></div>
    <div class="stat"><label>Highest</label><span id="st-alt">--</span></div>
    <div class="stat"><label>Fastest</label><span id="st-spd">--</span></div>
    <div class="stat"><label>Top airline</label><span id="st-top">--</span></div>
  </div>
  <div id="radar-wrap">
    <div id="radar-title">Radar — 120 km range</div>
    <div class="radar-screen-container">
      <div id="radar-map"></div>
      <div class="radar-sweep"></div>
      <svg id="radar-svg" width="320" height="320" viewBox="0 0 320 320"></svg>
    </div>
  </div>
  <div id="filter-wrap">
    <input id="filter-input" type="text" placeholder="Filter flights…" oninput="applyFilter()">
    <span id="filter-count"></span>
  </div>
  <table>
    <thead>
      <tr>
        <th class="logo-cell"></th>
        <th onclick="sortBy('callsign')">Flight <span class="sort-arrow">↕</span></th>
        <th onclick="sortBy('airline')">Airline <span class="sort-arrow">↕</span></th>
        <th class="type-cell" onclick="sortBy('type')">Type <span class="sort-arrow">↕</span></th>
        <th onclick="sortBy('alt_ft')">Altitude <span class="sort-arrow">↕</span></th>
        <th onclick="sortBy('spd_kt')">Speed <span class="sort-arrow">↕</span></th>
        <th onclick="sortBy('dist_km')">Distance <span class="sort-arrow">↕</span></th>
        <th>Phase</th>
      </tr>
    </thead>
    <tbody id="nearby-body"><tr><td colspan="8" style="text-align:center;color:var(--muted);padding:2rem">Scanning the skies…</td></tr></tbody>
  </table>
</div>

<!-- TAB 2: Now Showing -->
<div class="tab" id="tab-showing">
  <div id="showing-content">
    <p style="text-align:center;color:var(--muted);padding:3rem">Nothing on screen yet.</p>
  </div>
</div>

<!-- TAB 3: Track -->
<div class="tab" id="tab-track">
  <div class="track-form">
    <input id="flight-input" type="text"
           placeholder="AA1234  ·  DL456  ·  DAL789"
           autocomplete="off" autocapitalize="characters"
           onkeydown="if(event.key==='Enter')trackFlight()">
    <button class="btn-track" id="btn-track" onclick="trackFlight()">Track</button>
    <button class="btn-stop"  onclick="stopTracking()">Stop</button>
  </div>
  <div id="track-content">
    <p id="no-track">No flight pinned. Type a flight number above.</p>
  </div>
</div>

<!-- TAB 4: Weather -->
<div class="tab" id="tab-weather">
  <div id="weather-content">
    <p style="text-align:center;color:var(--muted);padding:2rem">Loading weather…</p>
  </div>
</div>

<div id="toast"></div>

<script>
// ── state ────────────────────────────────────────────────────────────────────
let _s = {};
let _sortKey  = 'dist_km';
let _sortAsc  = true;
let _filter   = '';
let _tickInterval = null;
const REFRESH_MS  = 15000;
let _nextRefresh  = Date.now() + REFRESH_MS;
let _health = {};

// ── tabs / keyboard ──────────────────────────────────────────────────────────
function showTab(name, btn) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'nearby' && _map) {
    setTimeout(() => _map.invalidateSize(), 50);
  }
}

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  const tabs = ['nearby','showing','track','weather'];
  const n = parseInt(e.key);
  if (n >= 1 && n <= tabs.length) {
    const btns = document.querySelectorAll('nav button');
    showTab(tabs[n-1], btns[n-1]);
  }
});

// ── toast ────────────────────────────────────────────────────────────────────
let _toastTimer;
function toast(msg, ms = 3000) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), ms);
}

// ── refresh ring ─────────────────────────────────────────────────────────────
function tickRing() {
  const pct  = Math.max(0, (_nextRefresh - Date.now()) / REFRESH_MS);
  const circ = 63;
  document.getElementById('ring-fill').style.strokeDashoffset = circ * (1 - pct);
}
setInterval(tickRing, 500);

// ── Leaflet Radar Map ────────────────────────────────────────────────────────
let _map = null;
let _planeMarkers = {};

function initMap(home) {
  if (_map || !home || !home.lat || typeof L === 'undefined') return;

  const lat = parseFloat(home.lat);
  const lon = parseFloat(home.lon);

  // Initialize Leaflet map
  _map = L.map('radar-map', {
    center: [lat, lon],
    zoom: 9,
    zoomControl: false,
    attributionControl: false,
    dragging: false,
    scrollWheelZoom: false,
    doubleClickZoom: false,
    boxZoom: false,
    keyboard: false,
    touchZoom: false
  });

  // Dark sleek style without text labels to keep it uncluttered
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png', {
    maxZoom: 18
  }).addTo(_map);

  // Fit bounds precisely so range matches the SVG radar rings
  const range_km = 120;
  // half-width of the map container in km to match SVG R=145 inside size=320
  const half_width_km = range_km * (160 / 145);
  const dLat = half_width_km / 111.32;
  const dLon = half_width_km / (111.32 * Math.cos(lat * Math.PI / 180));
  
  _map.fitBounds([
    [lat - dLat, lon - dLon],
    [lat + dLat, lon + dLon]
  ], { animate: false });
}

function getPlaneIcon(track, col, callsign) {
  const rot = track || 0;
  const html = `
    <div style="position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center; width: 64px; height: 48px;">
      <svg viewBox="0 0 24 24" width="20" height="20" style="transform: rotate(${rot}deg); display: block;">
        <path fill="${col}" stroke="#000" stroke-width="0.5" d="M21,16V14L13,9V3.5A1.5,1.5 0 0,0 11.5,2A1.5,1.5 0 0,0 10,3.5V9L2,14V16L10,13.5V19L8,20.5V22L11.5,21L15,22V20.5L13,19V13.5L21,16Z"/>
      </svg>
      <span style="font-size: 8px; font-weight: bold; font-family: monospace; color: #fff; background: rgba(0,0,0,0.75); padding: 1px 3px; border-radius: 3px; margin-top: 2px; border: 1px solid #333; pointer-events: none; white-space: nowrap;">
        ${callsign}
      </span>
    </div>
  `;
  return L.divIcon({
    html: html,
    className: 'plane-marker-icon',
    iconSize: [64, 48],
    iconAnchor: [32, 24]
  });
}

window.trackFlightFromMap = async function(callsign) {
  if (!callsign) return;
  toast('Tracking ' + callsign + '…');
  await fetch('/track?flight=' + encodeURIComponent(callsign));
  setTimeout(fetchAll, 600);
  const btns = document.querySelectorAll('nav button');
  showTab('track', btns[2]);
};

function drawRadar(flights, range_km = 120) {
  const svg  = document.getElementById('radar-svg');
  const size = 320, cx = 160, cy = 160, R = 145;

  if (_s.home && typeof L !== 'undefined') {
    initMap(_s.home);
  }

  let html = '';
  // rings
  [1, 0.66, 0.33].forEach(f => {
    const r = R * f;
    html += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="rgba(39, 174, 96, 0.4)" stroke-dasharray="3, 3" stroke-width="1"/>`;
  });
  // compass ticks
  for (let a = 0; a < 360; a += 30) {
    const rad = a * Math.PI / 180;
    const x1 = cx + (R-8) * Math.sin(rad), y1 = cy - (R-8) * Math.cos(rad);
    const x2 = cx + R * Math.sin(rad),     y2 = cy - R * Math.cos(rad);
    html += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="rgba(39, 174, 96, 0.3)" stroke-width="1"/>`;
  }
  // N/S/E/W labels
  const dirs = {N:[cx,cy-R-10], E:[cx+R+12,cy], S:[cx,cy+R+14], W:[cx-R-12,cy]};
  Object.entries(dirs).forEach(([d,[x,y]]) => {
    html += `<text x="${x}" y="${y}" fill="rgba(39, 174, 96, 0.8)" font-size="10" font-weight="bold" font-family="sans-serif" text-anchor="middle" dominant-baseline="middle">${d}</text>`;
  });
  // home dot
  html += `<circle cx="${cx}" cy="${cy}" r="4" fill="#c0392b" opacity=".9"/>`;
  html += `<circle cx="${cx}" cy="${cy}" r="8" fill="none" stroke="#c0392b" stroke-width="1" opacity=".4"/>`;

  svg.innerHTML = html;

  if (typeof L === 'undefined') {
    const radarMap = document.getElementById('radar-map');
    if (radarMap && !radarMap.dataset.fallbackShown) {
      radarMap.dataset.fallbackShown = '1';
      radarMap.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:rgba(232,232,232,.7);font-size:.85rem;text-align:center;padding:1rem">Radar map unavailable offline.<br>SVG radar remains active.</div>';
    }
  }

  // Update Leaflet plane markers
  if (_map) {
    const activeCallsigns = new Set();

    (flights || []).forEach(f => {
      if (!f.lat || !f.lon) return;
      activeCallsigns.add(f.callsign);

      const col = f.on_ground ? '#7f8c8d' : (f.vrate > 100 ? '#00b4d8' : f.vrate < -100 ? '#f3722c' : '#2ecc71');
      const icon = getPlaneIcon(f.track_deg || 0, col, f.callsign || 'UNK');

      if (_planeMarkers[f.callsign]) {
        _planeMarkers[f.callsign].setLatLng([f.lat, f.lon]);
        _planeMarkers[f.callsign].setIcon(icon);
      } else {
        const marker = L.marker([f.lat, f.lon], { icon: icon }).addTo(_map);
        
        const popupContent = `
          <div style="font-family:sans-serif; font-size:11px; color:#ddd; line-height:1.4;">
            <b style="color:#fff; font-size:12px;">${f.callsign || 'Unknown'}</b><br>
            <span style="color:#aaa;">${f.airline || 'Unknown Airline'}</span><br>
            <span style="font-family:monospace; background:#333; padding:1px 3px; border-radius:2px; font-size:9px; color:#fff;">${f.type || 'Type?'}</span><br>
            ${f.from_code ? `<b>${f.from_code}</b> ➔ <b>${f.to_code || '?'}</b>` : 'No Route Info'}<br>
            Alt: ${f.alt_ft ? f.alt_ft.toLocaleString() + ' ft' : '?'}<br>
            Spd: ${f.spd_kt ? f.spd_kt + ' kt' : '?'}<br>
            <button onclick="trackFlightFromMap('${f.callsign}')" style="margin-top:6px; width:100%; border:none; background:#c0392b; color:#fff; padding:3px; border-radius:3px; font-weight:bold; cursor:pointer;">Track Flight</button>
          </div>
        `;
        marker.bindPopup(popupContent, { minWidth: 100, autoPan: false, offset: [0, -5] });
        _planeMarkers[f.callsign] = marker;
      }
    });

    // Remove old plane markers
    Object.keys(_planeMarkers).forEach(cs => {
      if (!activeCallsigns.has(cs)) {
        _map.removeLayer(_planeMarkers[cs]);
        delete _planeMarkers[cs];
      }
    });
  }
}

// ── nearby table ─────────────────────────────────────────────────────────────
let _nearby = [];

function sortBy(key) {
  if (_sortKey === key) _sortAsc = !_sortAsc;
  else { _sortKey = key; _sortAsc = true; }
  document.querySelectorAll('thead th').forEach(th => {
    th.classList.remove('sorted');
    if (th.getAttribute('onclick') === `sortBy('${key}')`) th.classList.add('sorted');
  });
  renderNearby(_s.current && _s.current.flight);
}

function applyFilter() {
  _filter = document.getElementById('filter-input').value.toLowerCase();
  renderNearby(_s.current && _s.current.flight);
}

function phaseBadge(vs, gnd) {
  if (gnd) return '<span class="badge badge-gnd">GND</span>';
  if (vs > 100) return '<span class="badge badge-climb">↑ CLIMB</span>';
  if (vs < -100) return '<span class="badge badge-descend">↓ DESC</span>';
  return '<span class="badge badge-cruise">— CRUISE</span>';
}

function logoImg(code, size = 28) {
  if (!code) return '<span class="logo-placeholder"></span>';
  return `<img src="/logos/${code}.png" height="${size}" alt="${code}"
               onerror="this.style.display='none'" loading="lazy">`;
}

function renderNearby(currentCs) {
  const tbody = document.getElementById('nearby-body');
  let rows = [..._nearby];
  if (_filter) {
    rows = rows.filter(f =>
      (f.callsign||'').toLowerCase().includes(_filter) ||
      (f.airline||'').toLowerCase().includes(_filter) ||
      (f.type||'').toLowerCase().includes(_filter)
    );
  }
  const mul = _sortAsc ? 1 : -1;
  rows.sort((a,b) => {
    const av = a[_sortKey] ?? (typeof a[_sortKey] === 'string' ? '' : Infinity);
    const bv = b[_sortKey] ?? (typeof b[_sortKey] === 'string' ? '' : Infinity);
    return (av < bv ? -1 : av > bv ? 1 : 0) * mul;
  });

  drawRadar(rows);

  document.getElementById('filter-count').textContent =
    _filter ? `${rows.length} / ${_nearby.length} flights` : '';
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:2rem">
      ${_filter ? 'No matching flights.' : 'No aircraft nearby.'}</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(f => {
    const cur = f.callsign === currentCs;
    return `<tr class="${cur ? 'current-row' : ''}">
      <td class="logo-cell">${logoImg(f.airline_code)}</td>
      <td><span class="cs">${f.callsign||'--'}</span>
          ${cur ? ' <span style="color:var(--red);font-size:.7rem">●</span>' : ''}</td>
      <td>${f.airline||'--'}</td>
      <td class="type-cell">${f.type||'--'}</td>
      <td>${f.alt_ft ? f.alt_ft.toLocaleString()+' ft' : '--'}</td>
      <td>${f.spd_kt ? f.spd_kt+' kt' : '--'}</td>
      <td>${f.dist_km ? f.dist_km.toFixed(0)+' km' : '--'}</td>
      <td>${phaseBadge(f.vrate||0, f.on_ground)}</td>
    </tr>`;
  }).join('');
}

// ── stats bar ────────────────────────────────────────────────────────────────
function renderStats(nearby, stats) {
  const set = (id, v) => { const el = document.getElementById(id); if(el) el.textContent = v||'--'; };
  set('st-count', nearby ? nearby.length : '--');
  if (stats) {
    set('st-seen', stats.seen_1h);
    set('st-alt',  stats.max_alt_ft ? stats.max_alt_ft.toLocaleString()+' ft' : '--');
    set('st-spd',  stats.max_spd_kt ? stats.max_spd_kt+' kt' : '--');
    set('st-top',  stats.top_airline || '--');
  }
}

// ── now showing ──────────────────────────────────────────────────────────────
function renderShowing(cur) {
  const el = document.getElementById('showing-content');
  if (!cur) {
    el.innerHTML = '<p style="text-align:center;color:var(--muted);padding:3rem">Nothing on screen.</p>';
    return;
  }
  const from = cur.from_code || '--', to = cur.to_code || '--';
  el.innerHTML = `
    <div class="card">
      <div class="card-header">
        <div class="card-logo">${logoImg(cur.airline_code, 48)}</div>
        <div>
          <div class="flight-num">${cur.flight||'--'}</div>
          <div class="airline-name">${cur.airline||'General Aviation'}</div>
          ${cur.reg ? `<span class="reg-badge">${cur.reg}</span>` : ''}
        </div>
      </div>
      <div class="route">
        <span>${from}</span>
        <span class="arrow">→</span>
        <span>${to}</span>
      </div>
      <div class="city-row">
        ${[cur.from_city, cur.to_city].filter(Boolean).join(' → ')}
      </div>
      <div class="meta">
        <div class="meta-item"><label>Type</label><span>${cur.type||'--'}</span></div>
        <div class="meta-item"><label>Altitude</label>
          <span>${cur.alt_ft ? cur.alt_ft.toLocaleString()+' ft' : '--'}</span></div>
        <div class="meta-item"><label>Speed</label>
          <span>${cur.spd_kt ? cur.spd_kt+' kt' : '--'}</span></div>
        <div class="meta-item"><label>Track</label>
          <span>${cur.track_deg != null ? cur.track_deg+'°' : '--'}</span></div>
        <div class="meta-item"><label>Distance</label>
          <span>${cur.dist_km ? cur.dist_km.toFixed(0)+' km' : '--'}</span></div>
        <div class="meta-item"><label>Phase</label>
          <span>${phaseBadge(cur.vrate||0, cur.on_ground)}</span></div>
      </div>
    </div>`;
}

// ── track status ─────────────────────────────────────────────────────────────
let _prevTrackQuery = null;

function renderTrack(track) {
  const el      = document.getElementById('track-content');
  const noTrack = document.getElementById('no-track');
  if (!el || !noTrack) return;   // DOM not ready yet
  if (!track || !track.query) {
    noTrack.style.display = 'block';
    el.innerHTML = '';
    if (_prevTrackQuery) toast('Tracking stopped.');
    _prevTrackQuery = null;
    return;
  }
  if (track.query !== _prevTrackQuery) {
    toast(`Now tracking ${track.query.toUpperCase()} ✈`);
    _prevTrackQuery = track.query;
  }
  noTrack.style.display = 'none';

  const s    = track.sched || {};
  const frac = Math.min(1, Math.max(0, track.frac || 0));
  const pct  = (frac * 100).toFixed(1);
  const dep  = s.dep_sched || '--';
  const arr  = s.arr_sched || '--';
  const delay = s.delay_min;

  let delayHtml = '';
  if (delay != null) {
    if      (delay >= 5)  delayHtml = `<span class="delay-badge delay-pos">+${delay} min</span>`;
    else if (delay <= -2) delayHtml = `<span class="delay-badge delay-neg">${-delay} min early</span>`;
    else                  delayHtml = `<span class="delay-badge delay-zero">On time</span>`;
  }

  const modeClass = track.mode === 'track' ? 'status-enroute'
                  : track.mode === 'landed' ? 'status-landed' : 'status-awaiting';
  const modeLabel = track.mode === 'track' ? 'En Route'
                  : track.mode === 'landed' ? 'Landed' : 'Awaiting';

  el.innerHTML = `
    <div class="status-box">
      <div class="track-header">
        <span class="track-flight">${track.query.toUpperCase()}</span>
        <span class="status-label ${modeClass}">${modeLabel}</span>
        ${delayHtml}
      </div>
      <div class="route-line">
        <span>${s.dep_iata||'?'}</span>
        <span class="arrow">→</span>
        <span>${s.arr_iata||'?'}</span>
      </div>
      <div class="progress-wrap">
        <div style="position:relative;padding:0 2rem">
          <div class="progress-track">
            <div class="progress-fill" style="width:${pct}%"></div>
            <span class="progress-plane" style="left:${pct}%">✈</span>
          </div>
          <span class="progress-airport dep">${s.dep_iata||'?'}</span>
          <span class="progress-airport arr">${s.arr_iata||'?'}</span>
        </div>
      </div>
      <div class="times">
        <div class="time-col">
          <label>Departure</label>
          <div class="t">${dep}</div>
          ${s.dep_actual && s.dep_actual !== dep ? `<div class="revised">→ ${s.dep_actual}</div>` : ''}
        </div>
        <div class="time-col right">
          <label>Arrival</label>
          <div class="t">${arr}</div>
          ${s.arr_estimated && s.arr_estimated !== arr ? `<div class="revised">→ ${s.arr_estimated}</div>` : ''}
        </div>
      </div>
      ${track.eta_line ? `<div class="eta-line">${track.eta_line}</div>` : ''}
    </div>`;
}

// ── weather tab ──────────────────────────────────────────────────────────────
function renderWeather(wx, health) {
  const el = document.getElementById('weather-content');
  if (!wx || !Object.keys(wx).length) {
    el.innerHTML = '<p style="text-align:center;color:var(--muted);padding:2rem">No weather data.</p>';
    return;
  }
  const t = wx.temperature, w = wx.windspeed, wd = wx.winddirection, code = wx.weathercode;
  const dirs = ['N','NE','E','SE','S','SW','W','NW'];
  const wdir = wd != null ? dirs[Math.round(wd/45) % 8] : '';
  const uptime = health && health.uptime_s != null ? formatUptime(health.uptime_s) : '--';
  const displayMode = health && health.display_mode ? health.display_mode : '--';
  const bindHost = health && health.bind_host ? health.bind_host : '--';
  const port = health && health.control_port ? health.control_port : '8080';
  const inkyState = health && health.inky_available ? 'Connected' : 'Simulation';
  const hostLine = health && health.hostname ? health.hostname : '--';
  const ipLine = health && health.ip_address ? health.ip_address : '--';
  const cpuLine = health && health.cpu_temp_c ? health.cpu_temp_c : '--';
  const loadLine = health && health.load_avg && health.load_avg['1m'] != null ? `${health.load_avg['1m']}` : '--';

  el.innerHTML = `<div class="sys-grid">
    <div class="sys-card">
      <div class="sys-label">Dashboard</div>
      <div class="sys-val">${bindHost}:${port}</div>
      <div class="sys-sub">Open from any device on the LAN</div>
    </div>
    <div class="sys-card">
      <div class="sys-label">Host / IP</div>
      <div class="sys-val">${hostLine}</div>
      <div class="sys-sub">${ipLine}</div>
    </div>
    <div class="sys-card">
      <div class="sys-label">Uptime</div>
      <div class="sys-val">${uptime}</div>
      <div class="sys-sub">Since FlyInk started</div>
    </div>
    <div class="sys-card">
      <div class="sys-label">Display</div>
      <div class="sys-val">${inkyState}</div>
      <div class="sys-sub">${displayMode}</div>
    </div>
    <div class="sys-card">
      <div class="sys-label">Tracking</div>
      <div class="sys-val">${health && health.track_active ? 'Pinned' : 'Idle'}</div>
      <div class="sys-sub">Live control state</div>
    </div>
    <div class="sys-card">
      <div class="sys-label">CPU / Load</div>
      <div class="sys-val">${cpuLine}</div>
      <div class="sys-sub">1m load ${loadLine}</div>
    </div>
  </div>
  <div class="wx-grid">
    ${t != null ? `<div class="wx-card"><div class="wx-label">Temperature</div>
      <div class="wx-val">${t}°</div></div>` : ''}
    ${w != null ? `<div class="wx-card"><div class="wx-label">Wind</div>
      <div class="wx-val">${w}<small style="font-size:1rem"> mph</small></div>
      <div style="color:var(--muted);font-size:.8rem">${wdir}</div></div>` : ''}
    ${code != null ? `<div class="wx-card"><div class="wx-label">Conditions</div>
      <div class="wx-val" style="font-size:1.5rem">${wxEmoji(code)}</div>
      <div style="color:var(--muted);font-size:.8rem">${wxDesc(code)}</div></div>` : ''}
  </div>`;

  const parts = [];
  if (t != null) parts.push(`${t}°`);
  if (w != null) parts.push(`${w}mph ${wdir}`);
  document.getElementById('wx-temp').textContent = parts[0] || '';
  document.getElementById('wx-wind').textContent = parts[1] || '';
  document.getElementById('wx-cond').textContent = wxEmoji(code);
}

function formatUptime(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return `${hours}h ${mins}m`;
  if (mins > 0) return `${mins}m ${secs}s`;
  return `${secs}s`;
}

function wxEmoji(c) {
  if (c === 0) return '☀️';
  if (c <= 2)  return '🌤️';
  if (c === 3) return '☁️';
  if (c <= 48) return '🌫️';
  if (c <= 67) return '🌧️';
  if (c <= 77) return '❄️';
  if (c <= 82) return '🌦️';
  return '⛈️';
}

function wxDesc(c) {
  const m = {0:'Clear',1:'Mainly clear',2:'Partly cloudy',3:'Overcast',
             45:'Fog',48:'Freezing fog',61:'Light rain',63:'Rain',65:'Heavy rain',
             71:'Light snow',73:'Snow',75:'Heavy snow',80:'Showers',95:'Thunderstorm'};
  return m[c] || `Code ${c}`;
}

// ── data fetch ───────────────────────────────────────────────────────────────
async function fetchAll() {
  try {
    const [stateRes, statsRes, healthRes] = await Promise.all([
      fetch('/api/state'),
      fetch('/api/stats'),
      fetch('/api/health'),
    ]);
    _s = stateRes.ok ? await stateRes.json() : {};
    const stats = statsRes.ok ? await statsRes.json() : {};
    _health = healthRes.ok ? await healthRes.json() : {};

    _nearby = _s.nearby || [];
    const cur = _s.current;

    renderNearby(cur && cur.flight);
    renderShowing(cur);
    renderTrack(_s.track);
    renderStats(_nearby, stats);
    renderWeather(_s.weather || {});
  renderWeather(_s.weather || {}, _health);

    const t = _s.updated_at ? new Date(_s.updated_at).toLocaleTimeString() : '--:--';
    document.getElementById('updated-time').textContent = t;
    _nextRefresh = Date.now() + REFRESH_MS;
  } catch(e) {
    console.warn('fetch error', e);
  }
}

async function trackFlight() {
  const inp = document.getElementById('flight-input');
  const v   = inp.value.trim();
  if (!v) return;
  document.getElementById('btn-track').textContent = 'Tracking…';
  await fetch('/track?flight=' + encodeURIComponent(v));
  inp.value = '';
  document.getElementById('btn-track').textContent = 'Track';
  setTimeout(fetchAll, 600);
}

async function stopTracking() {
  await fetch('/stop');
  setTimeout(fetchAll, 400);
}

fetchAll();
setInterval(fetchAll, REFRESH_MS);
</script>
</body>
</html>"""


# ── HTTP handler ─────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _resp(self, body, ct="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p  = urlparse(self.path)
        qs = parse_qs(p.query)

        if p.path == "/api/state":
            self._resp(self._state_json(), "application/json")

        elif p.path == "/api/stats":
            self._resp(json.dumps(_stats_snapshot()).encode(), "application/json")

        elif p.path == "/api/health":
            self._resp(json.dumps(_health_snapshot()).encode(), "application/json")

        elif p.path.startswith("/logos/"):
            code = p.path[7:].replace(".png", "").upper()
            data = _logo_bytes(code)
            if data:
                self._resp(data, "image/png")
            else:
                self._resp(b"", "image/png", 404)

        elif p.path == "/track":
            fl = (qs.get("flight") or [""])[0].strip()
            if fl:
                norm, iata = normalize_query(fl)
                with TRACK_LOCK:
                    TRACK.update(query=fl, norm=norm, iata=iata, icao24=None, landed_at=None)
                log.info("tracking %s → norm=%s iata=%s", fl, norm, iata)
            self._resp(b'{"ok":true}', "application/json")

        elif p.path == "/stop":
            with TRACK_LOCK:
                TRACK.update(query=None, norm=None, iata=None, icao24=None, landed_at=None)
            log.info("tracking stopped via web")
            self._resp(b'{"ok":true}', "application/json")

        else:
            self._resp(_HTML.encode())

    def _state_json(self):
        with STATE_LOCK:
            nearby  = STATE.get("nearby") or []
            current = STATE.get("current")
            weather = STATE.get("weather") or {}
            upd     = STATE.get("updated_at")

        with TRACK_LOCK:
            track_q = TRACK.get("query")

        payload = {
            "nearby":     nearby,
            "current":    current,
            "weather":    weather,
            "track":      _track_payload(track_q),
            "updated_at": upd,
            "home":       {"lat": HOME_LAT, "lon": HOME_LON},
        }
        return json.dumps(payload, default=str).encode()


def _health_snapshot():
  with TRACK_LOCK:
    track_active = bool(TRACK.get("norm"))

  host, ip = get_net()
  cpu_c = cpu_temp()
  disk = shutil.disk_usage(os.getcwd())

  load_1m = load_5m = load_15m = None
  try:
    load_1m, load_5m, load_15m = [round(v, 2) for v in os.getloadavg()]
  except Exception:
    pass

  mem_total_mb = mem_free_mb = mem_avail_mb = None
  try:
    meminfo = {}
    with open("/proc/meminfo", encoding="utf-8") as handle:
      for line in handle:
        key, value = line.split(":", 1)
        meminfo[key] = int(value.strip().split()[0])
    mem_total_mb = round(meminfo["MemTotal"] / 1024, 0)
    mem_free_mb = round(meminfo.get("MemFree", 0) / 1024, 0)
    mem_avail_mb = round(meminfo.get("MemAvailable", 0) / 1024, 0)
  except Exception:
    pass

  return {
    "started_at": datetime.utcfromtimestamp(SERVER_STARTED_AT).isoformat() + "Z",
    "uptime_s": int(time.time() - SERVER_STARTED_AT),
    "control_port": CONTROL_PORT,
    "bind_host": "0.0.0.0",
    "hostname": host,
    "ip_address": ip,
    "inky_available": INKY_AVAILABLE,
    "display_mode": "hardware" if INKY_AVAILABLE else "simulation",
    "track_active": track_active,
    "platform": platform.platform(),
    "python_version": platform.python_version(),
    "cpu_temp_c": cpu_c,
    "load_avg": {
      "1m": load_1m,
      "5m": load_5m,
      "15m": load_15m,
    },
    "memory": {
      "total_mb": mem_total_mb,
      "free_mb": mem_free_mb,
      "available_mb": mem_avail_mb,
    },
    "disk": {
      "path": os.getcwd(),
      "free_gb": round(disk.free / (1024 ** 3), 2),
      "used_pct": round((disk.used / disk.total) * 100, 1) if disk.total else None,
    },
  }


class _ReuseAddrHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


# ── startup ───────────────────────────────────────────────────────────────────

def start_control_server():
    try:
        srv = _ReuseAddrHTTPServer(("0.0.0.0", CONTROL_PORT), _Handler)
        t   = threading.Thread(target=srv.serve_forever, daemon=True, name="web-srv")
        t.start()
        log.info("dashboard → http://0.0.0.0:%d/", CONTROL_PORT)
    except Exception as e:
        log.error("web server failed: %s", e)
