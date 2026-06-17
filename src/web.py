"""
web.py — HTTP control panel + flight dashboard (port 8080).

Endpoints:
  GET  /              → 3-tab HTML dashboard (SPA, auto-refreshes every 15 s)
  GET  /api/state     → JSON snapshot of shared STATE for JS polling
  GET  /track?flight= → pin a flight (writes to TRACK)
  GET  /stop          → unpin current tracked flight

The dashboard has three tabs:
  1. Nearby Flights  — table of all flights from the last poll
  2. Now Showing     — card for the flight currently on the Inky screen
  3. Track a Flight  — form + live tracking status with progress bar
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime

from src.config import CONTROL_PORT
from src.tracking import TRACK, TRACK_LOCK, normalize_query

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared STATE — written by main loop, read by web handler
# ---------------------------------------------------------------------------
STATE_LOCK = threading.Lock()
STATE: dict = {
    "nearby":     [],    # list of dicts: {callsign, airline, alt_ft, spd_kt, dist_km, heading}
    "current":    None,  # enriched info dict of the flight currently on screen
    "weather":    {},    # last weather fetch
    "updated_at": None,  # ISO timestamp string
}

# ---------------------------------------------------------------------------
# Dashboard HTML (single-file SPA, no external dependencies)
# ---------------------------------------------------------------------------
_DASHBOARD = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>✈ FlyInk Board</title>
<style>
  :root {
    --red:#c0392b; --dark:#111; --mid:#1e1e1e; --card:#252525;
    --border:#333; --text:#eee; --muted:#888; --green:#27ae60;
    --yellow:#f1c40f; --blue:#2980b9;
  }
  * { box-sizing:border-box; margin:0; padding:0 }
  body { background:var(--dark); color:var(--text); font-family:system-ui,sans-serif;
         min-height:100vh }
  header { background:var(--red); padding:1rem 1.5rem; display:flex;
           align-items:center; gap:.75rem }
  header h1 { font-size:1.4rem; letter-spacing:.04em }
  header span { font-size:1.6rem }
  #updated { margin-left:auto; font-size:.8rem; color:rgba(255,255,255,.7) }

  nav { display:flex; background:var(--mid); border-bottom:2px solid var(--border) }
  nav button { flex:1; padding:.75rem; background:none; border:none;
               color:var(--muted); font-size:.95rem; cursor:pointer;
               border-bottom:3px solid transparent; transition:.2s }
  nav button.active { color:var(--text); border-color:var(--red) }
  nav button:hover:not(.active) { color:var(--text) }

  .tab { display:none; padding:1.5rem; max-width:900px; margin:0 auto }
  .tab.active { display:block }

  /* --- Nearby table --- */
  table { width:100%; border-collapse:collapse; font-size:.9rem }
  th { text-align:left; padding:.5rem .75rem; color:var(--muted);
       border-bottom:1px solid var(--border); font-weight:600; font-size:.8rem;
       letter-spacing:.05em; text-transform:uppercase }
  td { padding:.55rem .75rem; border-bottom:1px solid var(--border) }
  tr.current-row td { background:rgba(192,57,43,.15); color:#fff }
  tr:hover td { background:var(--card) }
  .badge { display:inline-block; padding:.15rem .5rem; border-radius:4px;
           font-size:.75rem; font-weight:700; letter-spacing:.04em }
  .badge-climb { background:#1a5276; color:#7fb3d3 }
  .badge-cruise { background:#1e8449; color:#a9dfbf }
  .badge-descend { background:#7e5109; color:#f0b27a }
  .badge-gnd { background:#424242; color:#bbb }

  /* --- Now Showing card --- */
  .card { background:var(--card); border:1px solid var(--border);
          border-radius:10px; padding:1.5rem; margin-bottom:1rem }
  .card .route { display:flex; align-items:center; gap:1rem;
                 font-size:2.8rem; font-weight:700; margin:.5rem 0 }
  .card .route .arrow { font-size:1.5rem; color:var(--muted) }
  .card .city { font-size:.85rem; color:var(--muted); margin-bottom:1rem }
  .card .meta { display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr));
                gap:.75rem; margin-top:1rem }
  .card .meta-item label { display:block; font-size:.7rem; color:var(--muted);
                            text-transform:uppercase; letter-spacing:.05em }
  .card .meta-item span  { font-size:1.1rem; font-weight:600 }
  .flight-num { font-size:1.1rem; color:var(--red); font-weight:700 }
  .airline    { font-size:.9rem; color:var(--muted) }

  /* --- Track form --- */
  .track-form { display:flex; gap:.75rem; margin-bottom:1.5rem;
                flex-wrap:wrap }
  .track-form input { flex:1; min-width:180px; padding:.65rem .9rem;
                      background:var(--mid); border:1px solid var(--border);
                      border-radius:8px; color:var(--text); font-size:1rem }
  .track-form button { padding:.65rem 1.4rem; border:none; border-radius:8px;
                       font-size:1rem; cursor:pointer; font-weight:600 }
  .btn-track { background:var(--red); color:#fff }
  .btn-stop  { background:#444; color:#fff }
  .btn-track:hover { background:#a93226 }
  .btn-stop:hover  { background:#555 }

  .status-box { background:var(--card); border:1px solid var(--border);
                border-radius:10px; padding:1.25rem }
  .status-box h3 { margin-bottom:.75rem; font-size:1rem; color:var(--muted);
                   text-transform:uppercase; letter-spacing:.05em }
  .progress-wrap { margin:1rem 0 }
  .progress-track { background:#333; border-radius:6px; height:8px;
                    position:relative; overflow:visible }
  .progress-fill  { background:var(--red); height:8px; border-radius:6px;
                    transition:width .5s }
  .progress-plane { position:absolute; top:50%; transform:translateY(-50%);
                    font-size:1.4rem; transition:left .5s }
  .times { display:grid; grid-template-columns:1fr 1fr; gap:1rem; margin-top:1rem }
  .time-col label { font-size:.7rem; color:var(--muted); text-transform:uppercase;
                    letter-spacing:.05em }
  .time-col .t { font-size:1.3rem; font-weight:700 }
  .time-col .revised { color:var(--red); font-size:.85rem }
  .delay-badge { display:inline-block; padding:.2rem .7rem; border-radius:20px;
                 font-weight:700; font-size:.85rem; margin:.5rem 0 }
  .delay-pos  { background:rgba(192,57,43,.25); color:#e74c3c }
  .delay-neg  { background:rgba(39,174,96,.25);  color:#27ae60 }
  .delay-zero { background:rgba(52,152,219,.25); color:#3498db }
  #no-track   { color:var(--muted); text-align:center; padding:2rem }
  .eta-line   { font-size:.9rem; color:var(--muted); margin-top:.5rem }

  @media(max-width:600px) {
    .card .route { font-size:2rem }
    header h1 { font-size:1.1rem }
  }
</style>
</head>
<body>
<header>
  <span>✈</span>
  <h1>FlyInk Board</h1>
  <div id="updated">Updating…</div>
</header>
<nav>
  <button class="active" onclick="showTab('nearby',this)">Nearby Flights</button>
  <button onclick="showTab('showing',this)">Now Showing</button>
  <button onclick="showTab('track',this)">Track a Flight</button>
</nav>

<!-- TAB 1: Nearby -->
<div class="tab active" id="tab-nearby">
  <table id="nearby-table">
    <thead>
      <tr>
        <th>Flight</th><th>Airline</th><th>Type</th>
        <th>Altitude</th><th>Speed</th><th>Distance</th><th>Phase</th>
      </tr>
    </thead>
    <tbody id="nearby-body">
      <tr><td colspan="7" style="color:var(--muted);text-align:center;padding:2rem">
        Loading…</td></tr>
    </tbody>
  </table>
</div>

<!-- TAB 2: Now Showing -->
<div class="tab" id="tab-showing">
  <div id="showing-content">
    <p style="color:var(--muted);text-align:center;padding:3rem">Loading…</p>
  </div>
</div>

<!-- TAB 3: Track -->
<div class="tab" id="tab-track">
  <div class="track-form">
    <input id="flight-input" type="text" placeholder="Flight number e.g. AA1234 or DAL456"
           autocapitalize="characters" autocomplete="off">
    <button class="btn-track" onclick="trackFlight()">Track</button>
    <button class="btn-stop"  onclick="stopTracking()">Stop</button>
  </div>
  <div id="track-content">
    <p id="no-track">No flight is currently being tracked.</p>
  </div>
</div>

<script>
let _state = {};

function showTab(name, btn) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
}

function phaseBadge(vs, gnd) {
  if (gnd) return '<span class="badge badge-gnd">GND</span>';
  if (vs > 100)  return '<span class="badge badge-climb">CLIMB</span>';
  if (vs < -100) return '<span class="badge badge-descend">DESC</span>';
  return '<span class="badge badge-cruise">CRUISE</span>';
}

function renderNearby(nearby, currentCs) {
  const tbody = document.getElementById('nearby-body');
  if (!nearby || nearby.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:2rem">No flights nearby</td></tr>';
    return;
  }
  tbody.innerHTML = nearby.map(f => {
    const isCurrent = f.callsign === currentCs;
    return `<tr class="${isCurrent ? 'current-row' : ''}">
      <td><strong>${f.callsign || '--'}</strong></td>
      <td>${f.airline || '--'}</td>
      <td style="color:var(--muted);font-size:.85rem">${f.type || '--'}</td>
      <td>${f.alt_ft ? f.alt_ft.toLocaleString() + ' ft' : '--'}</td>
      <td>${f.spd_kt ? f.spd_kt + ' kt' : '--'}</td>
      <td>${f.dist_km ? f.dist_km.toFixed(0) + ' km' : '--'}</td>
      <td>${phaseBadge(f.vrate || 0, f.on_ground)}</td>
    </tr>`;
  }).join('');
}

function renderShowing(cur) {
  const el = document.getElementById('showing-content');
  if (!cur) {
    el.innerHTML = '<p style="color:var(--muted);text-align:center;padding:3rem">Nothing on screen right now.</p>';
    return;
  }
  const from = cur.from_code || '--';
  const to   = cur.to_code   || '--';
  el.innerHTML = `
    <div class="card">
      <div class="flight-num">${cur.flight || '--'}</div>
      <div class="airline">${cur.airline || 'General Aviation'}</div>
      <div class="route">
        <span>${from}</span>
        <span class="arrow">→</span>
        <span>${to}</span>
      </div>
      <div class="city">${cur.from_city || ''} ${cur.from_city && cur.to_city ? '→' : ''} ${cur.to_city || ''}</div>
      <div class="meta">
        <div class="meta-item"><label>Reg</label><span>${cur.reg || '--'}</span></div>
        <div class="meta-item"><label>Type</label><span>${cur.type || '--'}</span></div>
        <div class="meta-item"><label>Altitude</label><span>${cur.alt_ft ? cur.alt_ft.toLocaleString() + ' ft' : '--'}</span></div>
        <div class="meta-item"><label>Speed</label><span>${cur.spd_kt ? cur.spd_kt + ' kt' : '--'}</span></div>
        <div class="meta-item"><label>Track</label><span>${cur.track_deg != null ? cur.track_deg + '°' : '--'}</span></div>
        <div class="meta-item"><label>Distance</label><span>${cur.dist_km ? cur.dist_km.toFixed(0) + ' km' : '--'}</span></div>
      </div>
    </div>`;
}

function renderTrack(track) {
  const el = document.getElementById('track-content');
  const noTrack = document.getElementById('no-track');
  if (!track || !track.query) {
    noTrack.style.display = 'block';
    el.innerHTML = '';
    return;
  }
  noTrack.style.display = 'none';

  const s     = track.sched || {};
  const frac  = Math.min(1, Math.max(0, track.frac || 0));
  const pct   = (frac * 100).toFixed(1);
  const dep   = s.dep_sched || '--';
  const arr   = s.arr_sched || '--';
  const depAct= s.dep_actual   || '';
  const arrEst= s.arr_estimated|| '';
  const delay = s.delay_min;

  let delayHtml = '';
  if (delay != null) {
    if (delay >= 5)       delayHtml = `<span class="delay-badge delay-pos">DELAYED +${delay} MIN</span>`;
    else if (delay <= -2) delayHtml = `<span class="delay-badge delay-neg">${-delay} MIN EARLY</span>`;
    else                  delayHtml = `<span class="delay-badge delay-zero">ON TIME</span>`;
  }

  const statusLabel = (track.mode === 'track'  ? 'EN ROUTE' :
                       track.mode === 'landed' ? 'LANDED' : 'AWAITING').toUpperCase();

  el.innerHTML = `
    <div class="status-box">
      <h3>${track.query} &nbsp; <span style="color:var(--text)">${statusLabel}</span>
          &nbsp; ${delayHtml}</h3>
      <div style="display:flex;gap:2rem;font-size:1.5rem;font-weight:700;margin:.5rem 0">
        <span>${s.dep_iata || '?'}</span>
        <span style="color:var(--muted)">→</span>
        <span>${s.arr_iata || '?'}</span>
      </div>
      <div class="progress-wrap">
        <div style="position:relative;padding:0 .5rem">
          <div class="progress-track">
            <div class="progress-fill" style="width:${pct}%"></div>
            <span class="progress-plane" style="left:calc(${pct}% - .7rem)">✈</span>
          </div>
        </div>
      </div>
      <div class="times">
        <div class="time-col">
          <label>Departure</label>
          <div class="t">${dep}</div>
          ${depAct && depAct !== dep ? `<div class="revised">→ ${depAct}</div>` : ''}
        </div>
        <div class="time-col" style="text-align:right">
          <label>Arrival</label>
          <div class="t">${arr}</div>
          ${arrEst && arrEst !== arr ? `<div class="revised">→ ${arrEst}</div>` : ''}
        </div>
      </div>
      ${track.eta_line ? `<div class="eta-line">${track.eta_line}</div>` : ''}
    </div>`;
}

async function fetchState() {
  try {
    const r = await fetch('/api/state');
    if (!r.ok) return;
    _state = await r.json();
    const cur = _state.current;
    renderNearby(_state.nearby, cur && cur.flight);
    renderShowing(cur);
    renderTrack(_state.track);
    const upd = document.getElementById('updated');
    if (_state.updated_at) upd.textContent = 'Updated ' + new Date(_state.updated_at).toLocaleTimeString();
  } catch(e) { console.warn('State fetch error', e); }
}

async function trackFlight() {
  const v = document.getElementById('flight-input').value.trim();
  if (!v) return;
  await fetch('/track?flight=' + encodeURIComponent(v));
  document.getElementById('flight-input').value = '';
  setTimeout(fetchState, 500);
}

async function stopTracking() {
  await fetch('/stop');
  setTimeout(fetchState, 500);
}

fetchState();
setInterval(fetchState, 15000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # silence access log

    def _send(self, body: bytes, content_type="text/html; charset=utf-8"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        path   = parsed.path

        if path == "/api/state":
            self._send(self._build_json(), "application/json")

        elif path == "/track":
            fl = (qs.get("flight") or [""])[0].strip()
            if fl:
                norm, iata = normalize_query(fl)
                with TRACK_LOCK:
                    TRACK.update(query=fl, norm=norm, iata=iata,
                                 icao24=None, landed_at=None)
                logger.info("Tracking: %s → norm=%s iata=%s", fl, norm, iata)
            self._send(b'{"ok":true}', "application/json")

        elif path == "/stop":
            with TRACK_LOCK:
                TRACK.update(query=None, norm=None, iata=None,
                             icao24=None, landed_at=None)
            logger.info("Tracking stopped via web.")
            self._send(b'{"ok":true}', "application/json")

        else:
            # Serve dashboard for / and anything else
            self._send(_DASHBOARD.encode())

    def _build_json(self) -> bytes:
        """Build the /api/state JSON payload."""
        from src.tracking import TRACK, TRACK_LOCK

        with STATE_LOCK:
            nearby  = STATE.get("nearby")  or []
            current = STATE.get("current")
            upd     = STATE.get("updated_at")

        with TRACK_LOCK:
            track_q    = TRACK.get("query")
            track_norm = TRACK.get("norm")
            track_iata = TRACK.get("iata")
            track_mode = None

        # Compute track frac + eta from current STATE if tracking
        track_payload = None
        if track_q:
            from src import tracking as _trk
            ctx = _trk.track_context()
            if ctx:
                track_mode = ctx["mode"]
                sched = ctx.get("sched") or {}
                state = ctx.get("state")
                frac  = 0.0
                eta_line = ""
                if state:
                    from src.flights import haversine as _hav, fetch_route as _fr, _airport_obj as _ao
                    from src.config import HOME_LAT, HOME_LON
                    cs  = (state[1] or "").strip()
                    fr  = _fr(cs)
                    o   = _ao(fr.get("origin"))      if fr else None
                    dst = _ao(fr.get("destination")) if fr else None
                    lat, lon = state[6], state[5]
                    if o and dst and o.get("lat") and dst.get("lat") and lat:
                        tot = _hav(o["lat"], o["lon"], dst["lat"], dst["lon"])
                        if tot > 1:
                            frac = _hav(o["lat"], o["lon"], lat, lon) / tot
                    if (ctx["mode"] == "track" and dst and dst.get("lat")
                            and state[9] and state[9] > 30):
                        from src.flights import haversine as _hav2
                        rem  = _hav2(lat, lon, dst["lat"], dst["lon"])
                        mins = rem / (state[9] * 3.6) * 60
                        from datetime import datetime, timedelta
                        eta  = (datetime.now() + timedelta(minutes=mins)).strftime(
                            "%I:%M %p").lstrip("0")
                        eta_line = f"ETA ~{eta}  ·  {int(mins)} min left"

                track_payload = {
                    "query":    track_q,
                    "mode":     track_mode,
                    "sched":    sched,
                    "frac":     round(frac, 3),
                    "eta_line": eta_line,
                }

        payload = {
            "nearby":     nearby,
            "current":    current,
            "track":      track_payload,
            "updated_at": upd,
        }
        return json.dumps(payload, default=str).encode()


# ---------------------------------------------------------------------------
# Start server
# ---------------------------------------------------------------------------

def start_control_server():
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), _Handler)
        t   = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        logger.info("Dashboard on http://0.0.0.0:%d/", CONTROL_PORT)
    except Exception as e:
        logger.error("Control server failed: %s", e)
