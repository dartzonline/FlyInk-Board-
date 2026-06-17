#!/usr/bin/env python3
# main.py — FlyInk Board. Starts the web server then runs the e-ink refresh loop.
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

from src.config   import DISPLAY_INTERVAL, HOME_LAT, HOME_LON
from src.flights  import get_nearby, haversine, bearing, enrich, classify_kind
from src.weather  import fetch_weather
from src.tracking import track_context, TRACK, TRACK_LOCK
from src.display  import draw_view, draw_idle, draw_tracking
from src.web      import (start_control_server, STATE, STATE_LOCK,
                          record_nearby, pop_queued, peek_queued)


def _track_signature():
    """Snapshot of the control state that, if changed, should wake the display."""
    with TRACK_LOCK:
        q = TRACK.get("query")
    return (q, peek_queued())


def _responsive_sleep(seconds):
    """Sleep up to `seconds`, but wake early (within ~1s) if the pinned flight
    is changed/stopped or a flight is queued from the web, so the e-ink reacts
    promptly instead of waiting out the full refresh interval."""
    baseline = _track_signature()
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(min(1.0, end - time.time()))
        if _track_signature() != baseline:
            logger.info("Control state changed — refreshing display early.")
            return


def _flight_summary(state, dist_km) -> dict:
    callsign = (state[1] or "").strip()
    lat, lon = state[6], state[5]
    alt_m  = state[13] if state[13] is not None else state[7]
    alt_ft = round(alt_m * 3.281) if alt_m is not None else None
    spd_kt = round(state[9] * 1.94384) if state[9] is not None else None
    vrate_fpm = round(state[11] * 196.85) if state[11] is not None else None
    brg = bearing(HOME_LAT, HOME_LON, lat, lon) if (lat is not None and lon is not None) else None

    info = {}
    try:
        info = enrich(state) or {}
    except Exception as exc:
        logger.debug("enrichment fallback for %s: %s", callsign, exc)

    return {
        "callsign":     callsign or state[0] or "--",
        "flight":       callsign or state[0] or "--",
        "icao24":       state[0],
        "airline":      info.get("airline"),
        "airline_code": info.get("airline_code"),
        "type":         info.get("type"),
        "reg":          info.get("reg"),
        "from_code":    info.get("from_code"),
        "from_city":    info.get("from_city"),
        "from_country": info.get("from_country"),
        "to_code":      info.get("to_code"),
        "to_city":      info.get("to_city"),
        "to_country":   info.get("to_country"),
        "alt_ft":       alt_ft,
        "spd_kt":       spd_kt,
        "vrate":        vrate_fpm,
        "on_ground":    bool(state[8]),
        "track_deg":    state[10],
        "bearing_deg":  round(brg, 1) if brg is not None else None,
        "dist_km":      round(dist_km, 1) if dist_km is not None else None,
        "lat":          lat,
        "lon":          lon,
    }


def main():
    logger.info("FlyInk Board starting up…")
    start_control_server()

    weather      = {}
    last_wx      = 0.0
    WX_INTERVAL  = 600   # refresh weather every 10 min
    # When tracking is active we alternate: tracking screen → nearest nearby → repeat.
    # This boolean flips every cycle so the pilot still gets a local traffic update.
    show_nearby_interlude = False
    # Track whether the previous on-screen flight was commercial (had an airline),
    # so we can avoid showing two non-commercial flights back to back.
    last_shown_commercial = True

    while True:
        loop_start = time.time()

        # --- Weather (refresh every 10 min) ----------------------------------
        if loop_start - last_wx > WX_INTERVAL:
            weather = fetch_weather()
            last_wx = loop_start
            # Publish weather immediately so the web dashboard renders during the
            # first (slow) nearby scan instead of appearing blank on cold start.
            with STATE_LOCK:
                STATE["weather"] = weather
                if not STATE.get("updated_at"):
                    STATE["updated_at"] = datetime.utcnow().isoformat() + "Z"

        # --- Check for pinned flight -----------------------------------------
        with TRACK_LOCK:
            tracking_active = bool(TRACK.get("norm"))

        if tracking_active:
            ctx = track_context()
            if ctx:
                if show_nearby_interlude:
                    # Briefly show the closest real flight before flipping back
                    logger.info("Tracking interlude — showing nearest traffic.")
                    nearby = get_nearby(n=15)
                    nearby_summaries = []
                    for s, dist in nearby:
                        try:
                            nearby_summaries.append(_flight_summary(s, dist))
                        except Exception as e:
                            logger.debug("summary error: %s", e)
                    record_nearby(nearby_summaries)
                    if nearby:
                        best_state, best_dist = nearby[0]
                        draw_view(best_state, best_dist, weather, len(nearby))
                        with STATE_LOCK:
                            STATE["nearby"]     = nearby_summaries
                            STATE["current"]    = nearby_summaries[0] if nearby_summaries else None
                            STATE["weather"]    = weather
                            STATE["updated_at"] = datetime.utcnow().isoformat() + "Z"
                    else:
                        draw_idle(weather)
                else:
                    # Normal tracking screen
                    draw_tracking(ctx, weather)
                    with STATE_LOCK:
                        STATE["updated_at"] = datetime.utcnow().isoformat() + "Z"

                # flip for next cycle
                show_nearby_interlude = not show_nearby_interlude
                elapsed = time.time() - loop_start
                _responsive_sleep(max(0, DISPLAY_INTERVAL - elapsed))
                continue

            # ctx is None → flight ended, reset interlude state and fall through
            show_nearby_interlude = False

        # --- Normal mode: show closest nearby flight -------------------------
        show_nearby_interlude = False   # reset whenever not tracking
        nearby = get_nearby(n=15)
        logger.info("Found %d aircraft nearby.", len(nearby))

        # Build STATE.nearby for the web dashboard
        nearby_summaries = []
        for s, dist in nearby:
            try:
                nearby_summaries.append(_flight_summary(s, dist))
            except Exception as e:
                logger.debug("summary error: %s", e)

        record_nearby(nearby_summaries)  # feed the stats ring buffer

        if not nearby:
            draw_idle(weather)
            with STATE_LOCK:
                STATE["nearby"]     = []
                STATE["current"]    = None
                STATE["weather"]    = weather
                STATE["updated_at"] = datetime.utcnow().isoformat() + "Z"
        else:
            # A web click can queue one flight to be shown next, just once.
            queued_cs = pop_queued()
            chosen_idx = 0
            if queued_cs:
                for i, (s, _d) in enumerate(nearby):
                    if (s[1] or "").strip().upper() == queued_cs:
                        chosen_idx = i
                        logger.info("Showing queued flight %s (one-shot).", queued_cs)
                        break
                else:
                    logger.info("Queued flight %s no longer nearby; showing closest.",
                                queued_cs)

            # Variety guard: never show two non-commercial flights in a row. If
            # the closest is non-commercial and the last screen was too, jump to
            # the nearest commercial flight (one with an airline) instead.
            if not queued_cs:
                def _is_commercial(i):
                    return bool(nearby_summaries[i].get("airline")) if i < len(nearby_summaries) else False

                if not _is_commercial(chosen_idx) and not last_shown_commercial:
                    for i in range(len(nearby)):
                        if _is_commercial(i):
                            chosen_idx = i
                            logger.info("Variety: swapping to nearest commercial flight %s.",
                                        (nearby[i][0][1] or "").strip())
                            break

            best_state, best_dist = nearby[chosen_idx]
            last_shown_commercial = (bool(nearby_summaries[chosen_idx].get("airline"))
                                     if chosen_idx < len(nearby_summaries) else False)
            draw_view(best_state, best_dist, weather, len(nearby))

            current_summary = nearby_summaries[chosen_idx] if nearby_summaries else None
            with STATE_LOCK:
                STATE["nearby"]     = nearby_summaries
                STATE["current"]    = current_summary
                STATE["weather"]    = weather
                STATE["updated_at"] = datetime.utcnow().isoformat() + "Z"

        elapsed = time.time() - loop_start
        sleep_s = max(0, DISPLAY_INTERVAL - elapsed)
        logger.debug("Loop took %.1f s, sleeping %.1f s.", elapsed, sleep_s)
        _responsive_sleep(sleep_s)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutdown signal received. Exiting.")
