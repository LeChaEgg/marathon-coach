"""Hourly weather from Open-Meteo (no key), cached per location, averaged over each run."""
import datetime as dt
import json
import math
import socket
import urllib.error
import urllib.parse
import urllib.request

from .config import DATA
from .util import mean, parse_date, today

D_WEATHER = DATA / "weather"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"
FIELDS = ["temperature_2m", "relative_humidity_2m", "dew_point_2m", "apparent_temperature",
          "wind_speed_10m", "shortwave_radiation"]
KEYS = ["temp", "rh", "dew", "feels", "wind", "rad"]
FINAL_LAG_DAYS = 7  # archive (ERA5) values older than this are treated as final
AWAY_KM = 30

# Hadley temperature + dew point rule (°F sum -> % pace slowdown)
HADLEY = [(100, 0.0), (110, 0.25), (120, 0.75), (130, 1.5), (140, 2.5), (150, 3.75),
          (160, 5.25), (170, 7.0), (180, 9.0)]


class WeatherError(Exception):
    pass


def heat_adj(temp_c, dew_c, scale=1.0):
    """Fractional speed slowdown caused by heat/humidity (0.03 = 3%)."""
    if temp_c is None or dew_c is None:
        return 0.0
    s = temp_c * 9 / 5 + 32 + dew_c * 9 / 5 + 32
    if s <= HADLEY[0][0]:
        return 0.0
    for (x0, y0), (x1, y1) in zip(HADLEY, HADLEY[1:]):
        if s <= x1:
            return scale * (y0 + (y1 - y0) * (s - x0) / (x1 - x0)) / 100
    return scale * min(12.0, HADLEY[-1][1] + 0.2 * (s - HADLEY[-1][0])) / 100


def _km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def location_for(detail, cfg):
    """(lat, lon, source). GPS start point only when clearly away from home."""
    loc = cfg["location"]
    home = (loc["lat"], loc["lon"])
    start = (detail or {}).get("start_latlng")
    if start and _km(home, start) > AWAY_KM:
        return round(start[0], 2), round(start[1], 2), "GPS起点"
    return home[0], home[1], loc.get("name", "默认位置")


def _path(lat, lon):
    return D_WEATHER / f"{lat:.3f}_{lon:.3f}.json"


def load(lat, lon):
    try:
        return json.loads(_path(lat, lon).read_text())
    except FileNotFoundError:
        return {"hours": {}, "final_until": None}


def _save(lat, lon, obj):
    p = _path(lat, lon)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(p)


def _get(url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "tokyo-marathon-coach/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise WeatherError(f"Open-Meteo HTTP {e.code}") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, socket.gaierror):
            raise WeatherError("Open-Meteo DNS 解析失败：sandbox 网络受限，请以 full_network 权限重试") from None
        raise WeatherError(f"Open-Meteo 网络错误：{e.reason}") from None
    except (TimeoutError, socket.timeout):
        raise WeatherError("Open-Meteo 超时") from None


def _merge(cache, payload):
    h = payload.get("hourly") or {}
    times = h.get("time") or []
    n = 0
    for i, t in enumerate(times):
        row = [(h.get(f) or [None] * len(times))[i] for f in FIELDS]
        if row[0] is None:
            continue
        cache["hours"][t] = row
        n += 1
    return n


def ensure(lat, lon, dates):
    """Make sure hourly weather exists for all dates (YYYY-MM-DD). Returns number of hours fetched."""
    cache = load(lat, lon)
    t = today()
    final_until = parse_date(cache["final_until"]) if cache.get("final_until") else None
    have = {k[:10] for k in cache["hours"]}
    want = sorted({str(d)[:10] for d in dates})
    recent_cut = t - dt.timedelta(days=FINAL_LAG_DAYS)
    old = [d for d in want if parse_date(d) <= recent_cut and (d not in have or not final_until or parse_date(d) > final_until)]
    recent = [d for d in want if parse_date(d) > recent_cut]
    base = {"latitude": lat, "longitude": lon, "hourly": ",".join(FIELDS), "timezone": "auto"}
    n = 0
    if old:
        n += _merge(cache, _get(ARCHIVE, {**base, "start_date": old[0], "end_date": old[-1]}))
        cache["final_until"] = str(max(parse_date(old[-1]), final_until or parse_date(old[-1])))
    if recent:
        past = (t - parse_date(recent[0])).days + 1
        n += _merge(cache, _get(FORECAST, {**base, "past_days": min(92, past), "forecast_days": 1}))
    if n:
        _save(lat, lon, cache)
    return n


def conditions(act, lat, lon, source, cache=None):
    """Mean weather over the run window; None if not cached."""
    start = act.get("start_date_local")
    if not start:
        return None
    cache = cache or load(lat, lon)
    t0 = dt.datetime.fromisoformat(start[:19])
    t1 = t0 + dt.timedelta(seconds=act.get("elapsed_time") or act.get("moving_time") or 0)
    rows = []
    h = t0.replace(minute=0, second=0)
    while h <= t1:
        r = cache["hours"].get(h.strftime("%Y-%m-%dT%H:00"))
        if r:
            rows.append(r)
        h += dt.timedelta(hours=1)
    if not rows:
        return None
    out = {k: mean([r[i] for r in rows]) for i, k in enumerate(KEYS)}
    out["loc"] = source
    out["lat"], out["lon"] = lat, lon
    out["final"] = bool(cache.get("final_until")) and start[:10] <= cache["final_until"]
    return out
