"""
weather.py — Fetches current weather from Open-Meteo and decodes WMO codes.
"""
import logging
import requests
from src.config import HOME_LAT, HOME_LON, WEATHER_URL, TEMP_UNIT, WIND_UNIT

logger = logging.getLogger(__name__)

WMO = {
    0: "Clear",          1: "Mainly clear",  2: "Partly cloudy", 3: "Overcast",
    45: "Fog",           48: "Fog",
    51: "Drizzle",       53: "Drizzle",      55: "Drizzle",
    56: "Icy drizzle",   57: "Icy drizzle",
    61: "Rain",          63: "Rain",         65: "Heavy rain",
    66: "Icy rain",      67: "Icy rain",
    71: "Snow",          73: "Snow",         75: "Heavy snow",   77: "Snow",
    80: "Showers",       81: "Showers",      82: "Showers",
    85: "Snow showers",  86: "Snow showers",
    95: "Storms",        96: "Storms",       99: "Storms",
}


def fetch_weather():
    try:
        r = requests.get(WEATHER_URL, params={
            "latitude":        HOME_LAT,
            "longitude":       HOME_LON,
            "current_weather": True,
            "temperature_unit": TEMP_UNIT,
            "windspeed_unit":  WIND_UNIT,
        }, timeout=15)
        r.raise_for_status()
        return r.json().get("current_weather", {})
    except Exception as e:
        logger.error("Weather fetch error: %s", e)
        return {}


def weather_desc(code):
    try:
        return WMO.get(int(code))
    except (TypeError, ValueError):
        return None
