#!/usr/bin/env python3
"""
main.py — FlyInk Board entry point.

Starts the web control panel, then runs the e-ink refresh loop:
  - Fetches nearby flights from OpenSky
  - If a flight is pinned (via web UI), shows the tracking screen
  - Otherwise shows the closest aircraft dashboard
  - Updates shared STATE so the web dashboard stays current
"""
import time
import logging
import sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

from src.config       import DISPLAY_INTERVAL, HOME_LAT, HOME_LON
from src.flights      import get_nearby, haversine, enrich, classify_kind, fmt_alt, fmt_spd
from src.weather      import fetch_weather
from src.tracking     import track_context, TRACK, TRACK_LOCK
from src.display      import draw_view, draw_idle, draw_tracking
from src.web          import start_control_server, STATE, STATE_LOCK


def _flight_summary(state, dist_km) -> dict:
    """Build a JSON-serialisable summary dict for the web dashboard."""
    info = enrich(state)
    alt_m  = state[13] if state[13] is not None else state[7]
    alt_ft = round(alt_m * 3.281) if alt_m is not None else None
    spd_kt = round(state[9] * 1.94384) if state[9] is not None else None
    return {
        "callsign":  (state[1] or "").strip(),
        "flight":    (state[1] or "").strip(),
        "icao24":    state[0],
        "airline":   info.get("airline"),
        "airline_code": info.get("airline_code"),
        "type":      info.get("type"),
        "reg":       info.get("reg"),
        "from_code": info.get("from_code"),
        "from_city": info.get("from_city"),
        "to_code":   info.get("to_code"),
        "to_city":   info.get("to_city"),
        "alt_ft":    alt_ft,
        "spd_kt":    spd_kt,
        "vrate":     state[11],
        "on_ground": bool(state[8]),
        "track_deg": state[10],
        "dist_km":   round(dist_km, 1) if dist_km is not None else None,
    }


def main():
    logger.info("FlyInk Board starting up…")
    start_control_server()

    weather     = {}
    last_wx     = 0.0
    WX_INTERVAL = 600   # refresh weather every 10 min

    while True:
        loop_start = time.time()

        # --- Weather (refresh every 10 min) ----------------------------------
        if loop_start - last_wx > WX_INTERVAL:
            weather = fetch_weather()
            last_wx = loop_start

        # --- Check for pinned flight -----------------------------------------
        with TRACK_LOCK:
            tracking_active = bool(TRACK.get("norm"))

        if tracking_active:
            ctx = track_context()
            if ctx:
                draw_tracking(ctx, weather)
                # Update STATE for web dashboard
                with STATE_LOCK:
                    STATE["updated_at"] = datetime.utcnow().isoformat() + "Z"
                time.sleep(DISPLAY_INTERVAL)
                continue
            # ctx is None → flight ended, fall through to normal mode

        # --- Normal mode: show closest nearby flight -------------------------
        nearby = get_nearby(n=15)
        logger.info("Found %d aircraft nearby.", len(nearby))

        # Build STATE.nearby for the web dashboard
        nearby_summaries = []
        for s, dist in nearby:
            try:
                nearby_summaries.append(_flight_summary(s, dist))
            except Exception as e:
                logger.debug("Summary build error: %s", e)

        if not nearby:
            draw_idle(weather)
            with STATE_LOCK:
                STATE["nearby"]     = []
                STATE["current"]    = None
                STATE["weather"]    = weather
                STATE["updated_at"] = datetime.utcnow().isoformat() + "Z"
        else:
            # Pick the closest flight
            best_state, best_dist = nearby[0]
            draw_view(best_state, best_dist, weather, len(nearby))

            current_summary = nearby_summaries[0] if nearby_summaries else None
            with STATE_LOCK:
                STATE["nearby"]     = nearby_summaries
                STATE["current"]    = current_summary
                STATE["weather"]    = weather
                STATE["updated_at"] = datetime.utcnow().isoformat() + "Z"

        elapsed = time.time() - loop_start
        sleep_s = max(0, DISPLAY_INTERVAL - elapsed)
        logger.debug("Loop took %.1f s, sleeping %.1f s.", elapsed, sleep_s)
        time.sleep(sleep_s)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutdown signal received. Exiting.")
