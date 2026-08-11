"""
flight_sources.py — Keyless fallback feeds, plus the quota discipline for the
                    metered/scraped ones.

Positions used to come from OpenSky alone. Anonymous OpenSky rate-limits
constantly, and AIRLABS_KEY is a monthly quota that a handful of tracked
flights can exhaust outright — once either happens the board or the tracking
screen just goes dark. Three community ADS-B feeds (adsb.lol, adsb.fi,
airplanes.live) need no key at all and are tried whenever OpenSky comes up
empty; adsb.lol's route database also gives worldwide airport coordinates
that the local AIRPORTS table (a handful of Texas-area fields) never had.

Every metered or scraped call spends from a persisted daily Budget so a quota
cannot be silently drained again — the whole reason this module exists is that
AIRLABS_KEY had already hit AirLabs' *monthly* cap with no warning anywhere.

All three feeds document a ~1 request/second limit and ask non-feeders to be
gentle, so `throttle()` spaces calls per host. Nothing here raises: callers
get None/[]/{} on failure and fall through to the next source, matching this
project's existing degrade-to-empty style (see flights.py, tracking.py).
"""

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyless ADS-B feeds
# ---------------------------------------------------------------------------

# (name, point-url template, callsign-url template, hex-url template). Tried in
# this order; the first one to answer with aircraft wins. adsb.fi's point
# endpoint is versioned differently (v3) from its callsign/hex endpoints (v2
# — v3 has neither), hence the mixed versions.
ADSB_FEEDS = [
    (
        "adsb.lol",
        "https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}",
        "https://api.adsb.lol/v2/callsign/{callsign}",
        "https://api.adsb.lol/v2/hex/{hex}",
    ),
    (
        "adsb.fi",
        "https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}",
        "https://opendata.adsb.fi/api/v2/callsign/{callsign}",
        "https://opendata.adsb.fi/api/v2/hex/{hex}",
    ),
    (
        "airplanes.live",
        "https://api.airplanes.live/v2/point/{lat}/{lon}/{nm}",
        "https://api.airplanes.live/v2/callsign/{callsign}",
        "https://api.airplanes.live/v2/hex/{hex}",
    ),
]

MAX_FEED_RADIUS_NM = 250
KM_PER_NM = 1.852

# Documented limit on all three feeds is ~1 request/second; spacing is per
# host so one slow feed cannot stall the others.
MIN_REQUEST_SPACING_S = 1.1

_host_next_allowed = {}
_host_lock = threading.Lock()


def throttle(url):
    """Block until this host's minimum request spacing has elapsed."""
    host = url.split("/")[2]
    with _host_lock:
        wait = _host_next_allowed.get(host, 0.0) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _host_next_allowed[host] = time.monotonic() + MIN_REQUEST_SPACING_S


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# OpenSky state-vector width; every consumer in flights.py/tracking.py already
# indexes state vectors positionally, so converting at the edge lets the
# fallback feeds drop in without touching any of that logic.
STATE_VECTOR_WIDTH = 17


def state_row_from_feed(ac):
    """One community-feed aircraft record -> an OpenSky-shaped state vector."""
    icao24 = (ac.get("hex") or "").strip().lower().lstrip("~")
    if not icao24:
        return None

    # These feeds report a grounded aircraft as the literal string "ground" in
    # place of a numeric altitude.
    raw_baro = ac.get("alt_baro")
    on_ground = isinstance(raw_baro, str) and raw_baro.strip().lower() == "ground"

    def ft_to_m(v):
        n = _num(v)
        return None if (n is None or on_ground) else n / 3.28084

    callsign = ac.get("flight")
    callsign = callsign.strip() if isinstance(callsign, str) and callsign.strip() else None

    row = [None] * STATE_VECTOR_WIDTH
    row[0] = icao24
    row[1] = callsign
    row[5] = _num(ac.get("lon"))
    row[6] = _num(ac.get("lat"))
    row[7] = ft_to_m(raw_baro)
    row[8] = on_ground
    gs = _num(ac.get("gs"))
    row[9] = gs * 0.514444 if gs is not None else None
    row[10] = _num(ac.get("track"))
    rate = ac.get("baro_rate") if ac.get("baro_rate") is not None else ac.get("geom_rate")
    rate = _num(rate)
    row[11] = rate / 196.850394 if rate is not None else None
    row[13] = ft_to_m(ac.get("alt_geom"))
    return row


def aircraft_info_from_feed(ac):
    """(registration, type, reg_country) from a community-feed record, or None.

    The feeds carry the long human-readable `desc` ("BOEING 737 MAX 8") that
    the classifier reads best; the terse ICAO `t` ("B38M") only stands in
    when `desc` is absent. No registration-country field exists here — that
    stays adsbdb-only.
    """
    reg = ac.get("r")
    icao_type = ac.get("t")
    desc = ac.get("desc")
    if not (reg or icao_type or desc):
        return None
    reg = reg.strip() if isinstance(reg, str) else None
    typ = desc.strip() if isinstance(desc, str) and desc.strip() else (icao_type or None)
    return reg, typ, None


def _records(payload):
    if not isinstance(payload, dict):
        return []
    raw = payload.get("ac")
    if raw is None:
        raw = payload.get("aircraft")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _feed_get(url, name, on_error):
    throttle(url)
    try:
        r = requests.get(url, timeout=12, headers={"Accept": "application/json", "User-Agent": "flyink-board"})
        r.raise_for_status()
        return _records(r.json())
    except Exception as e:
        logger.debug("feed fetch failed (%s): %s", url, e)
        if on_error:
            on_error(name, str(e))
        return []


def feed_states_in_radius(lat, lon, radius_km, on_error=None):
    """Aircraft near a point from the first community feed that answers.

    Returns (state rows, raw records, feed name) so the caller can both use
    the positions and harvest the registration/type that came with them.
    `on_error(source, detail)` is called for each feed that fails, so a
    caller can tell "every position source is down" apart from "the sky
    really is quiet right now".
    """
    nm = max(1, min(MAX_FEED_RADIUS_NM, round(radius_km / KM_PER_NM)))
    for name, point_url, _, _ in ADSB_FEEDS:
        records = _feed_get(point_url.format(lat=round(lat, 4), lon=round(lon, 4), nm=nm), name, on_error)
        rows = [row for row in (state_row_from_feed(ac) for ac in records) if row]
        if rows:
            return rows, records, name
    return [], [], None


def feed_lookup(kind, value, on_error=None):
    """One aircraft by callsign or hex, from the first feed that has it.

    The callsign form is what makes this worth having beyond redundancy:
    resolving a pinned flight against OpenSky means downloading the entire
    planet's state vector and scanning it (see tracking.find_icao24_by_callsign);
    these feeds answer a callsign directly.
    """
    for name, _, callsign_url, hex_url in ADSB_FEEDS:
        template = callsign_url if kind == "callsign" else hex_url
        if not template:
            continue
        url = template.format(callsign=value.upper(), hex=value.lower())
        for ac in _feed_get(url, name, on_error):
            row = state_row_from_feed(ac)
            if row:
                return row, ac, name
    return None, None, None


# ---------------------------------------------------------------------------
# Worldwide route lookup (keyless)
# ---------------------------------------------------------------------------

ADSB_LOL_ROUTE_URL = "https://api.adsb.lol/api/0/route/{callsign}"

_route_cache = {}
ROUTE_TTL = 3600.0
ROUTE_MISS_TTL = 900.0


def _route_airport(raw):
    return {
        "code": raw.get("iata") or raw.get("icao"),
        "city": raw.get("location") or raw.get("name"),
        "country": raw.get("countryiso2"),
        "lat": _num(raw.get("lat")),
        "lon": _num(raw.get("lon")),
    }


def pick_leg(stops, lat, lon):
    """Which leg of a multi-stop rotation is this aircraft flying?

    adsb.lol returns the whole day's rotation for a callsign (e.g.
    DFW-HRL-DFW). Only one of those legs is in the air right now; the right
    one is whichever leg the aircraft's position sits closest to, measured as
    the detour through it versus flying it direct. Returns (origin, dest).
    """
    if not stops or len(stops) < 2:
        return None
    if lat is None or lon is None:
        return stops[0], stops[-1]

    best, best_excess = None, None
    for a, b in zip(stops, stops[1:]):
        leg = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
        via = (haversine_km(a["lat"], a["lon"], lat, lon)
               + haversine_km(lat, lon, b["lat"], b["lon"]))
        excess = via - leg
        if best_excess is None or excess < best_excess:
            best, best_excess = (a, b), excess
    return best


def haversine_km(lat1, lon1, lat2, lon2):
    """Local copy so this module stays independent of flights.py (which
    imports it -- taking the dependency the other way would be circular)."""
    import math
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def route_with_coordinates(callsign, lat=None, lon=None):
    """(origin, dest) with real coordinates for any callsign, from adsb.lol.

    AIRPORTS in config.py only covers a handful of fields near home, so a
    long-haul or foreign endpoint had no coordinates to draw a route with.
    This is keyless and worldwide. When the callsign's rotation has more than
    two stops, `lat`/`lon` select the leg actually being flown.
    """
    if not callsign:
        return None
    key = callsign.strip().upper()
    now = time.time()
    hit = _route_cache.get(key)
    if hit and (now - hit[0]) < (ROUTE_TTL if hit[1] else ROUTE_MISS_TTL):
        return pick_leg(hit[1], lat, lon)

    url = ADSB_LOL_ROUTE_URL.format(callsign=key)
    throttle(url)
    stops = None
    try:
        r = requests.get(url, timeout=12, allow_redirects=True,
                         headers={"Accept": "application/json", "User-Agent": "flyink-board"})
        if r.status_code == 200:
            payload = r.json()
            airports = payload.get("_airports") if isinstance(payload, dict) else None
            if isinstance(airports, list) and all(isinstance(a, dict) for a in airports):
                placed = [_route_airport(a) for a in airports]
                placed = [s for s in placed if s.get("lat") is not None]
                if len(placed) >= 2:
                    # Cached as the full rotation (which can be DFW-HRL-DFW);
                    # pick_leg narrows it to the leg being flown, so the same
                    # cache entry stays correct as the aircraft moves.
                    stops = placed
    except Exception as e:
        logger.debug("route lookup failed for %s: %s", key, e)

    _route_cache[key] = (now, stops)
    return pick_leg(stops, lat, lon)


# ---------------------------------------------------------------------------
# Daily budget for metered / scraped sources
# ---------------------------------------------------------------------------

# This project has no writable /data volume convention (that's the HA
# add-on's), so the budget lives beside the code, like _route_cache above,
# except persisted to survive a Pi reboot -- which is exactly when a
# crash-loop would otherwise re-drain a freshly-reset quota.
_BUDGET_PATH = Path(__file__).resolve().parent.parent / "flight_quota.json"

# AirLabs' free tier is monthly and had ALREADY been exhausted once with no
# guard at all; a flat daily allowance leaves headroom instead of letting one
# busy day spend the month.
DEFAULT_DAILY_BUDGETS = {"airlabs": 20, "flightstats": 150}

_budget_lock = threading.Lock()


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


class Budget:
    """A per-UTC-day call allowance, persisted so a reboot cannot reset it."""

    def __init__(self):
        self._counts = {}
        self._day = _today()
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            saved = json.loads(_BUDGET_PATH.read_text())
        except (OSError, ValueError):
            return
        if isinstance(saved, dict) and saved.get("day") == self._day:
            counts = saved.get("counts")
            if isinstance(counts, dict):
                self._counts = {str(k): int(v) for k, v in counts.items() if isinstance(v, int)}

    def _save(self):
        try:
            _BUDGET_PATH.write_text(json.dumps({"day": self._day, "counts": self._counts}))
        except OSError:
            pass

    def _roll(self):
        today = _today()
        if today != self._day:
            self._day = today
            self._counts = {}
            self._save()

    def limit(self, source):
        env = os.environ.get(f"{source.upper()}_DAILY_BUDGET")
        if env:
            try:
                return max(0, int(env))
            except ValueError:
                pass
        return DEFAULT_DAILY_BUDGETS.get(source, 0)

    def used(self, source):
        with _budget_lock:
            self._load()
            self._roll()
            return self._counts.get(source, 0)

    def remaining(self, source):
        return max(0, self.limit(source) - self.used(source))

    def allows(self, source):
        return self.remaining(source) > 0

    def spend(self, source):
        with _budget_lock:
            self._load()
            self._roll()
            self._counts[source] = self._counts.get(source, 0) + 1
            self._save()

    def snapshot(self):
        return {
            source: {"used": self.used(source), "limit": self.limit(source), "remaining": self.remaining(source)}
            for source in DEFAULT_DAILY_BUDGETS
        }


budget = Budget()


# ---------------------------------------------------------------------------
# FlightStats (last-resort schedule fallback, pinned flight only)
# ---------------------------------------------------------------------------

FLIGHTSTATS_URL = "https://www.flightstats.com/v2/flight-tracker/{carrier}/{number}"

# The page ships its state as a JSON blob for client-side hydration; this
# reads that blob rather than parsing rendered markup. Undocumented and
# unversioned, so this source is deliberately last-resort and only used for
# the one pinned flight, never the whole nearby board.
_NEXT_DATA_MARKER = "__NEXT_DATA__ = "

_FLIGHT_QUERY_RE = re.compile(r"^([A-Z]{2,3})\s*(\d{1,4})$")


def split_flight_number(iata_number):
    """"UA100" -> ("UA", "100"). None when it isn't a carrier+number pair."""
    if not iata_number:
        return None
    m = _FLIGHT_QUERY_RE.match(iata_number.strip().upper())
    return (m.group(1), m.group(2)) if m else None


def _extract_next_data(html):
    start = html.find(_NEXT_DATA_MARKER)
    if start == -1:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(html[start + len(_NEXT_DATA_MARKER):])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _time_of(times, key):
    """Bare "HH:MM", matching tracking._hhmm's output shape exactly so a
    FlightStats time and an AirLabs time can be equality-compared (e.g. to
    show only a delta between scheduled and actual) without one carrying a
    timezone suffix the other lacks."""
    entry = (times or {}).get(key) or {}
    time24 = entry.get("time24")
    return str(time24) if time24 else None


def parse_flightstats(payload):
    """Pull the schedule fields this display shows out of the page's hydration
    state. Walked with `or {}` at every level rather than a dict default: a
    JSON `null` anywhere along the path satisfies `.get(key, {})` while still
    being unsubscriptable, and this blob is treated as hostile on principle."""
    flight = payload
    for step in ("props", "initialState", "flightTracker", "flight"):
        flight = (flight or {}).get(step) if isinstance(flight, dict) else None
    if not isinstance(flight, dict):
        return {}

    schedule = flight.get("schedule") or {}
    status = flight.get("status") or {}
    departure = flight.get("departureAirport") or {}
    arrival = flight.get("arrivalAirport") or {}
    delay = status.get("delayStatus") or {}

    dep_times = departure.get("times") or {}
    # An *estimated* departure must never be shown as though it already
    # happened — only surface dep_actual when the source itself labels the
    # estimatedActual entry "Actual".
    dep_actual = (
        _time_of(dep_times, "estimatedActual")
        if (dep_times.get("estimatedActual") or {}).get("title") == "Actual"
        else None
    )

    result = {
        "dep_sched": _time_of(dep_times, "scheduled"),
        "dep_actual": dep_actual,
        "arr_sched": _time_of(arrival.get("times"), "scheduled"),
        "arr_estimated": _time_of(arrival.get("times"), "estimatedActual"),
        "delay_min": delay.get("minutes"),
        "status": (status.get("status") or "").lower() or None,
        "dep_iata": departure.get("iata") or departure.get("fs"),
        "arr_iata": arrival.get("iata") or arrival.get("fs"),
        "source": "flightstats",
    }

    track = (flight.get("positional") or {}).get("flexTrack") or {}
    if track.get("callsign"):
        result["_callsign"] = str(track["callsign"]).strip().upper()
    if track.get("tailNumber"):
        result["reg"] = str(track["tailNumber"]).strip().upper()
    if track.get("lat") is not None and track.get("lon") is not None:
        result["lat"] = _num(track.get("lat"))
        result["lng"] = _num(track.get("lon"))

    origin_ap = departure or {}
    dest_ap = arrival or {}
    if origin_ap.get("iata") and origin_ap.get("city"):
        result["origin"] = {"code": origin_ap.get("iata"), "city": origin_ap.get("city")}
    if dest_ap.get("iata") and dest_ap.get("city"):
        result["dest"] = {"code": dest_ap.get("iata"), "city": dest_ap.get("city")}

    return {k: v for k, v in result.items() if v is not None}


def schedule_from_flightstats(iata_number):
    """Schedule for one pinned flight, scraped as the last resort when AirLabs
    is unset or out of budget. Reads an undocumented internal JSON blob out of
    a public page, so it can break without notice -- kept to a trickle by its
    own budget and the per-host throttle."""
    parts = split_flight_number(iata_number)
    if not parts:
        return {}
    carrier, number = parts
    url = FLIGHTSTATS_URL.format(carrier=carrier, number=number)
    throttle(url)
    try:
        r = requests.get(
            url, timeout=15, allow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        r.raise_for_status()
    except requests.RequestException as e:
        logger.debug("flightstats fetch failed: %s", e)
        return {}

    payload = _extract_next_data(r.text)
    if payload is None:
        logger.debug("flightstats page carried no readable flight data")
        return {}
    return parse_flightstats(payload)
