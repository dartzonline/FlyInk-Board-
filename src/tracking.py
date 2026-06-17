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
    OPENSKY_STATES_URL,
)
from src.flights import (
    haversine, bearing, compass,
    enrich, classify_kind, fetch_route, _airport_obj,
    get_opensky_token,
)

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


# ---------------------------------------------------------------------------
# AirLabs — live status + schedule for a pinned flight
# ---------------------------------------------------------------------------

_sched_cache: dict = {}


def fetch_airlabs_status(iata_flight: str) -> dict:
    """
    Hit the AirLabs /flight endpoint for a single flight number.
    Returns a dict with: status, dep_iata, arr_iata,
    dep_sched, dep_actual, arr_sched, arr_estimated, delay_min.
    """
    if not (AIRLABS_KEY and iata_flight):
        return {}

    hit = _sched_cache.get(iata_flight)
    if hit and time.time() - hit[0] < 60:   # cache for 60 s
        return hit[1]

    result = {}
    try:
        r = requests.get(AIRLABS_FLIGHT_URL, params={
            "api_key":   AIRLABS_KEY,
            "flight_iata": iata_flight.upper(),
        }, timeout=12)
        if r.ok:
            data = (r.json().get("response") or {})
            dep_sched  = data.get("dep_time")        # "HH:MM"
            dep_actual = data.get("dep_actual")      # actual departure
            arr_sched  = data.get("arr_time")        # scheduled arrival
            arr_est    = data.get("arr_estimated") or data.get("arr_actual")
            delay      = data.get("delayed")         # minutes or None

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
            }
        else:
            logger.warning("AirLabs status non-OK: %s %s", r.status_code, r.text[:120])
    except Exception as e:
        logger.debug("AirLabs fetch error: %s", e)

    _sched_cache[iata_flight] = (time.time(), result)
    return result


# ---------------------------------------------------------------------------
# OpenSky — locate a single aircraft by icao24 or callsign
# ---------------------------------------------------------------------------

def fetch_one_state(icao24: str):
    if not icao24:
        return None
    tok = get_opensky_token()
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    try:
        r = requests.get(OPENSKY_STATES_URL, params={"icao24": icao24.lower()},
                         headers=headers, timeout=20)
        if r.ok:
            sts = r.json().get("states") or []
            return sts[0] if sts else None
    except Exception as e:
        logger.error("State-by-icao24 error: %s", e)
    return None


def find_icao24_by_callsign(norm: str) -> str | None:
    """Global OpenSky search — only called once when tracking starts."""
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

    # --- AirLabs status (uses IATA number if we have one) --------------------
    sched = fetch_airlabs_status(iata) if iata else {}

    # Try to resolve icao24 from AirLabs position data or by callsign scan
    if not icao:
        if sched.get("aircraft_icao"):
            icao = sched["aircraft_icao"].lower()
        else:
            icao = find_icao24_by_callsign(norm)

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
