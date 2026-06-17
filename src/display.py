"""
display.py — All Inky e-paper rendering.

Covers:
  - Display initialisation (inky object, palette, W/H)
  - Font helpers
  - Aircraft silhouette icons
  - Logo loading / dithering
  - draw_view      : normal nearby-flight dashboard
  - draw_idle      : no-aircraft screen
  - draw_tracking  : pinned-flight tracking screen
  - draw_lower     : shared lower panel (telemetry, radar, footer)
  - render         : push image to hardware
"""
import os
import math
import time
import socket
import logging
from datetime import datetime, timedelta

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from src.config import (
    ROTATE, RADAR_RANGE_KM, LOGO_DIR,
    FONT_BOLD, FONT_REG, TEMP_UNIT, WIND_UNIT,
    HOME_LAT, HOME_LON,
)
from src.flights import (
    haversine, bearing, compass, enrich, classify_kind,
    fmt_alt, fmt_spd, fmt_vs, fmt_track,
    fetch_route, _airport_obj,
)
from src.weather import weather_desc

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Display init
# ---------------------------------------------------------------------------
try:
    from inky.auto import auto as _inky_auto
    inky = _inky_auto()
    inky.set_border(inky.WHITE)
    INKY_AVAILABLE = True
except Exception as exc:
    inky = None
    INKY_AVAILABLE = False
    logger.warning("Inky display not available — running in simulation mode: %s", exc)


def col(name):
    if inky is None:
        return {"WHITE": 255, "BLACK": 0, "RED": 128,
                "BLUE": 64, "YELLOW": 192, "GREEN": 96}.get(name, 0)
    return getattr(inky, name, inky.BLACK)


if inky and ROTATE in (90, 270):
    W, H = inky.height, inky.width
elif inky:
    W, H = inky.width, inky.height
else:
    W, H = 480, 800   # simulation fallback (portrait)

PAD         = int(W * 0.058)
KM_TO_MI    = 0.621371
TEMP_SYMBOL = "F" if TEMP_UNIT == "fahrenheit" else "C"
WIND_LABEL  = {"mph": "mph", "kmh": "km/h", "ms": "m/s", "kn": "kt"}[WIND_UNIT]

# Build palette for logo dithering
_INK_CANDIDATES = [
    ("WHITE",  (255, 255, 255)), ("BLACK",  (0,   0,   0)),
    ("RED",    (200,  40,  40)), ("YELLOW", (235, 210,  40)),
    ("GREEN",  ( 45, 140,  60)), ("BLUE",   ( 40,  70, 170)),
    ("ORANGE", (220, 120,  30)),
]
if inky:
    INKS = [(n, rgb) for n, rgb in _INK_CANDIDATES
            if isinstance(getattr(inky, n, None), int)]
else:
    INKS = _INK_CANDIDATES

_PAL  = Image.new("P", (1, 1))
_flat: list = []
for _n, _rgb in INKS:
    _flat += list(_rgb)
_flat += [0, 0, 0] * (256 - len(INKS))
_PAL.putpalette(_flat)
_DITHER = getattr(getattr(Image, "Dither", Image), "FLOYDSTEINBERG", 3)

# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------
_font_cache: dict = {}


def font(size, reg=False):
    key = (int(size), reg)
    if key not in _font_cache:
        try:
            _font_cache[key] = ImageFont.truetype(
                FONT_REG if reg else FONT_BOLD, int(size))
        except OSError:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def fit_font(text, max_w, max_size, min_size=12):
    s = int(max_size)
    while s > min_size:
        if font(s).getlength(text) <= max_w:
            return font(s)
        s -= 2
    return font(min_size)


# ---------------------------------------------------------------------------
# Aircraft silhouette icons
# ---------------------------------------------------------------------------
_PARAMS = {
    "heavy":     dict(span=1.05, sweep=0.42, fus=0.13, nose=1.0,  eng=4),
    "jet":       dict(span=0.95, sweep=0.34, fus=0.10, nose=0.95, eng=2),
    "bizjet":    dict(span=0.78, sweep=0.28, fus=0.085,nose=0.95, eng="rear"),
    "turboprop": dict(span=1.02, sweep=0.10, fus=0.12, nose=0.88, eng="prop"),
    "light":     dict(span=0.88, sweep=0.02, fus=0.10, nose=0.78, eng="nose"),
}
_BK = (0, 0, 0, 255)


def _fixedwing(d, cx, cy, s, p):
    fus = p["fus"]
    d.ellipse([cx - fus*s, cy - p["nose"]*s, cx + fus*s, cy + 0.82*s], fill=_BK)
    k, span = p["sweep"], p["span"]
    for sign in (1, -1):
        d.polygon([(cx + sign*0.05*s, cy - 0.10*s),
                   (cx + sign*span*s, cy + k*s),
                   (cx + sign*span*s, cy + (k+0.12)*s),
                   (cx + sign*0.05*s, cy + 0.18*s)], fill=_BK)
        d.polygon([(cx + sign*0.04*s, cy + 0.58*s),
                   (cx + sign*0.42*s, cy + (0.72 + k*0.4)*s),
                   (cx + sign*0.42*s, cy + (0.80 + k*0.4)*s),
                   (cx + sign*0.04*s, cy + 0.72*s)], fill=_BK)
    if p["eng"] in (2, 4):
        xs = [0.42] if p["eng"] == 2 else [0.32, 0.62]
        for sign in (1, -1):
            for ex in xs:
                ey = cy + (k*(ex/span) + 0.16)*s
                d.ellipse([cx+sign*ex*s - 0.05*s, ey - 0.10*s,
                           cx+sign*ex*s + 0.05*s, ey + 0.12*s], fill=_BK)
    elif p["eng"] == "prop":
        for sign in (1, -1):
            ex = 0.4
            ey = cy + (k*(ex/span))*s
            d.ellipse([cx+sign*ex*s - 0.16*s, ey - 0.16*s,
                       cx+sign*ex*s + 0.16*s, ey + 0.16*s], outline=_BK, width=2)
    elif p["eng"] == "rear":
        for sign in (1, -1):
            d.ellipse([cx+sign*0.12*s - 0.05*s, cy + 0.42*s,
                       cx+sign*0.12*s + 0.05*s, cy + 0.62*s], fill=_BK)
    elif p["eng"] == "nose":
        d.line([cx - 0.22*s, cy - p["nose"]*s,
                cx + 0.22*s, cy - p["nose"]*s], fill=_BK, width=3)


def _heli(d, cx, cy, s):
    d.ellipse([cx - 0.22*s, cy - 0.5*s, cx + 0.22*s, cy + 0.45*s], fill=_BK)
    d.line([cx, cy + 0.4*s, cx, cy + 0.95*s], fill=_BK,
           width=max(2, int(0.06*s)))
    d.ellipse([cx - 0.12*s, cy + 0.85*s, cx + 0.12*s, cy + 1.02*s], fill=_BK)
    d.ellipse([cx - 0.98*s, cy - 0.98*s, cx + 0.98*s, cy + 0.98*s],
              outline=_BK, width=2)


def icon_tile(kind, track, box):
    tile = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    td   = ImageDraw.Draw(tile)
    c    = box / 2
    s    = box * 0.40
    if kind == "heli":
        _heli(td, c, c, s)
    else:
        _fixedwing(td, c, c, s, _PARAMS.get(kind, _PARAMS["jet"]))
    return tile.rotate(-(track or 0), resample=Image.BICUBIC, expand=False)


def paste_icon(img, kind, track, cx, cy, box):
    tile = icon_tile(kind, track, box)
    img.paste(col("BLACK"), (int(cx - box/2), int(cy - box/2)),
              tile.getchannel("A"))


# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------

def load_logo(code, max_box):
    if not code:
        return None
    path = os.path.join(LOGO_DIR, f"{code}.png")
    if not os.path.exists(path):
        return None
    try:
        logo = Image.open(path).convert("RGBA")
        logo.thumbnail(max_box)
        return logo
    except Exception as e:
        logger.debug("Logo load error: %s", e)
        return None


def paste_logo(img, logo, x_left, top):
    rgb   = logo.convert("RGB").quantize(palette=_PAL, dither=_DITHER).convert("RGB")
    arr   = np.asarray(rgb)
    alpha = np.asarray(logo.split()[3])
    for name, c in INKS:
        if name == "WHITE":
            continue
        m = (np.all(arr == np.array(c, dtype=arr.dtype), axis=-1) & (alpha > 40))
        if m.any():
            mask = Image.fromarray((m * 255).astype("uint8"), "L")
            img.paste(col(name), (int(x_left), int(top)), mask)


# ---------------------------------------------------------------------------
# Device utilities
# ---------------------------------------------------------------------------
_net = {"ts": 0.0, "ip": None, "host": None}


def get_net():
    if _net["ip"] and time.time() - _net["ts"] < 300:
        return _net["host"], _net["ip"]
    host = ip = None
    try:
        host = socket.gethostname().split(".")[0]
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = None
    _net.update(ts=time.time(), ip=ip, host=host)
    return host, ip



def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return f"{int(f.read().strip()) / 1000:.0f}°C"
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Radar
# ---------------------------------------------------------------------------

def draw_radar(img, d, cx, cy, R, dist_km, brg, track, kind):
    d.ellipse([cx-R, cy-R, cx+R, cy+R], outline=col("BLACK"), width=2)
    rr = R / 2
    for a in range(0, 360, 12):
        x1 = cx + rr * math.sin(math.radians(a))
        y1 = cy - rr * math.cos(math.radians(a))
        d.ellipse([x1-1, y1-1, x1+1, y1+1], fill=col("BLACK"))
    d.line([cx, cy-R-6, cx, cy-R+6], fill=col("BLACK"), width=2)
    d.text((cx, cy-R-14), "N", font=font(13), fill=col("BLACK"), anchor="mm")
    d.ellipse([cx-4, cy-4, cx+4, cy+4], fill=col("RED"))
    if dist_km is not None and brg is not None:
        r  = min(R, R * (dist_km / RADAR_RANGE_KM))
        ax = cx + r * math.sin(math.radians(brg))
        ay = cy - r * math.cos(math.radians(brg))
        paste_icon(img, kind, track, ax, ay, 38)


# ---------------------------------------------------------------------------
# Shared lower panel
# ---------------------------------------------------------------------------

def draw_lower(img, d, state, info, dist_km, brg, kind, weather, caption):
    from src.weather import weather_desc
    if info.get("type"):
        tf = fit_font(info["type"].upper(), int(W * 0.9), 34)
        d.text((W/2, 322), info["type"].upper(), font=tf,
               fill=col("BLUE"), anchor="mm")
    d.text((W/2, 360), f"{dist_km * KM_TO_MI:.0f} MI {compass(brg)} OF YOU",
           font=font(20), fill=col("RED"), anchor="mm")
    d.line([PAD, 386, W-PAD, 386], fill=col("BLACK"), width=1)

    cols = [("ALT",    fmt_alt(state)), ("GND SPD", fmt_spd(state)),
            ("V/SPEED", fmt_vs(state)), ("TRACK",   fmt_track(state))]
    colw = (W - 2*PAD) / 4
    for i, (lab, val) in enumerate(cols):
        cx = PAD + colw * (i + 0.5)
        d.text((cx, 406), lab, font=font(14), fill=col("RED"),   anchor="mm")
        d.text((cx, 440), val, font=fit_font(val, colw-6, 26),
               fill=col("BLACK"), anchor="mm")

    d.line([PAD, 468, W-PAD, 468], fill=col("BLACK"), width=1)
    d.text((W/2, 500), "RANGE & BEARING FROM HOME",
           font=font(14), fill=col("RED"), anchor="mm")
    draw_radar(img, d, W/2, 600, 80, dist_km, brg, state[10], kind)

    # Aircraft identity either side of the radar, in the free margins:
    # registration country on the left, registration number on the right.
    margin_w = int((W/2 - 80) - PAD - 6)   # width of the gap beside the radar

    def _radar_label(cx, head, txt):
        d.text((cx, 588), head, font=font(11), fill=col("RED"), anchor="mm")
        d.text((cx, 610), txt.upper(),
               font=fit_font(txt.upper(), margin_w, 14, 9),
               fill=col("BLACK"), anchor="mm")

    left_cx  = PAD + margin_w/2
    right_cx = W - PAD - margin_w/2
    reg_country = info.get("reg_country")
    reg         = info.get("reg")
    if reg_country:
        _radar_label(left_cx, "COUNTRY", reg_country)
    elif dist_km is not None:
        _radar_label(left_cx, "RANGE", f"{dist_km * KM_TO_MI:.0f} MI")
    if reg:
        _radar_label(right_cx, "REG", reg)
    elif brg is not None:
        _radar_label(right_cx, "BEARING", f"{brg:.0f}° {compass(brg)}")

    d.text((W/2, 690), caption, font=font(12, reg=True),
           fill=col("BLACK"), anchor="mm")
    d.line([PAD, 708, W-PAD, 708], fill=col("RED"), width=2)

    now = datetime.now()
    d.text((PAD,    734), now.strftime("%a, %b %d").upper(),
           font=font(18), fill=col("BLACK"), anchor="lm")
    d.text((W-PAD, 734), now.strftime("%I:%M %p").lstrip("0"),
           font=font(30), fill=col("BLACK"), anchor="rm")

    # Footer split into three non-overlapping zones across the width so the
    # weather, condition, and host/IP never collide.
    third = (W - 2*PAD) / 3
    t  = weather.get("temperature")
    w  = weather.get("windspeed")
    wd = weather.get("winddirection")
    parts = []
    if t is not None:
        parts.append(f"{t}°{TEMP_SYMBOL}")
    if w is not None:
        wdir = f" {compass(wd)}" if wd is not None else ""
        parts.append(f"{w} {WIND_LABEL}{wdir}")
    wx = "  ·  ".join(parts)
    if wx:
        d.text((PAD, 766), wx, font=fit_font(wx, int(third-8), 15),
               fill=col("BLACK"), anchor="lm")
    cond = weather_desc(weather.get("weathercode"))
    if cond:
        d.text((W/2, 766), cond.upper(),
               font=fit_font(cond.upper(), int(third-8), 13, 9),
               fill=col("RED"), anchor="mm")
    host, ip = get_net()
    netinfo = f"{host} · {ip}" if (host and ip) else (ip or host or "")
    if netinfo:
        d.text((W-PAD, 766), netinfo,
               font=fit_font(netinfo, int(third-8), 14, 9),
               fill=col("BLACK"), anchor="rm")


# ---------------------------------------------------------------------------
# draw_view — normal dashboard
# ---------------------------------------------------------------------------

def draw_view(state, dist_km, weather, total_nearby):
    info  = enrich(state)
    kind  = classify_kind(info["type"], bool(info["airline"]))
    track = state[10]
    lat, lon = state[6], state[5]
    brg   = bearing(HOME_LAT, HOME_LON, lat, lon)

    img = Image.new("P", (W, H), col("WHITE") if inky is None else inky.WHITE)
    d   = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, int(H*0.01)+6], fill=col("RED"))

    hx   = PAD
    logo = load_logo(info["airline_code"], (120, 80))
    if logo:
        paste_logo(img, logo, PAD, 20)
        hx = PAD + logo.size[0] + 16

    d.text((W-PAD, 40), info["flight"] or "----",
           font=font(30), fill=col("RED"), anchor="rm")
    if info["reg"]:
        d.text((W-PAD, 72), info["reg"],
               font=font(18, reg=True), fill=col("BLACK"), anchor="rm")
    if info["airline"]:
        af = fit_font(info["airline"].upper(), W - hx - 150, 26)
        d.text((hx, 60), info["airline"].upper(), font=af,
               fill=col("BLACK"), anchor="lm")
    d.line([PAD, 114, W-PAD, 114], fill=col("RED"), width=3)

    yC       = 220
    has_from = bool(info["from_code"])
    has_to   = bool(info["to_code"])
    if has_from and has_to:
        of = fit_font(info["from_code"], 150, 84)
        d.text((PAD,    yC), info["from_code"], font=of,
               fill=col("BLACK"), anchor="lm")
        df = fit_font(info["to_code"], 150, 84)
        d.text((W-PAD, yC), info["to_code"], font=df,
               fill=col("BLACK"), anchor="rm")
        if info["from_city"]:
            d.text((PAD, yC+58), info["from_city"].upper(),
                   font=font(16), fill=col("BLACK"), anchor="lm")
        if info["to_city"]:
            d.text((W-PAD, yC+58), info["to_city"].upper(),
                   font=font(16), fill=col("BLACK"), anchor="rm")
        paste_icon(img, kind, track, W/2, yC, 96)
    elif has_from or has_to:
        code   = info["from_code"] if has_from else info["to_code"]
        city   = info["from_city"] if has_from else info["to_city"]
        prefix = "DEPARTING" if has_from else "INBOUND TO"
        paste_icon(img, kind, track, W/2, 178, 86)
        cf = fit_font(f"{prefix}  {code}", int(W*0.82), 46)
        d.text((W/2, 250), f"{prefix}  {code}", font=cf,
               fill=col("BLACK"), anchor="mm")
        if city:
            d.text((W/2, 286), city.upper(), font=font(16),
                   fill=col("BLACK"), anchor="mm")
    else:
        paste_icon(img, kind, track, W/2, 160, 74)
        cf = fit_font(info["flight"] or "----", int(W*0.7), 84)
        d.text((W/2, yC+18), info["flight"] or "----", font=cf,
               fill=col("BLACK"), anchor="mm")

    cap = f"{total_nearby} NEARBY · {RADAR_RANGE_KM * KM_TO_MI:.0f} MI"
    ct  = cpu_temp()
    if ct:
        cap += f" · CPU {ct}"
    draw_lower(img, d, state, info, dist_km, brg, kind, weather, cap)
    render(img)


# ---------------------------------------------------------------------------
# draw_idle
# ---------------------------------------------------------------------------

def draw_idle(weather):
    img = Image.new("P", (W, H), col("WHITE") if inky is None else inky.WHITE)
    d   = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 6], fill=col("RED"))
    paste_icon(img, "jet", 0, W/2, H*0.42, 90)
    d.text((W/2, H*0.56), "No aircraft nearby",
           font=font(24), fill=col("BLACK"), anchor="mm")
    d.text((W/2, H*0.92), datetime.now().strftime("%H:%M"),
           font=font(15), fill=col("RED"), anchor="mm")
    render(img)


# ---------------------------------------------------------------------------
# draw_tracking — pinned-flight screen
# ---------------------------------------------------------------------------

def draw_tracking(ctx: dict, weather: dict):
    from src.tracking import (draw_progress, route_progress,
                              TRACK_LOCK, TRACK, TRACK_LINGER_S)

    mode  = ctx["mode"]
    sched = ctx.get("sched") or {}
    state = ctx.get("state")

    with TRACK_LOCK:
        query = TRACK["query"]

    img = Image.new("P", (W, H), col("WHITE") if inky is None else inky.WHITE)
    d   = ImageDraw.Draw(img)

    # Red "FLIGHT TRACKING" banner
    d.rectangle([0, 0, W, 42], fill=col("RED"))
    d.text((W/2, 21), "✈  FLIGHT TRACKING", font=font(20),
           fill=col("WHITE"), anchor="mm")

    if state is not None:
        info     = enrich(state)
        kind     = classify_kind(info.get("type"), bool(info.get("airline")))
        lat, lon = state[6], state[5]
        brg      = bearing(HOME_LAT, HOME_LON, lat, lon)
        dist_km  = haversine(HOME_LAT, HOME_LON, lat, lon)
    else:
        info     = {"airline": None, "airline_code": None, "flight": query,
                    "type": None, "reg": None}
        kind     = "jet"
        brg      = 0.0
        dist_km  = 0.0
        lat, lon = None, None

    # Header
    hx   = PAD
    logo = load_logo(info.get("airline_code"), (108, 60))
    if logo:
        paste_logo(img, logo, PAD, 54)
        hx = PAD + logo.size[0] + 14
    d.text((W-PAD, 70), (info.get("flight") or query or "").upper(),
           font=font(28), fill=col("RED"), anchor="rm")
    if info.get("reg"):
        d.text((W-PAD, 98), info["reg"],
               font=font(15, reg=True), fill=col("BLACK"), anchor="rm")
    if info.get("airline"):
        af = fit_font(info["airline"].upper(), W-hx-140, 22)
        d.text((hx, 80), info["airline"].upper(), font=af,
               fill=col("BLACK"), anchor="lm")
    d.line([PAD, 120, W-PAD, 120], fill=col("RED"), width=2)

    # Status + delay badge
    status_str = (sched.get("status") or
                  ("EN ROUTE" if mode == "track" else
                   "LANDED"   if mode == "landed" else "SCHEDULED")).upper()
    d.text((PAD, 144), status_str, font=font(18), fill=col("BLACK"), anchor="lm")
    delay = sched.get("delay_min")
    if delay is not None and delay >= 5:
        d.text((W-PAD, 144), f"DELAYED +{delay} MIN",
               font=font(16), fill=col("RED"), anchor="rm")
    elif delay is not None and delay <= -2:
        d.text((W-PAD, 144), f"{-delay} MIN EARLY",
               font=font(16), fill=col("BLACK"), anchor="rm")
    elif sched:
        d.text((W-PAD, 144), "ON TIME",
               font=font(16), fill=col("BLACK"), anchor="rm")

    # Route + progress bar
    cs    = (state[1] if state else ctx.get("norm")) or ctx.get("norm")
    fr    = fetch_route(cs)
    o     = _airport_obj(fr.get("origin"))      if fr else None
    dst   = _airport_obj(fr.get("destination")) if fr else None
    o_code  = sched.get("dep_iata") or (o   or {}).get("code") or "?"
    d_code  = sched.get("arr_iata") or (dst or {}).get("code") or "?"

    d.text((PAD,    180), o_code,
           font=fit_font(o_code, 130, 56), fill=col("BLACK"), anchor="lm")
    d.text((W-PAD, 180), d_code,
           font=fit_font(d_code, 130, 56), fill=col("BLACK"), anchor="rm")

    frac, rem_km = route_progress(mode, state, o, dst, lat, lon)

    draw_progress(img, d, PAD+78, W-PAD-78, 214, frac, kind, paste_icon, col)

    # Scheduled / actual times
    dep_sched = sched.get("dep_sched") or "--"
    arr_sched = sched.get("arr_sched") or "--"
    dep_act   = sched.get("dep_actual")
    arr_est   = sched.get("arr_estimated")

    d.text((PAD,    244), f"DEP  {dep_sched}",
           font=font(14), fill=col("BLACK"), anchor="lm")
    if dep_act and dep_act != dep_sched:
        d.text((PAD, 264), f"→ {dep_act}",
               font=font(14), fill=col("RED"), anchor="lm")
    d.text((W-PAD, 244), f"ARR  {arr_sched}",
           font=font(14), fill=col("BLACK"), anchor="rm")
    if arr_est and arr_est != arr_sched:
        d.text((W-PAD, 264), f"→ {arr_est}",
               font=font(14), fill=col("RED"), anchor="rm")

    # ETA from groundspeed (works without a schedule API key). Remaining distance
    # is the same value that positions the icon, so the "MIN LEFT" stays
    # consistent with how far along the bar the plane sits.
    eta_line = ""
    if (mode == "track" and state and rem_km is not None
            and state[9] and state[9] > 30):
        gs   = state[9] * 3.6
        mins = rem_km / gs * 60
        eta  = (datetime.now() + timedelta(minutes=mins)).strftime(
            "%I:%M %p").lstrip("0")
        eta_line = f"ETA ~{eta}  ·  {mins:.0f} MIN LEFT"
    elif mode == "landed":
        eta_line = "ARRIVED"
    if eta_line:
        d.text((W/2, 290), eta_line, font=font(15),
               fill=col("RED"), anchor="mm")

    if state is not None:
        if mode == "landed" and ctx.get("landed_at"):
            left = max(0, int(TRACK_LINGER_S - (time.time() - ctx["landed_at"])))
            cap  = f"TRACKING · STOPS IN {left//60}:{left%60:02d}"
        else:
            cap = f"TRACKING  {query}"
        draw_lower(img, d, state, info, dist_km, brg, kind, weather, cap)
    else:
        paste_icon(img, "jet", 0, W/2, 360, 80)
        d.text((W/2, 430), "AWAITING DEPARTURE",
               font=font(22), fill=col("BLACK"), anchor="mm")

    render(img)


# ---------------------------------------------------------------------------
# render — push to hardware (simulation mode is a no-op unless explicitly enabled)
# ---------------------------------------------------------------------------

def render(img):
    out = img.rotate(ROTATE, expand=True) if ROTATE else img
    if inky:
        inky.set_image(out)
        inky.show()
    elif os.environ.get("SAVE_SIMULATION_OUTPUT") == "1":
        path = "simulation_output.png"
        out.convert("RGB").save(path)
        logger.info("Simulation saved to %s", path)
