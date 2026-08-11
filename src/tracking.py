"""
tracking.py — Pinned-flight state, AirLabs schedule/status, and the tracking
              Inky screen (draw_tracking, draw_progress).

The TRACK dict is the single source of truth for which flight (if any) is
currently pinned.  It is read by main.py's loop and written by web.py's
HTTP handler.  All access is protected by TRACK_LOCK.
"""
import math
import time
import logging
import threading
from datetime import datetime, timedelta

import requests

from src.config import (
    AIRLABS_KEY, AIRLABS_FLIGHT_URL,
    HOME_LAT, HOME_LON,
    TRACK_LINGER_S, IATA_TO_ICAO,
    OPENSKY_STATES_URL, OPENSKY_FLIGHTS_URL,
)
from src.flights import (
    haversine, bearing, compass,
    enrich, classify_kind, fetch_route, _airport_obj,
    get_opensky_token,
)
from src import flight_sources

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared pinned-flight state
# ---------------------------------------------------------------------------
TRACK_LOCK = threading.Lock()
TRACK: dict = {
    "query":     None,   # raw string the user typed
    "norm":      None,   # normalised ICAO callsign (e.g. "AAL1234")
    "iata":      None,   # IATA flight number (e.g. "AA1234") for AirLabs
    "icao24":    None,   # hex Mode-S address once resolved
    "landed_at": None,   # timestamp when landing detected
}


def normalize_query(q: str) -> tuple[str, str]:
    """
    Returns (icao_callsign, iata_number).
    e.g. 'DL 123'  -> ('DAL123', 'DL123')
         'DAL123'  -> ('DAL123', '')
         'AA1234'  -> ('AAL1234', 'AA1234')   (IATA prefix detected)
    """
    q = (q or "").strip().upper().replace(" ", "")
    if not q:
        return "", ""

    # Already looks like an ICAO callsign (3-letter alpha prefix)
    if len(q) >= 4 and q[:3].isalpha() and q[3].isdigit():
        # Could still be IATA if prefix is 2-char  (e.g. "AA1234" starts with "AA")
        if q[:2] in IATA_TO_ICAO and not (q[:3] in {v[:3] for v in IATA_TO_ICAO.values()}):
            icao = IATA_TO_ICAO[q[:2]] + q[2:]
            return icao, q
        return q, ""

    # 2-char IATA prefix
    if len(q) >= 3 and q[:2] in IATA_TO_ICAO:
        icao = IATA_TO_ICAO[q[:2]] + q[2:]
        return icao, q

    return q, ""


def resolve_track_route(cs, sched):
    """(origin, dest) airport dicts for the pinned flight, with coordinates.

    adsbdb's route (fetch_route) only covers the flight's scheduled endpoints
    and _airport_obj already gives it coordinates when known; but AIRPORTS
    covers only a handful of fields near home, so a foreign or long-haul
    endpoint outside that table had a code with no lat/lon and nothing to
    draw. This fills either side in from adsb.lol's worldwide route database.
    """
    fr = fetch_route(cs)
    o = _airport_obj(fr.get("origin")) if fr else None
    dst = _airport_obj(fr.get("destination")) if fr else None

    o_code = sched.get("dep_iata") or (o or {}).get("code")
    d_code = sched.get("arr_iata") or (dst or {}).get("code")
    need_geo = (o and o.get("lat") is None) or (dst and dst.get("lat") is None) or not (o and dst)
    if need_geo:
        geo = flight_sources.route_with_coordinates(cs)
        if geo:
            geo_o, geo_d = geo
            if not o or o.get("lat") is None:
                if not o_code or o_code == geo_o.get("code"):
                    o = geo_o
                elif o_code == geo_d.get("code"):
                    o = geo_d
            if not dst or dst.get("lat") is None:
                if not d_code or d_code == geo_d.get("code"):
                    dst = geo_d
                elif d_code == geo_o.get("code"):
                    dst = geo_o
    return o, dst


# ---------------------------------------------------------------------------
# Route progress — shared by the Inky tracking screen and the web track tab
# ---------------------------------------------------------------------------

def route_progress(mode, state, o, dst, lat, lon):
    """
    Return (frac, rem_km): how far along the route the aircraft is (0..1) and
    the remaining distance to the destination in km.

    Both are derived from the same geometry so the progress-bar icon position
    and the "MIN LEFT"/ETA always agree. Uses the plane's distance to *both*
    endpoints (d_from_origin / (d_from_origin + d_to_dest)) so an off-corridor
    position is handled gracefully. If the two leg distances sum to far more
    than the published route length the adsbdb route is stale (callsign reused
    on a different leg), so we report (0.0, None) rather than a misleading bar.
    """
    if mode == "landed":
        return 1.0, 0.0
    if not (state and o and dst and o.get("lat") is not None
            and dst.get("lat") is not None and lat is not None):
        return 0.0, None

    route = haversine(o["lat"], o["lon"], dst["lat"], dst["lon"])
    d_from = haversine(o["lat"], o["lon"], lat, lon)
    d_to   = haversine(lat, lon, dst["lat"], dst["lon"])
    if route <= 1:
        return 0.0, None
    # Staleness guard: on a sane route the legs sum to roughly the route length.
    if (d_from + d_to) > route * 1.6 + 60:
        return 0.0, None

    frac = max(0.0, min(1.0, d_from / (d_from + d_to)))
    return frac, d_to


# ---------------------------------------------------------------------------
# AirLabs — live status + schedule for a pinned flight
# ---------------------------------------------------------------------------

_sched_cache: dict = {}


def _hhmm(t):
    """AirLabs returns 'YYYY-MM-DD HH:MM' (or just 'HH:MM'). Trim to HH:MM."""
    if not t:
        return None
    s = str(t).strip()
    if len(s) >= 16 and s[10] == " ":
        return s[11:16]
    return s[:5] if len(s) >= 5 else s


def fetch_airlabs_status(iata_flight: str) -> dict:
    """
    Hit the AirLabs /flight endpoint for a single flight number.
    Returns a dict with: status, dep_iata, arr_iata,
    dep_sched, dep_actual, arr_sched, arr_estimated, delay_min.

    Guarded by the daily budget so a monthly quota cannot be silently drained
    again — it already had been, with nothing anywhere noticing.
    """
    if not (AIRLABS_KEY and iata_flight):
        return {}
    if not flight_sources.budget.allows("airlabs"):
        logger.debug("AirLabs daily budget spent; skipping call for %s.", iata_flight)
        return {}

    hit = _sched_cache.get(iata_flight)
    if hit and time.time() - hit[0] < 60:   # cache for 60 s
        return hit[1]

    flight_sources.budget.spend("airlabs")
    result = {}
    try:
        r = requests.get(AIRLABS_FLIGHT_URL, params={
            "api_key":   AIRLABS_KEY,
            "flight_iata": iata_flight.upper(),
        }, timeout=12)
        payload = r.json() if r.ok else {}
        # AirLabs returns HTTP 200 with an {"error": {...}} body on quota
        # exhaustion rather than a 4xx/5xx — checking only r.ok let this pass
        # through as a "successful" empty result instead of a visible failure.
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            logger.warning("AirLabs error: %s", payload["error"].get("message") or payload["error"])
        elif r.ok:
            data = payload.get("response") or {}
            dep_sched  = _hhmm(data.get("dep_time"))        # scheduled departure
            dep_actual = _hhmm(data.get("dep_actual"))      # actual departure
            arr_sched  = _hhmm(data.get("arr_time"))        # scheduled arrival
            arr_est    = _hhmm(data.get("arr_estimated") or data.get("arr_actual"))
            delay      = data.get("delayed")                # minutes or None

            result = {
                "status":        data.get("status", ""),
                "dep_iata":      data.get("dep_iata", ""),
                "arr_iata":      data.get("arr_iata", ""),
                "dep_sched":     dep_sched,
                "dep_actual":    dep_actual,
                "arr_sched":     arr_sched,
                "arr_estimated": arr_est,
                "delay_min":     int(delay) if delay else None,
                "aircraft_icao": data.get("aircraft_icao", ""),
                "reg":           data.get("reg_number", ""),
                "lat":           data.get("lat"),
                "lng":           data.get("lng"),
                "alt":           data.get("alt"),
                "speed":         data.get("speed"),
                "dir":           data.get("dir"),
                "source":        "airlabs",
            }
        else:
            logger.warning("AirLabs status non-OK: %s %s", r.status_code, r.text[:120])
    except Exception as e:
        logger.debug("AirLabs fetch error: %s", e)

    _sched_cache[iata_flight] = (time.time(), result)
    return result


# key: iata number -> (fetched_at, schedule dict). A schedule changes on the
# order of minutes, not seconds, so this cache is what keeps the metered/
# scraped sources to a trickle even while the tracking screen polls every
# main-loop iteration.
_resolved_sched_cache: dict = {}
RESOLVED_SCHED_TTL = 300.0
RESOLVED_SCHED_EMPTY_TTL = 120.0


def resolve_schedule(iata_flight: str) -> dict:
    """Schedule for a pinned flight from whichever source can answer.

    AirLabs runs first when it has budget left — it is the documented API.
    FlightStats fills in only the fields AirLabs left empty (or supplies
    everything when AirLabs is unset/out of budget), so losing either source
    degrades the detail on screen rather than emptying it. FlightStats is a
    scrape of an undocumented page and can break without notice, which is why
    it is the fallback and never the only source tried.
    """
    if not iata_flight:
        return {}
    key = iata_flight.strip().upper()
    now = time.time()
    hit = _resolved_sched_cache.get(key)
    if hit:
        ttl = RESOLVED_SCHED_TTL if hit[1] else RESOLVED_SCHED_EMPTY_TTL
        if now - hit[0] < ttl:
            return dict(hit[1])

    airlabs = fetch_airlabs_status(key)
    result = dict(airlabs)

    if flight_sources.budget.allows("flightstats") and not (
        airlabs.get("dep_sched") and airlabs.get("arr_sched")
    ):
        flight_sources.budget.spend("flightstats")
        fs = flight_sources.schedule_from_flightstats(key)
        for field, value in fs.items():
            if value is not None and result.get(field) is None:
                result[field] = value
        if fs and result.get("source") != "airlabs":
            result["source"] = "flightstats"

    _resolved_sched_cache[key] = (now, dict(result))
    return result


# ---------------------------------------------------------------------------
# OpenSky — locate a single aircraft by icao24 or callsign
# ---------------------------------------------------------------------------

def fetch_one_state(icao24: str):
    """Current state vector for one aircraft, trying OpenSky first and the
    keyless community feeds if it comes back empty -- the same rate-limit
    that empties the nearby board (flights.fetch_states) also strands a
    pinned flight in "await" between polls."""
    if not icao24:
        return None
    tok = get_opensky_token()
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    try:
        r = requests.get(OPENSKY_STATES_URL, params={"icao24": icao24.lower()},
                         headers=headers, timeout=20)
        if r.ok:
            sts = r.json().get("states") or []
            if sts:
                return sts[0]
    except Exception as e:
        logger.error("State-by-icao24 error: %s", e)

    row, record, feed = flight_sources.feed_lookup("hex", icao24)
    if row:
        from src.flights import _note_source, _prime_aircraft_cache
        _note_source(feed or "adsb")
        if record:
            _prime_aircraft_cache([record])
        return row
    return None


def find_icao24_by_callsign(norm: str) -> str | None:
    """Resolve a pinned callsign to its icao24 hex.

    Tried against the keyless community feeds first — they answer "which
    aircraft is flying this callsign" directly. The OpenSky fallback below
    has no such endpoint; the only way is downloading every state vector on
    earth and scanning it, which is far more expensive and only reachable at
    all with OpenSky credentials configured.
    """
    row, _, feed = flight_sources.feed_lookup("callsign", norm)
    if row and row[0]:
        from src.flights import _note_source
        _note_source(feed or "adsb")
        return row[0]

    tok = get_opensky_token()
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    try:
        r = requests.get(OPENSKY_STATES_URL, headers=headers, timeout=30)
        if r.ok:
            for s in (r.json().get("states") or []):
                if (s[1] or "").strip().upper() == norm.upper():
                    return s[0]
    except Exception as e:
        logger.error("Callsign search error: %s", e)
    return None


_dep_time_cache: dict = {}


def opensky_dep_arr_times(icao24: str, callsign: str) -> dict:
    """
    Fallback actual departure / arrival times (local HH:MM) from OpenSky flight
    history, used when AirLabs isn't configured. No scheduled times or delay are
    derivable here, so only dep_actual / arr_estimated are returned.
    """
    if not icao24:
        return {}
    hit = _dep_time_cache.get(icao24)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    out = {}
    tok = get_opensky_token()
    if not tok:
        _dep_time_cache[icao24] = (time.time(), out)
        return out
    now = int(time.time())
    try:
        r = requests.get(OPENSKY_FLIGHTS_URL,
                         params={"icao24": icao24.lower(),
                                 "begin": now - 129600, "end": now},
                         headers={"Authorization": f"Bearer {tok}"}, timeout=15)
        if r.ok:
            flights = r.json()
            if isinstance(flights, list) and flights:
                flights.sort(key=lambda f: f.get("lastSeen", 0), reverse=True)
                top = flights[0]
                first_seen = top.get("firstSeen")
                last_seen  = top.get("lastSeen")
                top_cs = (top.get("callsign") or "").strip().upper()
                # Only attribute times if this record is the current flight
                if top_cs == (callsign or "").upper():
                    if first_seen:
                        out["dep_actual"] = datetime.fromtimestamp(
                            first_seen).strftime("%H:%M")
                    # arrival only meaningful once the aircraft is back on ground
                    if last_seen and (now - last_seen) > 60:
                        out["arr_estimated"] = datetime.fromtimestamp(
                            last_seen).strftime("%H:%M")
    except Exception as e:
        logger.debug("OpenSky dep/arr time error: %s", e)
    _dep_time_cache[icao24] = (time.time(), out)
    return out


# ---------------------------------------------------------------------------
# track_context — called every loop iteration when a flight is pinned
# ---------------------------------------------------------------------------

def track_context() -> dict | None:
    """
    Returns a render context dict or None (if no flight is pinned or it is done).

    Modes:
      "track"  — airborne and live
      "landed" — on ground; will auto-stop after TRACK_LINGER_S
      "await"  — pinned but not yet departed / not yet found
    """
    with TRACK_LOCK:
        norm  = TRACK["norm"]
        query = TRACK["query"]
        iata  = TRACK["iata"]
        icao  = TRACK["icao24"]

    if not norm:
        return None

    # If the user pinned an ICAO callsign we have no IATA number for AirLabs.
    # adsbdb's route record carries the IATA callsign (e.g. UAL456 -> UA456),
    # so derive it once. AirLabs is only ever queried for this one pinned flight.
    if not iata and norm:
        fr0 = fetch_route(norm)
        if fr0 and fr0.get("callsign_iata"):
            iata = fr0["callsign_iata"]
            with TRACK_LOCK:
                if TRACK["norm"] == norm:
                    TRACK["iata"] = iata

    # --- Schedule: AirLabs first, FlightStats filling any gap ----------------
    sched = resolve_schedule(iata) if iata else {}
    # A true codeshare flies under the operating airline's own callsign, so
    # nothing in the sky broadcasts the number that was pinned. FlightStats
    # knows the real callsign, which is what makes those flights findable
    # instead of stuck on "await" forever.
    schedule_callsign = sched.pop("_callsign", None)

    # Try to resolve icao24 from AirLabs position data or by callsign lookup
    if not icao:
        if sched.get("aircraft_icao"):
            icao = sched["aircraft_icao"].lower()
        else:
            icao = find_icao24_by_callsign(norm)
            if not icao and schedule_callsign and schedule_callsign != norm:
                icao = find_icao24_by_callsign(schedule_callsign)

    # --- Live OpenSky position -----------------------------------------------
    state = fetch_one_state(icao) if icao else None

    # If AirLabs gave us a live position and OpenSky didn't, build a minimal state
    if state is None and sched.get("lat") and sched.get("lng"):
        # Fake a minimal state vector so the display can still render position info
        state = [
            icao or "", norm, "", None, None,
            sched["lng"], sched["lat"],
            (sched.get("alt") or 0) * 0.3048,   # ft -> m
            False,
            (sched.get("speed") or 0) * 0.514,  # kn -> m/s
            sched.get("dir") or 0,
            None, None, None, None, None, None,
        ]

    with TRACK_LOCK:
        if TRACK["norm"] == norm:
            TRACK["icao24"] = icao

    # AirLabs preferred; backfill actual dep/arr times from OpenSky history when
    # AirLabs is absent or didn't supply them.
    if icao and not (sched.get("dep_actual") and sched.get("arr_estimated")):
        fallback = opensky_dep_arr_times(icao, norm)
        for k, v in fallback.items():
            sched.setdefault(k, v)

    now      = time.time()
    airborne = state is not None and not state[8]
    arrived  = (sched.get("status") or "").lower() in ("landed", "arrived", "en-route")
    on_ground = state is not None and state[8]

    if airborne:
        with TRACK_LOCK:
            TRACK["landed_at"] = None
        return {"mode": "track", "state": state, "sched": sched,
                "norm": norm, "query": query, "iata": iata}

    if on_ground or (sched.get("status") or "").lower() in ("landed", "arrived"):
        with TRACK_LOCK:
            if TRACK["landed_at"] is None:
                TRACK["landed_at"] = now
            landed_at = TRACK["landed_at"]
        if now - landed_at >= TRACK_LINGER_S:
            with TRACK_LOCK:
                TRACK.update(query=None, norm=None, iata=None,
                             icao24=None, landed_at=None)
            logger.info("Tracked flight done; auto-stopping.")
            return None
        return {"mode": "landed", "state": state, "sched": sched,
                "norm": norm, "query": query, "iata": iata,
                "landed_at": landed_at}

    return {"mode": "await", "state": None, "sched": sched,
            "norm": norm, "query": query, "iata": iata}


# ---------------------------------------------------------------------------
# Drawing helpers — used by display.py
# ---------------------------------------------------------------------------

def draw_progress(img, d, x1, x2, y, frac, kind, paste_icon_fn, col_fn):
    """Draw a horizontal flight-progress bar with an aircraft icon on it."""
    from PIL import Image as _Image
    BLACK = col_fn("BLACK")
    RED   = col_fn("RED")
    d.line([x1, y, x2, y], fill=BLACK, width=3)
    px = x1 + (x2 - x1) * max(0.0, min(1.0, frac))
    d.line([x1, y, px, y], fill=RED, width=4)
    for x in (x1, x2):
        d.line([x, y - 7, x, y + 7], fill=BLACK, width=2)
    paste_icon_fn(img, kind, 90, px, y, 40)
