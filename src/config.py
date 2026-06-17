"""
config.py — All application constants and environment-variable driven settings.
Load this first; every other module imports from here.
"""
import os
from dotenv import load_dotenv

load_dotenv()  # picks up .env for local runs; env vars take precedence on the Pi

# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------
HOME_LAT = float(os.environ.get("HOME_LAT", "30.6333"))
HOME_LON = float(os.environ.get("HOME_LON", "-97.6770"))

# ---------------------------------------------------------------------------
# Display behaviour
# ---------------------------------------------------------------------------
DISPLAY_INTERVAL = int(os.environ.get("DISPLAY_INTERVAL", "150"))  # seconds
SEARCH_RADII     = [120, 300, 700]                                  # km, expanding
ROTATE           = int(os.environ.get("ROTATE", "90"))              # 0/90/180/270
RADAR_RANGE_KM   = 80
TEMP_UNIT        = os.environ.get("TEMP_UNIT", "fahrenheit")        # or "celsius"
WIND_UNIT        = os.environ.get("WIND_UNIT", "mph")               # mph/kmh/ms/kn

# ---------------------------------------------------------------------------
# Flight tracking (pin a specific flight)
# ---------------------------------------------------------------------------
CONTROL_PORT   = int(os.environ.get("CONTROL_PORT", "8080"))
TRACK_LINGER_S = 600   # keep a landed flight on screen for 10 min then auto-stop

# ---------------------------------------------------------------------------
# API credentials
# ---------------------------------------------------------------------------
OPENSKY_CLIENT_ID     = os.environ.get("OPENSKY_CLIENT_ID", "")
OPENSKY_CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET", "")
AIRLABS_KEY           = os.environ.get("AIRLABS_KEY", "")   # free key from airlabs.co

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
OPENSKY_FLIGHTS_URL = "https://opensky-network.org/api/flights/aircraft"
OPENSKY_TOKEN_URL  = (
    "https://auth.opensky-network.org/auth/realms/"
    "opensky-network/protocol/openid-connect/token"
)
AIRLABS_FLIGHT_URL = "https://airlabs.co/api/v9/flight"     # single-flight status
AIRLABS_ROUTES_URL = "https://airlabs.co/api/v9/routes"     # route DB lookup
WEATHER_URL        = "https://api.open-meteo.com/v1/forecast"
ADSBDB_CALLSIGN    = "https://api.adsbdb.com/v0/callsign/{}"
ADSBDB_AIRCRAFT    = "https://api.adsbdb.com/v0/aircraft/{}"

# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------
LOGO_DIR  = os.path.expanduser("~/logos")
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# ---------------------------------------------------------------------------
# Airline / airport data
# ---------------------------------------------------------------------------
AIRLINES = {
    "AAL": "American Airlines",  "UAL": "United Airlines",
    "DAL": "Delta Air Lines",    "SWA": "Southwest Airlines",
    "JBU": "JetBlue",            "ASA": "Alaska Airlines",
    "FFT": "Frontier",           "NKS": "Spirit Airlines",
    "SKW": "SkyWest",            "ENY": "Envoy Air",
    "RPA": "Republic Airways",   "EDV": "Endeavor Air",
    "AAY": "Allegiant Air",      "FDX": "FedEx",
    "UPS": "UPS Airlines",       "EJA": "NetJets",
    "LXJ": "Flexjet",
}

# IATA -> ICAO prefix map so typed "DL 123" -> "DAL123"
IATA_TO_ICAO = {
    "AA": "AAL", "UA": "UAL", "DL": "DAL", "WN": "SWA", "B6": "JBU", "AS": "ASA",
    "F9": "FFT", "NK": "NKS", "G4": "AAY", "HA": "HAL", "AC": "ACA", "WS": "WJA",
    "BA": "BAW", "LH": "DLH", "AF": "AFR", "KL": "KLM", "EK": "UAE", "QR": "QTR",
    "SQ": "SIA", "NH": "ANA", "JL": "JAL", "FR": "RYR", "U2": "EZY", "VS": "VIR",
    "FI": "ICE", "AM": "AMX", "Y4": "VOI", "CX": "CPA", "KE": "KAL", "QF": "QFA",
    "EY": "ETD", "TK": "THY", "IB": "IBE",
}

# ICAO -> (IATA, display name, lat, lon)
AIRPORTS = {
    "KAUS": ("AUS", "Austin-Bergstrom Intl",   30.1945, -97.6699),
    "KGTU": ("GTU", "Georgetown Municipal",     30.6786, -97.6794),
    "KEDC": ("EDC", "Austin Executive",         30.3917, -97.5664),
    "KGRK": ("GRK", "Killeen Regional",         31.0672, -97.8289),
    "KSAT": ("SAT", "San Antonio Intl",         29.5337, -98.4698),
    "KSSF": ("SSF", "Stinson Municipal",        29.3370, -98.4710),
    "KACT": ("ACT", "Waco Regional",            31.6113, -97.2305),
    "KCLL": ("CLL", "College Station",          30.5886, -96.3638),
    "KTPL": ("TPL", "Temple",                   31.1525, -97.4078),
    "KBAZ": ("BAZ", "New Braunfels",            29.7045, -98.0421),
    "KDFW": ("DFW", "Dallas-Fort Worth Intl",   32.8968, -97.0380),
    "KDAL": ("DAL", "Dallas Love Field",        32.8471, -96.8518),
    "KAFW": ("AFW", "Fort Worth Alliance",      32.9876, -97.3188),
    "KHOU": ("HOU", "Houston Hobby",            29.6454, -95.2789),
    "KIAH": ("IAH", "Houston Bush Intl",        29.9902, -95.3368),
    "KBNA": ("BNA", "Nashville Intl",           36.1245, -86.6782),
    "KATL": ("ATL", "Atlanta Intl",             33.6407, -84.4277),
    "KORD": ("ORD", "Chicago O'Hare",           41.9742, -87.9073),
    "KMDW": ("MDW", "Chicago Midway",           41.7868, -87.7522),
    "KDEN": ("DEN", "Denver Intl",              39.8561, -104.6737),
    "KLAX": ("LAX", "Los Angeles Intl",         33.9416, -118.4085),
    "KPHX": ("PHX", "Phoenix Sky Harbor",       33.4342, -112.0116),
    "KLAS": ("LAS", "Las Vegas Reid",           36.0840, -115.1537),
    "KMCO": ("MCO", "Orlando Intl",             28.4312, -81.3081),
    "KMIA": ("MIA", "Miami Intl",               25.7959, -80.2870),
    "KJFK": ("JFK", "New York JFK",             40.6413, -73.7781),
    "KEWR": ("EWR", "Newark Liberty",           40.6895, -74.1745),
    "KSEA": ("SEA", "Seattle-Tacoma",           47.4502, -122.3088),
    "KSFO": ("SFO", "San Francisco Intl",       37.6213, -122.3790),
    "KMSP": ("MSP", "Minneapolis-St Paul",      44.8848, -93.2223),
}

# Small / GA fields — de-prioritised when an airline callsign is known
SMALL_FIELDS = {"GTU", "EDC", "GRK", "SSF", "ACT", "CLL", "TPL", "BAZ", "AFW"}

# Route-corridor sanity check parameters
DEP_ALT_M        = 7600   # climbing ceiling for departure detection (~25 000 ft)
DEP_RADIUS_KM    = 90
ARR_ALT_M        = 4500   # descending ceiling for arrival detection (~15 000 ft)
ARR_RADIUS_KM    = 70
NEAR_ENDPOINT_KM = 60     # within this of an endpoint → on corridor
ROUTE_SLACK      = 1.5    # corridor width multiplier
ROUTE_PAD_KM     = 120    # flat additional corridor padding
