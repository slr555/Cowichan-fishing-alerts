#!/usr/bin/env python3
"""
Cowichan River fishing alert, v2.

Twice a day: pulls real-time discharge (Water Survey of Canada 08HA011 near
Duncan and 08HA002 at the Lake Cowichan weir) and weather (Open-Meteo), scores
each species against Cowichan-specific flow bands and the regulation calendar,
and pushes an ntfy alert only when something changes.

Also:
  * watches the DFO and BC regulation pages and alerts when a Cowichan line changes
  * alerts when a data feed fails or goes stale (and when it recovers)
  * sends a weekend outlook on Thursday afternoon
  * attaches a strategy card (open section, legal gear, the play, sun times)

Standard library only. Python 3.11+.

  python cowichan_alert.py                 normal run
  python cowichan_alert.py --dry-run       run, print, no push
  python cowichan_alert.py --weekend       force the weekend outlook
  python cowichan_alert.py --demo nov-drop synthetic scenario (see DEMOS):
                                           nov-drop, oct-pulse, drought, feb-steel, thursday
"""
import argparse
import csv
import hashlib
import html.parser
import io
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Vancouver")
UTC = timezone.utc
HERE = os.path.dirname(os.path.abspath(__file__))

# =============================== CONFIG ====================================

STATION_LOWER = "08HA011"   # Cowichan River near Duncan
STATION_WEIR = "08HA002"    # Cowichan River at Lake Cowichan (weir release)
WX_UPPER = (48.82, -124.05)  # Lake Cowichan: rain that moves the river
WX_LOWER = (48.78, -123.71)  # Duncan: temperature, barometer, cloud, sun

NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
ALWAYS_SEND = os.getenv("ALWAYS_SEND", "false").strip().lower() == "true"
STATE_FILE = os.getenv("STATE_FILE", os.path.join(HERE, "state.json"))
DFO_REGS_URL = os.getenv("DFO_REGS_URL", "https://www.pac.dfo-mpo.gc.ca/fm-gp/rec/fresh-douce/region1-eng.html")
BC_REGS_URL = os.getenv("BC_REGS_URL", "https://www2.gov.bc.ca/gov/content/sports-culture/recreation/fishing-hunting/fishing/fishing-regulations")

GOOD, FAIR = 7.0, 5.0
DROUGHT_FLOW = 7.0        # Sep-Oct below this at Duncan: no GOOD alerts
STALE_HOURS = 12          # gauge reading older than this = feed problem
REG_FAILS_BEFORE_ALERT = 3
OUTLOOK_WEEKDAY, OUTLOOK_HOUR = 3, 13   # Thursday, after 1pm Pacific
MAX_PUSH_BYTES = 3900     # ntfy turns longer messages into attachments
REGS_VALID_UNTIL = date(2027, 3, 31)    # 2025-2027 synopsis

# Rough flow projection for the weekend outlook (tune after backtesting).
# q_next = base + (q - base) * recession + k * rain_mm, base = weir release
PROJ = {"recession": 0.7, "k_dry_fall": 0.4, "k_wet_fall": 0.8,
        "k_winter": 1.5, "k_other": 0.6, "wet_fall_mm14": 40}

# Flow bands in m3/s at 08HA011 (mean annual flow is ~53).
SPECIES = [
    {"key": "coho", "name": "Coho", "kind": "salmon",
     "season": {10: 0.6, 11: 1.0, 12: 0.6, 1: 0.2},
     "good_flow": (15, 60), "fishable_flow": (8, 110), "style": "freshet",
     "cold_limit": None, "heat_limit": None},
    {"key": "chum", "name": "Chum", "kind": "salmon",
     "season": {10: 0.4, 11: 1.0, 12: 0.5},
     "good_flow": (12, 80), "fishable_flow": (7, 120), "style": "freshet",
     "cold_limit": None, "heat_limit": None},
    {"key": "steelhead", "name": "Winter steelhead", "kind": "steelhead",
     "season": {12: 0.5, 1: 0.8, 2: 1.0, 3: 1.0, 4: 0.5},
     "good_flow": (25, 90), "fishable_flow": (12, 150), "style": "freshet",
     "cold_limit": -3, "heat_limit": None},
    {"key": "egg", "name": "Trout egg bite", "kind": "trout",
     "season": {10: 0.7, 11: 1.0, 12: 1.0, 1: 0.7},
     "good_flow": (10, 45), "fishable_flow": (6, 70), "style": "egg",
     "cold_limit": None, "heat_limit": None},
    {"key": "spring", "name": "Spring trout", "kind": "trout",
     "season": {3: 0.6, 4: 1.0, 5: 0.9, 6: 0.6},
     "good_flow": (8, 40), "fishable_flow": (5, 70), "style": "stable",
     "cold_limit": None, "heat_limit": 25},
]

SECTIONS = {
    "A": "Weir to Greendale Trestle",
    "B": "Greendale Trestle to Mile 70.2 bridge",
    "C": "Mile 70.2 to Mile 66 trestle (Skutz Falls, Marie Canyon)",
    "D": "Mile 66 trestle to the Sandy Pool sign (Stoltz Pool area)",
    "E": "Sandy Pool sign to tidewater (Duncan)",
}

STANDING_RULES = ("Single barbless, bait ban (keep it unscented), release wild trout and "
                  "wild steelhead, no fishing within 100 m of the Skutz fishway or counting fence.")

CARDS = {
    "coho": {
        "prefer": ["D", "E"],
        "where": "Riverbottom Road pools (Stoltz, Sandy Pool); slow deep holding water and back eddies",
        "gear": "Vibrex #3-4 (green water) or #2 (low/clear) in brass, silver, chartreuse or pink, "
                "treble swapped for a single barbless siwash; 3/8 oz pink/purple twitching jig; "
                "Corky or Spin-N-Glo in pink/orange",
        "fly": "8 wt sink tip, pink or chartreuse Clouser or rabbit strip",
        "play": "Fresh fish push on the rise; fish the drop 1-3 days after",
        "timing": "first light",
        "extra": "Retaining salmon needs the non-tidal salmon stamp.",
    },
    "chum": {
        "prefer": ["E", "D"],
        "where": "Lower river and side-channel mouths",
        "gear": "Same spinners and jigs, chartreuse/pink; bigger profile is fine",
        "fly": "8 wt, chartreuse or pink streamer",
        "play": "Retention is still TBD on the DFO table: treat as catch and release",
        "timing": "any light",
        "extra": "",
    },
    "steelhead": {
        "prefer": ["D", "C", "E", "B"],
        "where": "Duncan through the middle river; tailouts and soft edges",
        "gear": "Float: 1/8 oz jig (1/4 high and coloured, 1/16 low and clear); unscented bubblegum "
                "pink worm; Corky/Spin-N-Glo #14 to #8, bigger as colour comes in; brass or silver Colorado",
        "fly": "Swing a sink tip: egg-sucking leech or intruder in black, purple or pink; stones in March",
        "play": "Fish the drop after a freshet; in high water work the soft edges close to shore",
        "timing": "all day, slow presentation in cold water",
        "extra": "Steelhead stamp; release wild fish. New log jams since 2025: scout drifts.",
    },
    "egg": {
        "prefer": ["A", "B", "C", "D", "E"],
        "where": "Chinook redds (Bible Camp up to the lake); tailouts and seams just below them",
        "gear": "Float with an unscented 8-10 mm bead pegged above a single barbless hook (gear water only)",
        "fly": "Indicator, single egg 8-10 mm in peach/apricot plus a pale cream; late Nov on, "
               "flesh fly (pale pink/tan bunny #4-6); streamers in low light",
        "play": "Stay below the redds and never wade on them; leave spawning browns alone",
        "timing": "eggs midday; streamers first and last light",
        "extra": "",
    },
    "spring": {
        "prefer": ["A", "B", "C"],
        "where": "Upper river (Greendale down to Skutz until Apr 15, then up to the lake)",
        "gear": "Small spinners or spoons in gear water",
        "fly": "White/pearl bead-head streamer or fry pattern #6-8; March Brown dries (Adams #12) "
               "12:30-2:30 from mid-April; golden stones in March",
        "play": "Big browns key on emerging salmon fry from late March",
        "timing": "streamers first and last light; dries midday",
        "extra": "",
    },
}

LINKS = ("DFO: https://www.pac.dfo-mpo.gc.ca/fm-gp/rec/fresh-douce/region1-eng.html\n"
         "BC: https://www2.gov.bc.ca/gov/content/sports-culture/recreation/fishing-hunting/fishing/fishing-regulations")
GAUGE_LINK = f"https://wateroffice.ec.gc.ca/report/real_time_e.html?stn={STATION_LOWER}"

# ============================ REG CALENDAR =================================


def in_range(d, start, end):
    md = (d.month, d.day)
    return start <= md <= end if start <= end else (md >= start or md <= end)


def open_sections(kind, d):
    """Open sections and rules for a species kind on date d (2025-2027 regs + DFO table)."""
    if in_range(d, (7, 15), (8, 31)):
        return []
    fall = in_range(d, (9, 1), (11, 15))           # fly only above Mile 66; closed below
    top_closed = in_range(d, (11, 15), (4, 15))    # weir to Greendale closed
    out = []
    if kind in ("trout", "steelhead"):
        if not top_closed:
            out.append(("A", "fly only" if fall else "gear OK"))
        out.append(("B", "fly only"))
        out.append(("C", "fly only" if fall else "gear OK"))
        if not fall:
            out += [("D", "gear OK"), ("E", "gear OK")]
    elif kind == "salmon":
        rule = "coho 1/day, others C&R" if in_range(d, (11, 1), (12, 31)) else "salmon C&R"
        if fall:
            rule += " [provincial table says closed to Nov 15: confirm with DFO Duncan 250-746-6221]"
        out.append(("D", rule))
        if not fall:
            out.append(("E", "coho 1/day, others C&R" if in_range(d, (11, 16), (12, 31)) else "salmon C&R"))
    return out

# ============================ DATA FETCHING ================================

UA = {"User-Agent": "cowichan-fishing-alert/2.0"}


def http_get(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8-sig", errors="replace")


def parse_dt(s):
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


def _flow_from_ogc(station, days):
    start = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = urllib.parse.urlencode({"f": "json", "STATION_NUMBER": station, "datetime": f"{start}/..",
                                "sortby": "DATETIME", "limit": 10000})
    data = json.loads(http_get(f"https://api.weather.gc.ca/collections/hydrometric-realtime/items?{q}"))
    return [(parse_dt(p["DATETIME"]), float(p["DISCHARGE"]))
            for p in (f.get("properties", {}) for f in data.get("features", []))
            if p.get("DISCHARGE") is not None and p.get("DATETIME")]


def _flow_from_datamart(station):
    for sub in ("today/hydrometric", "hydrometric"):
        for res in ("daily", "hourly"):
            url = f"https://dd.weather.gc.ca/{sub}/csv/BC/{res}/BC_{station}_{res}_hydrometric.csv"
            try:
                rows = list(csv.reader(io.StringIO(http_get(url))))
                hdr = [h.lower() for h in rows[0]]
                i_d = next(i for i, h in enumerate(hdr) if h.startswith("date"))
                i_q = next(i for i, h in enumerate(hdr) if "discharge" in h)
            except Exception:  # noqa: BLE001
                continue
            out = []
            for r in rows[1:]:
                try:
                    if r[i_q].strip():
                        out.append((parse_dt(r[i_d]), float(r[i_q])))
                except (ValueError, IndexError):
                    pass
            if out:
                return out
    return []


def fetch_flow(station, days=14):
    errors = []
    for fn in (lambda: _flow_from_ogc(station, days), lambda: _flow_from_datamart(station)):
        try:
            s = fn()
            if s:
                return hourly(sorted(s))
            errors.append("empty")
        except Exception as e:  # noqa: BLE001
            errors.append(str(e)[:120])
    raise RuntimeError(f"no discharge data for {station} ({'; '.join(errors)})")


def hourly(series):
    """Keep the last reading in each hour (5-minute data is overkill)."""
    out = {}
    for t, v in series:
        out[t.replace(minute=0, second=0, microsecond=0)] = (t, v)
    return [out[k] for k in sorted(out)]


def fetch_weather(lat, lon):
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "hourly": "precipitation,temperature_2m,pressure_msl,cloud_cover",
        "daily": "precipitation_sum,temperature_2m_max,temperature_2m_min,sunrise,sunset",
        "past_days": 14, "forecast_days": 7, "timezone": "America/Vancouver",
    })
    d = json.loads(http_get(f"https://api.open-meteo.com/v1/forecast?{q}"))
    h, dl = d["hourly"], d["daily"]
    hrs = [{"t": datetime.fromisoformat(t).replace(tzinfo=PT).astimezone(UTC),
            "precip": h["precipitation"][i] or 0.0, "temp": h["temperature_2m"][i],
            "pressure": h["pressure_msl"][i], "cloud": h["cloud_cover"][i]}
           for i, t in enumerate(h["time"])]
    days = {date.fromisoformat(t): {"precip": dl["precipitation_sum"][i] or 0.0,
                                    "tmax": dl["temperature_2m_max"][i], "tmin": dl["temperature_2m_min"][i],
                                    "sunrise": dl["sunrise"][i], "sunset": dl["sunset"][i]}
            for i, t in enumerate(dl["time"])}
    return {"hourly": hrs, "daily": days}

# ============================== ANALYSIS ===================================


@dataclass
class Conditions:
    day: date
    q_now: float
    trend_24: float
    phase: str
    flat_10d: bool
    rain_next24: float
    rain_next48: float
    tmin_24: float
    tmax_24: float
    pressure_trend: float
    cloud_day: float
    peak_7d: float = 0.0
    t_peak_7d: datetime | None = None
    weir_q: float | None = None
    weir_24: float | None = None
    weir_pulse: bool = False
    drought: bool = False
    rain_past72: float = 0.0
    ante14: float = 0.0
    sunrise: str = ""
    sunset: str = ""


def value_at(series, t):
    best = series[0][1]
    for ts, v in series:
        if ts > t:
            break
        best = v
    return best


def wsum(wx, t0, t1, key="precip"):
    return sum(h[key] or 0 for h in wx if t0 <= h["t"] < t1)


def wvals(wx, t0, t1, key):
    return [h[key] for h in wx if t0 <= h["t"] < t1 and h[key] is not None]


def classify(q, q24, peak, mn, hrs_peak):
    trend = (q - q24) / q24 if q24 else 0.0
    freshet = peak >= 1.5 * mn and peak >= 15
    if trend > 0.15:
        return "rising", trend
    if freshet and q < 0.9 * peak and 6 <= hrs_peak <= 120 and trend < 0.02:
        return "dropping after freshet", trend
    if trend < -0.10:
        return "dropping", trend
    return "stable", trend


def hhmm(iso):
    return iso[11:16] if iso and len(iso) >= 16 else "?"


def analyse(flow, weir, wxu, wxl, now):
    t_last, q_now = flow[-1]
    q24 = value_at(flow, t_last - timedelta(hours=24))
    last7 = [(t, v) for t, v in flow if t >= t_last - timedelta(days=7)] or flow
    t_peak, peak = max(last7, key=lambda x: x[1])
    mn = min(v for _, v in last7)
    hrs_peak = (t_last - t_peak).total_seconds() / 3600
    phase, trend = classify(q_now, q24, peak, mn, hrs_peak)
    last10 = [v for t, v in flow if t >= t_last - timedelta(days=10)]
    day = now.astimezone(PT).date()

    weir_q = weir_24 = None
    pulse = False
    if weir:
        weir_q = weir[-1][1]
        weir_24 = value_at(weir, weir[-1][0] - timedelta(hours=24))
        pulse = 5 <= day.month <= 11 and weir_q >= 1.3 * weir_24 and weir_q - weir_24 >= 2

    hl = wxl["hourly"]
    p0 = wvals(hl, now - timedelta(hours=7), now - timedelta(hours=5), "pressure")
    p1 = wvals(hl, now + timedelta(hours=5), now + timedelta(hours=7), "pressure")
    temps = wvals(hl, now, now + timedelta(hours=24), "temp") or [10.0]
    local = now.astimezone(PT)
    d0 = local.replace(hour=7, minute=0, second=0, microsecond=0)
    if local.hour >= 17:
        d0 += timedelta(days=1)
    clouds = wvals(hl, d0.astimezone(UTC), (d0 + timedelta(hours=10)).astimezone(UTC), "cloud") or [50]
    sun = wxl["daily"].get(d0.date(), {})
    hu = wxu["hourly"]
    return Conditions(
        day=day, q_now=q_now, trend_24=trend, phase=phase,
        flat_10d=bool(last10) and max(last10) < 1.25 * min(last10),
        rain_next24=wsum(hu, now, now + timedelta(hours=24)),
        rain_next48=wsum(hu, now, now + timedelta(hours=48)),
        tmin_24=min(temps), tmax_24=max(temps),
        pressure_trend=(p1[0] - p0[0]) if p0 and p1 else 0.0,
        cloud_day=sum(clouds) / len(clouds),
        peak_7d=peak, t_peak_7d=t_peak, weir_q=weir_q, weir_24=weir_24, weir_pulse=pulse,
        drought=day.month in (9, 10) and q_now < DROUGHT_FLOW,
        rain_past72=wsum(hu, now - timedelta(hours=72), now),
        ante14=wsum(hu, now - timedelta(days=14), now),
        sunrise=hhmm(sun.get("sunrise", "")), sunset=hhmm(sun.get("sunset", "")),
    )


def season_key(d):
    return d.year if d.month >= 9 else d.year - 1


def detect_fall_freshet(flow, weir, season_start):
    """First big fall flow increase (rain freshet or weir pulse) since Sep 1."""
    hits = []
    for t, q in flow:
        if t >= season_start and q >= 15:
            window = [v for tt, v in flow if t - timedelta(hours=72) <= tt <= t]
            if q >= 1.6 * min(window):
                hits.append(t)
                break
    for t, q in weir:
        if t >= season_start:
            q24 = value_at(weir, t - timedelta(hours=24))
            if q >= 1.3 * q24 and q - q24 >= 2:
                hits.append(t)
                break
    return min(hits) if hits else None


def egg_activation(freshet, d):
    """Egg bite ramps up as chinook get onto the redds after the first fall freshet."""
    if freshet is None:
        return 0.3
    days = (d - freshet.astimezone(PT).date()).days
    return 0.4 if days < 3 else 0.7 if days < 7 else 1.0

# =============================== SCORING ===================================


@dataclass
class Score:
    sp: dict
    score: float
    why: list
    heads: list = field(default_factory=list)   # (key, text)
    opens: list = field(default_factory=list)


def score_species(sp, c, egg_factor=1.0):
    d = c.day
    w = sp["season"].get(d.month, 0.0)
    if w == 0:
        return Score(sp, 0.0, ["out of season"])
    opens = open_sections(sp["kind"], d)
    if not opens:
        return Score(sp, 0.0, ["closed by regulation"])
    if sp["style"] == "egg":
        w *= egg_factor
    lo, hi = sp["good_flow"]
    flo, fhi = sp["fishable_flow"]
    q, ph = c.q_now, c.phase
    why, heads = [], []

    if lo <= q <= hi:
        s = 5.0
        why.append(f"flow in sweet spot ({lo}-{hi})")
    elif flo <= q <= fhi:
        s = 2.5
        why.append(f"flow fishable but {'low' if q < lo else 'high'}")
    else:
        s = 0.0
        why.append("too low" if q < flo else "blown out")

    style = sp["style"]
    if style == "freshet":
        if ph == "dropping after freshet":
            s += 3.5
            why.append("dropping and clearing after a freshet")
        elif (ph == "rising" or c.weir_pulse) and q <= hi:
            s += 1.5
            why.append("fresh fish moving" + (" on a weir release" if c.weir_pulse else ""))
        elif ph == "dropping":
            s += 1.5
        elif ph == "stable":
            s += 0.5
            if c.flat_10d and q < lo:
                s -= 1.0
                why.append("low and flat 10+ days, fish stale")
        if 5 <= c.rain_next24 < 25:
            s += 0.5
            why.append("light rain on the way")
    elif style == "egg":
        if ph in ("dropping", "dropping after freshet"):
            s += 2.5
            why.append("settling flows, eggs drifting")
        elif ph == "stable":
            s += 2.0
            why.append("steady flow")
        elif ph == "rising":
            s -= 0.5
            why.append("rising, colour coming in")
    else:
        if ph == "stable":
            s += 2.5
            why.append("stable flow")
        elif ph in ("dropping", "dropping after freshet"):
            s += 1.5
            why.append("flow settling")
        elif ph == "rising":
            s -= 1.5
            why.append("flow rising, likely coloured")
        if c.cloud_day >= 70:
            s += 0.5
            why.append("overcast")

    if sp["key"] == "coho" and 28 <= q <= 45 and (ph in ("rising", "dropping after freshet") or c.weir_pulse):
        s += 0.5
        why.append("prime push flows")
    if sp["key"] == "spring" and in_range(d, (3, 20), (5, 31)):
        s += 0.5
        why.append("salmon fry out, browns on streamers")
    if sp["key"] == "steelhead" and hi < q <= fhi:
        why.append("high: fish the soft edges close to shore")
    if c.pressure_trend <= -3:
        s += 0.5
        why.append("falling barometer")
    if c.rain_next24 >= 40:
        s -= 1.5
        why.append("heavy rain next 24h")
    if sp["cold_limit"] is not None and c.tmin_24 < sp["cold_limit"]:
        s -= 2.0
        why.append("cold snap")
    if sp["heat_limit"] is not None and c.tmax_24 > sp["heat_limit"]:
        s -= 2.0
        why.append("hot, warm-water stress")

    s = max(0.0, min(10.0, s)) * w
    if not (flo <= q <= fhi):
        s = min(s, 2.0)
    if c.drought and d.month in (9, 10):
        s = min(s, 4.0)
        why.append("drought hold")
    if style == "egg" and egg_factor < 1:
        why.append("egg bite still building" if egg_factor >= 0.4 else "no fall freshet yet")
    if sp["season"].get(d.month, 0) < 1:
        why.append(f"shoulder season x{sp['season'][d.month]}")

    if style == "freshet" and sp["season"].get(d.month, 0) >= 0.6:
        if q > hi and c.rain_next48 < 10 and q <= fhi * 1.5:
            heads.append((f"{sp['key']}:window",
                          f"{sp['name']}: window likely in ~1-3 days as flow drops into {lo}-{hi}"))
        elif q < lo and c.rain_next48 >= 15:
            heads.append((f"{sp['key']}:freshen",
                          f"{sp['name']}: {c.rain_next48:.0f} mm coming should freshen the river, watch for fish pushing in 24-48h"))
    if s >= GOOD and c.rain_next24 >= 40:
        heads.append((f"{sp['key']}:blowout", f"{sp['name']}: good now but heavy rain likely blows it out by tomorrow"))
    return Score(sp, round(s, 1), why, heads, opens)


def tier(s):
    return "GOOD" if s >= GOOD else "FAIR" if s >= FAIR else "POOR"


def strategy_card(sc, c):
    card = CARDS[sc.sp["key"]]
    by = dict(sc.opens)
    picks = [k for k in card["prefer"] if k in by][:2]
    if not picks:
        return ""
    lines = [f"{sc.sp['name']} plan:"]
    lines += [f"  Where: {k} {SECTIONS[k]} ({by[k]})" for k in picks]
    lines.append(f"  Spots: {card['where']}")
    gear_ok = any("fly only" not in by[k] for k in picks)
    if gear_ok:
        lines.append(f"  Gear: {card['gear']}")
    lines.append(f"  Fly: {card['fly']}")
    lines.append(f"  Play: {card['play']}")
    lines.append(f"  When: {card['timing']}; sunrise {c.sunrise}, sunset {c.sunset}")
    if card["extra"]:
        lines.append(f"  Note: {card['extra']}")
    return "\n".join(lines)

# ============================ WEEKEND OUTLOOK ==============================


def daily_means(flow):
    by = {}
    for t, v in flow:
        by.setdefault(t.astimezone(PT).date(), []).append(v)
    return {d: sum(v) / len(v) for d, v in by.items()}


def outlook_days(today):
    start = today + timedelta(days=(4 - today.weekday()) % 7)   # next Friday (or today)
    if today.weekday() in (5, 6):
        start = today
    return [d for d in (start + timedelta(days=i) for i in range(3)) if d.weekday() >= 4 or d == start]


def build_outlook(c, flow, wxu, wxl, freshet):
    today = c.day
    days = outlook_days(today)
    rain = {d: v["precip"] for d, v in wxu["daily"].items()}
    hist = daily_means(flow)
    series = {d: v for d, v in hist.items() if d < today}
    series[today] = c.q_now
    m = today.month
    if m in (9, 10):
        k = PROJ["k_wet_fall"] if c.ante14 >= PROJ["wet_fall_mm14"] else PROJ["k_dry_fall"]
    elif m in (11, 12, 1, 2, 3):
        k = PROJ["k_winter"]
    else:
        k = PROJ["k_other"]
    base = c.weir_q if c.weir_q else min(c.q_now, 10)
    d, q = today, c.q_now
    while d < days[-1]:
        d += timedelta(days=1)
        q = base + (q - base) * PROJ["recession"] + k * (0.5 * rain.get(d, 0) + 0.5 * rain.get(d - timedelta(days=1), 0))
        series[d] = q

    hl = wxl["hourly"]
    lines = ["Weekend outlook (flow projection is rough until calibrated)"]
    best = None
    for d in days:
        if d not in wxl["daily"]:
            continue
        prev = [series.get(d - timedelta(days=i)) for i in range(1, 8)]
        prev = [p for p in prev if p is not None]
        q = series[d]
        q24 = prev[0] if prev else q
        window = [q] + prev[:6]
        peak = max(window)
        hrs_peak = 24 * window.index(peak)
        phase, trend = classify(q, q24, peak, min(window), hrs_peak)
        noon = datetime(d.year, d.month, d.day, 12, tzinfo=PT).astimezone(UTC)
        prs = wvals(hl, noon - timedelta(hours=24), noon - timedelta(hours=23), "pressure")
        prn = wvals(hl, noon, noon + timedelta(hours=1), "pressure")
        clouds = wvals(hl, noon - timedelta(hours=5), noon + timedelta(hours=5), "cloud") or [50]
        dl = wxl["daily"][d]
        cd = Conditions(
            day=d, q_now=q, trend_24=trend, phase=phase,
            flat_10d=max(window) < 1.25 * min(window), rain_next24=rain.get(d, 0),
            rain_next48=rain.get(d, 0) + rain.get(d + timedelta(days=1), 0),
            tmin_24=dl["tmin"], tmax_24=dl["tmax"],
            pressure_trend=(prn[0] - prs[0]) / 2 if prs and prn else 0.0,
            cloud_day=sum(clouds) / len(clouds), weir_q=base,
            drought=d.month in (9, 10) and q < DROUGHT_FLOW,
            sunrise=hhmm(dl["sunrise"]), sunset=hhmm(dl["sunset"]))
        scores = [score_species(sp, cd, egg_activation(freshet, d)) for sp in SPECIES]
        live = sorted([s for s in scores if s.score > 0], key=lambda s: -s.score)
        lines.append(f"{d.strftime('%a %b %d')}: ~{q:.0f} m3/s {phase} | rain {rain.get(d, 0):.0f} mm | "
                     f"{dl['tmin']:.0f} to {dl['tmax']:.0f} C")
        lines.append("   " + (", ".join(f"{s.sp['name']} {s.score} {tier(s.score)}" for s in live[:3]) or "nothing open or in season"))
        if live and (best is None or live[0].score > best[1].score):
            best = (d, live[0], cd)
    if best and best[1].score >= FAIR:
        d, sc, cd = best
        title = f"Cowichan weekend: best is {d.strftime('%a')} ({sc.sp['name']} {sc.score})"
        lines += ["", f"Best bet: {d.strftime('%a')}, {sc.sp['name']} {sc.score}", strategy_card(sc, cd)]
    else:
        title = "Cowichan weekend: nothing worth the drive"
    lines += ["", f"Rules: {STANDING_RULES}"]
    return title, "\n".join(lines)

# ============================ REG WATCHER ==================================


class TableParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def table_rows(page):
    p = TableParser()
    p.feed(page)
    return p.rows


def dfo_cowichan_rows(page):
    rows = table_rows(page)
    for i, r in enumerate(rows):
        if r and r[0].lower() == "cowichan river":
            block = [r]
            for nxt in rows[i + 1:]:
                if nxt and nxt[0].strip():
                    break
                block.append(nxt)
            out, area = [], ""
            for b in block:
                cells = b + [""] * (5 - len(b))
                area = cells[1] or area   # carry the section down to continuation rows
                short = area.replace("from the ", "").replace("from ", "")[:45]
                out.append(" | ".join(x for x in [short] + cells[2:] if x))
            return out
    return None


def bc_cowichan_rows(page):
    rows = [" | ".join(r) for r in table_rows(page) if any("cowichan" in x.lower() for x in r)]
    return rows if table_rows(page) else None


def check_regs(state, now):
    msgs = []
    regs = state.setdefault("regs", {})
    for src, url, parse in (("DFO salmon table", DFO_REGS_URL, dfo_cowichan_rows),
                            ("BC in-season changes", BC_REGS_URL, bc_cowichan_rows)):
        st = regs.setdefault(src, {"rows": None, "fails": 0, "alerted": False})
        try:
            rows = parse(http_get(url))
            if rows is None:
                raise ValueError("Cowichan rows not found (page layout may have changed)")
        except Exception as e:  # noqa: BLE001
            st["fails"] += 1
            print(f"reg watcher: {src} failed ({e})")
            if st["fails"] >= REG_FAILS_BEFORE_ALERT and not st["alerted"]:
                st["alerted"] = True
                msgs.append((f"Cowichan regs watcher can't read {src}",
                             f"{st['fails']} failed checks in a row: {str(e)[:200]}\nCheck manually:\n{url}", "3"))
            continue
        st["fails"], st["alerted"] = 0, False
        old = st["rows"]
        st["rows"] = rows
        if old is None:
            print(f"reg watcher: baseline captured for {src} ({len(rows)} rows)")
            continue
        if rows != old:
            added = [r for r in rows if r not in old]
            removed = [r for r in old if r not in rows]
            body = [f"{src} changed for the Cowichan:"]
            body += [f"+ {r}" for r in added] + [f"- {r}" for r in removed]
            body += ["", url]
            msgs.append(("Cowichan regulations changed", "\n".join(body), "4"))
    return msgs

# ============================== ALERTING ===================================


def build_report(c, scores, general_heads):
    peak = c.t_peak_7d.astimezone(PT).strftime("%a %H:%M") if c.t_peak_7d else "?"
    lines = [f"Flow {STATION_LOWER}: {c.q_now:.1f} m3/s ({c.trend_24:+.0%} 24h), 7d peak {c.peak_7d:.0f} @ {peak}",
             f"Phase: {c.phase}"]
    if c.weir_q is not None:
        lines.append(f"Weir {STATION_WEIR}: {c.weir_q:.1f} m3/s" +
                     (f" (raised from {c.weir_24:.1f})" if c.weir_pulse else ""))
    lines += [f"Rain (Lake Cowichan): 72h {c.rain_past72:.0f} mm | next 24h {c.rain_next24:.0f} | next 48h {c.rain_next48:.0f}",
              f"Air {c.tmin_24:.0f} to {c.tmax_24:.0f} C | barometer {c.pressure_trend:+.0f} hPa/12h | "
              f"cloud {c.cloud_day:.0f}% | sun {c.sunrise}-{c.sunset}"]
    if c.drought:
        lines.append("Drought hold: river is too low for fair fishing; guides advise staying off.")
    lines.append("")
    icon = {"GOOD": "✅", "FAIR": "🟡", "POOR": "⚪"}
    live = sorted([s for s in scores if s.why not in (["out of season"], ["closed by regulation"])],
                  key=lambda s: -s.score)
    for s in live:
        lines.append(f"{icon[tier(s.score)]} {s.sp['name']} {s.score} {tier(s.score)}: {', '.join(s.why)}")
    heads = general_heads + [h for s in scores for h in s.heads]
    if heads:
        lines += ["", "Heads-up:"] + [f"• {t}" for _, t in heads]
    return lines, live, heads


def decide(state, scores, heads, live):
    prev = state.get("tiers", {})
    up = [s for s in scores if tier(s.score) == "GOOD" and prev.get(s.sp["key"]) != "GOOD"]
    down = [s for s in scores if tier(s.score) != "GOOD" and prev.get(s.sp["key"]) == "GOOD"]
    old_heads = set(state.get("heads", []))
    new_heads = [h for h in heads if h[0] not in old_heads]
    state["tiers"] = {s.sp["key"]: tier(s.score) for s in scores}
    state["heads"] = [h[0] for h in heads]
    good = [s for s in scores if tier(s.score) == "GOOD"]
    if up:
        title, pr = f"Cowichan GOOD: {', '.join(s.sp['name'] for s in good)}", "4"
    elif new_heads:
        title, pr = f"Cowichan heads-up: {new_heads[0][1].split(':')[0]}", "3"
    elif down:
        title, pr = f"Cowichan: {', '.join(s.sp['name'] for s in down)} window closing", "2"
    else:
        best = live[0] if live else None
        title = f"Cowichan: best is {best.sp['name']} {best.score}" if best else "Cowichan: nothing open or in season"
        pr = "2"
    changes = [f"{s.sp['name']}: {prev.get(s.sp['key'], 'new')} -> GOOD" for s in up]
    changes += [f"{s.sp['name']}: GOOD -> {tier(s.score)}" for s in down]
    return title, pr, bool(up or down or new_heads), changes


def fit(body, limit=MAX_PUSH_BYTES):
    if len(body.encode()) <= limit:
        return body
    note = "\n…(trimmed; full report in the GitHub run summary)"
    out = body.encode()[: limit - len(note.encode())].decode("utf-8", errors="ignore")
    return out.rsplit("\n", 1)[0] + note


def send_ntfy(title, body, priority):
    if not NTFY_TOPIC:
        print(f"[no NTFY_TOPIC] would push: {title}")
        return
    req = urllib.request.Request(
        f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}", data=fit(body).encode("utf-8"), method="POST",
        headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": priority,
                 "Tags": "fish", "Click": GAUGE_LINK})
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"ntfy: HTTP {r.status} for '{title}'")


def flush(outbox, dry_run):
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    for title, body, pr in outbox:
        print(f"\n=== {title} [p{pr}] ===\n{body}")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(f"## {title}\n\n```\n{body}\n```\n")
        if not dry_run:
            send_ntfy(title, body, pr)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True, default=str)

# =============================== DEMOS =====================================


def _demo(now, flow_fn, weir_fn, rain_fn, temp=8.0):
    flow = [(now - timedelta(hours=h), flow_fn(h)) for h in range(14 * 24, -1, -1)]
    weir = [(now - timedelta(hours=h), weir_fn(h)) for h in range(3 * 24, -1, -1)]
    hrs = [{"t": now + timedelta(hours=i), "precip": rain_fn(i), "temp": temp + 4 * (10 <= (i % 24) <= 16),
            "pressure": 1015 - 0.15 * i, "cloud": 80} for i in range(-14 * 24, 7 * 24)]
    days = {}
    for h in hrs:
        d = h["t"].astimezone(PT).date()
        e = days.setdefault(d, {"precip": 0.0, "tmax": -99, "tmin": 99,
                                "sunrise": f"{d}T07:35", "sunset": f"{d}T17:50"})
        e["precip"] += h["precip"]
        e["tmax"], e["tmin"] = max(e["tmax"], h["temp"]), min(e["tmin"], h["temp"])
    wx = {"hourly": hrs, "daily": days}
    return flow, weir, wx, wx


def _pt(s):
    return datetime.fromisoformat(s).replace(tzinfo=PT).astimezone(UTC)


DEMOS = {
    # Mid-November: 40 mm storm two days ago, now dropping and clearing
    "nov-drop": lambda: ("2026-11-18T15:00",
                         lambda h: 10 if h > 72 else 10 + (72 - h) * 3.2 if h > 50 else 80 * 0.982 ** (50 - h),
                         lambda h: 20.0, lambda i: 2.0 if -72 <= i <= -50 else 0.3 if 0 <= i < 24 else 0.0),
    # Late October drought year: weir release raised from 5.5 to 10 overnight
    "oct-pulse": lambda: ("2026-10-20T07:00",
                          lambda h: 6.2 if h > 18 else 10.5, lambda h: 5.5 if h > 20 else 10.0, lambda i: 0.0),
    # October drought: flat 5.5 m3/s
    "drought": lambda: ("2026-10-05T07:00", lambda h: 5.5, lambda h: 5.5, lambda i: 0.0),
    # February: freshet to 120 two and a half days ago, now ~65 and dropping
    "feb-steel": lambda: ("2027-02-10T07:00",
                          lambda h: 35 if h > 84 else 35 + (84 - h) * 3.6 if h > 60 else 120 * 0.99 ** (60 - h),
                          lambda h: 40.0, lambda i: 1.5 if -84 <= i <= -60 else 0.0, ),
    # Thursday afternoon, low steady river, 30 mm due Friday then drying out
    "thursday": lambda: ("2026-10-22T16:00", lambda h: 12.0, lambda h: 9.0,
                         lambda i: 1.5 if 10 <= i < 30 else 0.2 if 30 <= i < 48 else 0.0),
}

# =============================== MAIN ======================================


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--weekend", action="store_true", help="force the weekend outlook")
    ap.add_argument("--skip-regs", action="store_true")
    ap.add_argument("--demo", choices=sorted(DEMOS))
    args = ap.parse_args()

    # Dry runs never touch state; demos only persist to an explicitly set STATE_FILE.
    persist = not args.dry_run and (not args.demo or "STATE_FILE" in os.environ)
    state = load_state() if (persist or "STATE_FILE" in os.environ) else {}
    now = datetime.now(UTC)
    outbox = []
    tag = "[DEMO] " if args.demo else ""

    regs_local = DFO_REGS_URL.startswith("file:")
    if not args.skip_regs and (not args.demo or regs_local):
        outbox += check_regs(state, now)

    fail = state.setdefault("fail", {"count": 0, "alerted_at": None})
    try:
        if args.demo:
            when, ffn, wfn, rfn = DEMOS[args.demo]()
            now = _pt(when)
            flow, weir, wxu, wxl = _demo(now, ffn, wfn, rfn)
        else:
            flow = fetch_flow(STATION_LOWER)
            age = (now - flow[-1][0]).total_seconds() / 3600
            if age > STALE_HOURS:
                raise RuntimeError(f"gauge {STATION_LOWER} data is stale (last reading {age:.0f} h ago)")
            try:
                weir = fetch_flow(STATION_WEIR, days=3)
            except Exception as e:  # noqa: BLE001
                print(f"weir fetch failed (continuing): {e}")
                weir = []
            wxu, wxl = fetch_weather(*WX_UPPER), fetch_weather(*WX_LOWER)
    except Exception as e:  # noqa: BLE001
        fail["count"] += 1
        last = fail.get("alerted_at")
        if not last or now - datetime.fromisoformat(last) > timedelta(hours=24):
            fail["alerted_at"] = now.isoformat()
            outbox.append(("Cowichan alert: data problem",
                           f"Run failed ({fail['count']} in a row): {str(e)[:300]}\n"
                           f"No fishing alerts until this clears.\nGauge: {GAUGE_LINK}", "3"))
        flush(outbox, args.dry_run)
        if persist:
            save_state(state)
        return
    if fail.get("alerted_at"):
        outbox.append(("Cowichan alert: data feeds back", "Data is flowing again; alerts resumed.", "2"))
    state["fail"] = {"count": 0, "alerted_at": None}

    c = analyse(flow, weir, wxu, wxl, now)

    sk = season_key(c.day)
    season = state.get("season", {})
    if season.get("key") != sk:
        season = {"key": sk, "fall_freshet": None}
    det = detect_fall_freshet(flow, weir, datetime(sk, 9, 1, tzinfo=PT).astimezone(UTC))
    rec = datetime.fromisoformat(season["fall_freshet"]) if season.get("fall_freshet") else None
    if det and (rec is None or det < rec):
        rec = det
    season["fall_freshet"] = rec.isoformat() if rec else None
    state["season"] = season

    scores = [score_species(sp, c, egg_activation(rec, c.day)) for sp in SPECIES]
    general = []
    if c.weir_pulse and c.day.month in (9, 10, 11):
        general.append(("weir:pulse", f"Weir release raised {c.weir_24:.1f} to {c.weir_q:.1f} m3/s: salmon will "
                                      "move; the trout egg bite follows once chinook are on the redds"))
    lines, live, heads = build_report(c, scores, general)
    title, pr, changed, changes = decide(state, scores, heads, live)

    if changes:
        lines[0:0] = ["Changed: " + "; ".join(changes)]
    cards = [s for s in live if tier(s.score) == "GOOD"][:2]
    if not cards:
        keys = {h[0].split(":")[0] for h in heads}
        cards = [s for s in live if s.sp["key"] in keys][:1]
    for s in cards:
        card = strategy_card(s, c)
        if card:
            lines += ["", card]
    lines += ["", f"Rules: {STANDING_RULES}"]
    if c.day > REGS_VALID_UNTIL:
        lines.append("WARNING: regulation calendar is from the 2025-2027 synopsis; update open_sections().")
    lines += [LINKS]
    body = "\n".join(lines)

    if changed or ALWAYS_SEND:
        outbox.append((tag + title, body, pr))
    else:
        print(f"No change worth a push ({title}).")
        summary = os.getenv("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(f"## {title} (no push)\n\n```\n{body}\n```\n")

    local = now.astimezone(PT)
    due = local.weekday() == OUTLOOK_WEEKDAY and local.hour >= OUTLOOK_HOUR
    if args.weekend or (due and state.get("last_outlook") != local.date().isoformat()):
        ot, ob = build_outlook(c, flow, wxu, wxl, rec)
        outbox.append((tag + ot, ob, "3"))
        state["last_outlook"] = local.date().isoformat()

    state["last_run"] = now.isoformat()
    flush(outbox, args.dry_run)
    if persist:
        save_state(state)


if __name__ == "__main__":
    main()
