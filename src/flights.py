"""
flights.py — Everything related to fetching, enriching, and geo-resolving flights.

Covers:
  - Geo maths (haversine, bearing, compass)
  - Airport lookup + departure/arrival inference
  - OpenSky token management + state fetching
  - Flight history (OpenSky)
  - Route / aircraft enrichment (adsbdb)
  - Aircraft classification
"""

import math
import time
import logging

import requests

from src.config import (
    HOME_LAT, HOME_LON, SEARCH_RADII,
    OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET,
    OPENSKY_STATES_URL, OPENSKY_FLIGHTS_URL, OPENSKY_TOKEN_URL,
    ADSBDB_CALLSIGN, ADSBDB_AIRCRAFT,
    AIRPORTS, SMALL_FIELDS, AIRLINES,
    DEP_ALT_M, DEP_RADIUS_KM, ARR_ALT_M, ARR_RADIUS_KM,
    NEAR_ENDPOINT_KM, ROUTE_SLACK, ROUTE_PAD_KM,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GEO UTILITIES
# ---------------------------------------------------------------------------

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing(lat1, lon1, lat2, lon2):
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) -
         math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(dlon))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def angle_off(track, brg):
    return abs(((brg - track + 180) % 360) - 180)


_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass(deg):
    return _COMPASS[int((deg + 22.5) % 360 // 45)]


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# AIRPORT HELPERS
# ---------------------------------------------------------------------------

def resolve_airport(icao):
    """ICAO code → airport dict. Returns pretty values for known fields."""
    if not icao:
        return None
    icao = icao.upper()
    if icao in AIRPORTS:
        iata, name, lat, lon = AIRPORTS[icao]
        return {"code": iata, "city": name, "lat": lat, "lon": lon}
    code = icao[1:] if (len(icao) == 4 and icao[0] == "K") else icao
    return {"code": code, "city": None, "lat": None, "lon": None}


def on_corridor(plat, plon, o, d):
    """True if the aircraft is plausibly on the route between airports o and d."""
    if not (o and d and o.get("lat") is not None and d.get("lat") is not None):
        return True
    if plat is None or plon is None:
        return True
    d_od = haversine(o["lat"], o["lon"], d["lat"], d["lon"])
    d_o  = haversine(plat, plon, o["lat"], o["lon"])
    d_d  = haversine(plat, plon, d["lat"], d["lon"])
    if min(d_o, d_d) <= NEAR_ENDPOINT_KM:
        return True
    corridor_limit = d_od * (ROUTE_SLACK * 1.35) + (ROUTE_PAD_KM * 1.5)
    return (d_o + d_d) <= corridor_limit


def _field_match(plat, plon, track, want_toward, max_km, has_airline):
    best, bscore = None, None
    for icao, (iata, name, alat, alon) in AIRPORTS.items():
        dd = haversine(plat, plon, alat, alon)
        if dd > max_km:
            continue
        if track is not None:
            brg = (bearing(plat, plon, alat, alon) if want_toward
                   else bearing(alat, alon, plat, plon))
            if angle_off(track, brg) > 75:
                continue
        score = dd + (120 if (has_airline and iata in SMALL_FIELDS) else 0)
        if bscore is None or score < bscore:
            best = {"code": iata, "city": name, "lat": alat, "lon": alon}
            bscore = score
    return best


def departure_airport(plat, plon, track, vrate, alt, has_airline):
    if vrate is None or vrate < 1.0 or alt is None or alt > DEP_ALT_M or plat is None:
        return None
    return _field_match(plat, plon, track, False, DEP_RADIUS_KM, has_airline)


def arrival_airport(plat, plon, track, vrate, alt, has_airline):
    if vrate is None or vrate > -1.0 or alt is None or alt > ARR_ALT_M or plat is None:
        return None
    return _field_match(plat, plon, track, True, ARR_RADIUS_KM, has_airline)


# ---------------------------------------------------------------------------
# OPENSKY — token + fetch
# ---------------------------------------------------------------------------

_token = {"value": None, "expires": 0.0}


def get_opensky_token():
    if not (OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET):
        return None
    if _token["value"] and time.time() < _token["expires"] - 60:
        return _token["value"]
    try:
        r = requests.post(OPENSKY_TOKEN_URL, data={
            "grant_type":    "client_credentials",
            "client_id":     OPENSKY_CLIENT_ID,
            "client_secret": OPENSKY_CLIENT_SECRET,
        }, timeout=15)
        r.raise_for_status()
        j = r.json()
        _token["value"]   = j["access_token"]
        _token["expires"] = time.time() + j.get("expires_in", 1800)
        logger.info("Refreshed OpenSky access token.")
        return _token["value"]
    except Exception as e:
        logger.error("OpenSky token error: %s", e)
        return None


def _bbox(lat, lon, radius_km):
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.01, math.cos(math.radians(lat))))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


def fetch_states(radius_km):
    lamin, lamax, lomin, lomax = _bbox(HOME_LAT, HOME_LON, radius_km)
    headers = {}
    tok = get_opensky_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        r = requests.get(OPENSKY_STATES_URL,
                         params={"lamin": lamin, "lamax": lamax,
                                 "lomin": lomin, "lomax": lomax},
                         headers=headers, timeout=20)
        r.raise_for_status()
        return r.json().get("states") or []
    except Exception as e:
        logger.error("OpenSky fetch error: %s", e)
        return []


def get_nearby(n=10):
    """Return up to n nearest aircraft states sorted by distance."""
    for radius in SEARCH_RADII:
        valid = []
        for s in fetch_states(radius):
            lon, lat = s[5], s[6]
            if lat is None or lon is None:
                continue
            valid.append((s, haversine(HOME_LAT, HOME_LON, lat, lon)))
        valid.sort(key=lambda x: x[1])
        if valid or radius == SEARCH_RADII[-1]:
            return valid[:n]
    return []


# ---------------------------------------------------------------------------
# OPENSKY — flight history (real departure / last landing)
# ---------------------------------------------------------------------------

_hist_cache: dict = {}


def flight_history(icao24, callsign):
    """Return (origin_icao, dest_icao) from actual recent tracks."""
    if not icao24:
        return None, None
    hit = _hist_cache.get(icao24)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    result = (None, None)
    tok = get_opensky_token()
    if not tok:
        _hist_cache[icao24] = (time.time(), result)
        return result
    now = int(time.time())
    try:
        r = requests.get(OPENSKY_FLIGHTS_URL,
                         params={"icao24": icao24, "begin": now - 129600, "end": now},
                         headers={"Authorization": f"Bearer {tok}"}, timeout=15)
        if r.ok:
            flights = r.json()
            if isinstance(flights, list) and flights:
                flights.sort(key=lambda f: f.get("lastSeen", 0), reverse=True)
                top    = flights[0]
                dep    = top.get("estDepartureAirport")
                arr    = top.get("estArrivalAirport")
                top_cs = (top.get("callsign") or "").strip()
                result = (dep, arr) if top_cs == callsign else (arr or dep, None)
    except Exception as e:
        logger.debug("Flight history error: %s", e)
    _hist_cache[icao24] = (time.time(), result)
    return result


# ---------------------------------------------------------------------------
# ADSBDB — route + aircraft
# ---------------------------------------------------------------------------

_route_cache: dict = {}
_ac_cache: dict    = {}


def airline_of(callsign):
    cs = (callsign or "").strip()
    if len(cs) >= 4 and cs[:3].isalpha() and cs[3].isdigit():
        code = cs[:3].upper()
        return AIRLINES.get(code, code), code
    return None, None


def _airport_obj(o):
    if not o:
        return None
    return {
        "code": o.get("iata_code") or o.get("icao_code"),
        "city": o.get("municipality") or o.get("name"),
        "lat":  _to_float(o.get("latitude")),
        "lon":  _to_float(o.get("longitude")),
    }


def fetch_route(cs):
    cs = (cs or "").strip()
    if not cs:
        return None
    hit = _route_cache.get(cs)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    fr = None
    try:
        r = requests.get(ADSBDB_CALLSIGN.format(cs), timeout=8,
                         headers={"User-Agent": "flyink-board"})
        if r.ok:
            fr = (r.json().get("response") or {}).get("flightroute")
    except Exception as e:
        logger.debug("Route lookup error: %s", e)
    _route_cache[cs] = (time.time(), fr)
    return fr


def fetch_aircraft(icao24):
    if not icao24:
        return None, None
    hit = _ac_cache.get(icao24)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    reg = typ = None
    try:
        r = requests.get(ADSBDB_AIRCRAFT.format(icao24), timeout=8,
                         headers={"User-Agent": "flyink-board"})
        if r.ok:
            ac = (r.json().get("response") or {}).get("aircraft") or {}
            if isinstance(ac, dict):
                reg = ac.get("registration") or None
                typ = ac.get("type") or ac.get("icao_type") or None
    except Exception as e:
        logger.debug("Aircraft lookup error: %s", e)
    _ac_cache[icao24] = (time.time(), (reg, typ))
    return reg, typ


# ---------------------------------------------------------------------------
# CLASSIFICATION
# ---------------------------------------------------------------------------

def classify_kind(type_str, has_airline):
    t = (type_str or "").lower()
    if not t:
        return "jet" if has_airline else "light"
    groups = [
        ("heli",      ["helicopter", "robinson", "sikorsky", "eurocopter", "bell ",
                        "ec135", "ec145", "ec130", "as350", "h125", "h130", "h145",
                        "r44", "r66", "md 500", "aw139", "aw169"]),
        ("heavy",     ["747", "777", "787", "a380", "a350", "a330", "a340", "767",
                        "md-11", "dc-10", "a300", "a310", "il-96"]),
        ("bizjet",    ["citation", "gulfstream", "learjet", "challenger", "global",
                        "falcon", "phenom", "hawker", "legacy", "praetor",
                        "vision jet", "hondajet", "g550", "g650"]),
        ("turboprop", ["king air", "caravan", "cessna 208", "atr ", "atr-",
                        "dash 8", "dhc-8", "q400", "pc-12", "tbm", "pilatus",
                        "saab 340", "beech 1900", "metroliner", "do228",
                        "twin otter", "dhc-6"]),
        ("light",     ["cessna 1", "cessna 2", "piper", "cirrus", "sr20", "sr22",
                        "diamond", "da40", "da42", "pa-", "c172", "c152", "c182",
                        "bonanza", "mooney", "grumman", "tecnam"]),
    ]
    for kind, keys in groups:
        if any(k in t for k in keys):
            return kind
    return "jet"


# ---------------------------------------------------------------------------
# ENRICHMENT
# ---------------------------------------------------------------------------

def enrich(state):
    """Given a raw OpenSky state vector, return an enriched info dict."""
    icao24 = (state[0] or "").strip().lower()
    cs     = (state[1] or "").strip()
    lat, lon, track = state[6], state[5], state[10]
    alt_m  = state[13] if state[13] is not None else state[7]
    vrate  = state[11]

    info = {
        "reg": None, "type": None, "airline": None, "airline_code": None,
        "flight": cs, "from_code": None, "from_city": None,
        "to_code": None, "to_city": None,
    }
    info["airline"], info["airline_code"] = airline_of(cs)
    info["reg"], info["type"] = fetch_aircraft(icao24)

    has_airline = bool(info["airline"])

    # Candidate A: schedule DB (adsbdb), kept only if the plane is on its corridor
    db_origin = db_dest = None
    fr = fetch_route(cs)
    if fr:
        air = fr.get("airline") or {}
        if air.get("name"):
            info["airline"] = air["name"]
        if air.get("icao"):
            info["airline_code"] = air["icao"]
        o = _airport_obj(fr.get("origin"))
        d = _airport_obj(fr.get("destination"))
        if o and d and on_corridor(lat, lon, o, d):
            # Orient by heading: destination = the endpoint we're flying toward
            if (track is not None and o.get("lat") is not None and
                    d.get("lat") is not None):
                ao = angle_off(track, bearing(lat, lon, o["lat"], o["lon"]))
                ad = angle_off(track, bearing(lat, lon, d["lat"], d["lon"]))
                db_origin, db_dest = (d, o) if ao < ad else (o, d)
            else:
                db_origin, db_dest = o, d

    # Candidate B: live motion inference
    dep = departure_airport(lat, lon, track, vrate, alt_m, has_airline)
    arr = arrival_airport(lat, lon, track, vrate, alt_m, has_airline)

    # Candidate C: real flight history (authoritative origin)
    h_dep, h_arr = flight_history(icao24, cs)
    h_origin = resolve_airport(h_dep) if h_dep else None
    h_dest   = resolve_airport(h_arr) if h_arr else None

    # Resolve by priority: live motion > history > corridor-checked schedule DB
    origin = dep or h_origin or db_origin
    dest   = arr or h_dest or db_dest

    # Guard: never show origin == destination
    if origin and dest and origin["code"] == dest["code"]:
        dest = db_dest if (db_dest and db_dest["code"] != origin["code"]) else None

    if origin:
        info["from_code"], info["from_city"] = origin["code"], origin["city"]
    if dest:
        info["to_code"],   info["to_city"]   = dest["code"],   dest["city"]
    return info


# ---------------------------------------------------------------------------
# STATE FORMATTING HELPERS
# ---------------------------------------------------------------------------

def fmt_alt(state):
    if state[8]:
        return "GND"
    a = state[13] if state[13] is not None else state[7]
    return "--" if a is None else f"{a * 3.281:,.0f} ft"


def fmt_spd(state):
    return f"{state[9] * 1.94384:.0f} kt" if state[9] is not None else "--"


def fmt_vs(state):
    if state[11] is None:
        return "--"
    fpm = state[11] * 196.85
    return "level" if abs(fpm) < 100 else f"{fpm:+,.0f}"


def fmt_track(state):
    if state[10] is None:
        return "--"
    return f"{state[10]:.0f}° {compass(state[10])}"
