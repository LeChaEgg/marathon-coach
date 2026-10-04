"""Local cache of Intervals.icu data with incremental sync."""
import datetime as dt
import gzip
import json
import sys
import time

from . import weather
from .api import ApiError, Intervals
from .config import DATA, JOURNAL, load_config
from .metrics import METRICS_VERSION, analyze_run, is_placeholder, is_run, score_key
from .util import parse_date, today

STREAM_TYPES = ["time", "distance", "heartrate", "velocity_smooth", "cadence", "altitude", "fixed_altitude",
                "watts", "stance_time", "vertical_oscillation", "step_length", "latlng"]

F_ACTS = DATA / "activities.json"
F_WELL = DATA / "wellness.json"
F_EVENTS = DATA / "events.json"
F_STATE = DATA / "sync_state.json"
F_METRICS = DATA / "run_metrics.json"
D_DETAILS = DATA / "details"
D_STREAMS = DATA / "streams"
F_FEEDBACK = JOURNAL / "feedback.jsonl"

DROP_KEYS = {"skyline_chart_bytes", "icu_athlete_id", "external_id", "oauth_client_id", "file_sport_index"}


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def _save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(path)


def log(msg):
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------- readers

def activities():
    """dict id -> activity, sorted by start time"""
    acts = _load(F_ACTS, {})
    return dict(sorted(acts.items(), key=lambda kv: kv[1].get("start_date_local") or ""))


def wellness():
    return _load(F_WELL, {})


def events():
    return _load(F_EVENTS, [])


def detail(aid):
    return _load(D_DETAILS / f"{aid}.json", None)


def streams(aid):
    p = D_STREAMS / f"{aid}.json.gz"
    if not p.exists():
        return None
    with gzip.open(p, "rt") as f:
        return json.load(f)


def run_metrics():
    return _load(F_METRICS, {})


def feedback():
    if not F_FEEDBACK.exists():
        return []
    out = []
    for line in F_FEEDBACK.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def type_overrides():
    ov = {}
    for fb in feedback():
        if fb.get("activity") and fb.get("type"):
            ov[fb["activity"]] = fb["type"]
    return ov


def add_feedback(entry):
    F_FEEDBACK.parent.mkdir(parents=True, exist_ok=True)
    with open(F_FEEDBACK, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def state():
    return _load(F_STATE, {})


# ---------------------------------------------------------------- sync

def _start_latlng(streams):
    for x in streams or []:
        if x.get("type") == "latlng" and x.get("data") and x.get("data2"):
            for la, lo in zip(x["data"], x["data2"]):
                if la is not None and lo is not None:
                    return [round(la, 3), round(lo, 3)]
    return None


def sync_weather(acts, cfg, quiet=False):
    """Fetch hourly weather for every run date; failures only warn."""
    by_loc = {}
    s_since = cfg["sync"].get("streams_since", cfg["sync"]["history_start"])
    for a in acts.values():
        if not is_run(a) or is_placeholder(a) or (a.get("start_date_local") or "")[:10] < s_since:
            continue
        lat, lon, _ = weather.location_for(detail(a["id"]), cfg)
        by_loc.setdefault((lat, lon), set()).add(a["start_date_local"][:10])
    n = 0
    for (lat, lon), dates in by_loc.items():
        try:
            n += weather.ensure(lat, lon, dates)
        except weather.WeatherError as e:
            log(f"  天气读取失败（评分将不做温湿度修正）：{e}")
            return n, str(e)
    if n and not quiet:
        log(f"  天气：更新 {n} 小时（{len(by_loc)} 个位置）")
    return n, None


def _merge_window(cache, fresh, oldest, newest, key_date):
    fresh_ids = {x["id"] for x in fresh}
    removed = 0
    for k in list(cache):
        dd = key_date(cache[k])
        if dd and oldest <= dd <= newest and k not in fresh_ids:
            del cache[k]
            removed += 1
    added = sum(1 for x in fresh if x["id"] not in cache)
    for x in fresh:
        cache[x["id"]] = {k: v for k, v in x.items() if k not in DROP_KEYS}
    return added, removed


def sync(full=False, fetch_streams=True, quiet=False):
    cfg = load_config()
    sc = cfg["sync"]
    st = state()
    api = Intervals()
    t = today()
    newest = t + dt.timedelta(days=1)
    overlap = dt.timedelta(days=sc.get("overlap_days", 10))
    hist = parse_date(sc["history_start"])

    # activities
    acts = {} if full else _load(F_ACTS, {})
    a_old = hist if (full or not st.get("activities_synced")) else max(hist, parse_date(st["activities_synced"]) - overlap)
    fresh = api.activities(a_old, newest)
    added, removed = _merge_window(acts, fresh, a_old, newest,
                                   lambda a: parse_date(a["start_date_local"]) if a.get("start_date_local") else None)
    _save(F_ACTS, acts)
    st["activities_synced"] = str(t)

    # wellness
    well = {} if full else _load(F_WELL, {})
    w_old = hist if (full or not st.get("wellness_synced")) else max(hist, parse_date(st["wellness_synced"]) - overlap)
    wl = api.wellness(w_old, newest)
    for w in wl:
        well[w["id"]] = w
    _save(F_WELL, dict(sorted(well.items())))
    st["wellness_synced"] = str(t)

    # planned events (read-only)
    try:
        ev = api.events(t - dt.timedelta(days=7), t + dt.timedelta(days=21))
        _save(F_EVENTS, [{k: e.get(k) for k in ("id", "start_date_local", "category", "type", "name",
                                                  "description", "moving_time", "distance", "icu_training_load")}
                         for e in ev])
    except ApiError as e:
        log(f"  events 读取失败（忽略）：{e}")

    # details + streams for runs
    n_det = 0
    missing = []
    s_since = sc.get("streams_since", sc["history_start"])
    if fetch_streams:
        D_DETAILS.mkdir(parents=True, exist_ok=True)
        D_STREAMS.mkdir(parents=True, exist_ok=True)
        todo = []
        for a in acts.values():
            if not is_run(a) or (a.get("start_date_local") or "")[:10] < s_since:
                continue
            if is_placeholder(a):
                missing.append(a["id"])
                continue
            det = detail(a["id"])
            if det is None or det.get("analyzed") != a.get("analyzed") or not (D_STREAMS / f"{a['id']}.json.gz").exists():
                todo.append(a)
        if todo and not quiet:
            log(f"  下载 {len(todo)} 个跑步的分段与逐秒数据…")
        for k, a in enumerate(todo, 1):
            try:
                d = api.activity(a["id"])
                s = api.streams(a["id"], STREAM_TYPES)
                _save(D_DETAILS / f"{a['id']}.json",
                      {"analyzed": a.get("analyzed"), "icu_intervals": d.get("icu_intervals"),
                       "icu_groups": d.get("icu_groups"), "description": d.get("description"),
                       "start_latlng": _start_latlng(s)})
                with gzip.open(D_STREAMS / f"{a['id']}.json.gz", "wt") as f:
                    json.dump({x["type"]: x.get("data") for x in s if not x.get("allNull") and x["type"] != "latlng"}, f)
                n_det += 1
            except ApiError as e:
                log(f"  {a['id']} 详情读取失败：{e}")
            if len(todo) > 20:
                if k % 20 == 0:
                    log(f"    {k}/{len(todo)}")
                time.sleep(0.2)

    _, werr = sync_weather(acts, cfg, quiet)
    st["weather_error"] = werr
    st["last_sync"] = dt.datetime.now().isoformat(timespec="seconds")
    st["placeholders"] = len(missing)
    _save(F_STATE, st)
    n_m = rebuild_metrics(cfg)
    if not quiet:
        log(f"同步完成：活动窗口 {a_old}~{t}（新增 {added}，删除 {removed}），wellness {len(wl)} 天，"
            f"跑步详情 {n_det} 个，重算指标 {n_m} 个；缓存共 {len(acts)} 条活动、{len(well)} 天 wellness。")
    return st


def resting_hr(well, a, cfg):
    """(bpm, source) for the run date: wellness that day > 7-day median > Intervals > config."""
    d = parse_date(a["start_date_local"])
    v = (well.get(str(d)) or {}).get("restingHR")
    if v:
        return v, "wellness"
    prev = [(well.get(str(d - dt.timedelta(days=k))) or {}).get("restingHR") for k in range(1, 8)]
    prev = sorted(x for x in prev if x)
    if len(prev) >= 3:
        return prev[len(prev) // 2], "wellness7日中位"
    if a.get("icu_resting_hr"):
        return a["icu_resting_hr"], "Intervals"
    return cfg["score"].get("rest_hr_fallback", 62), "默认值"


def rebuild_metrics(cfg=None, force=False):
    cfg = cfg or load_config()
    acts = activities()
    well = wellness()
    rm = {} if force else run_metrics()
    ov = type_overrides()
    wcache = {}
    changed = 0
    for a in acts.values():
        if not is_run(a) or is_placeholder(a):
            continue
        old = rm.get(a["id"])
        has_s = (D_STREAMS / f"{a['id']}.json.gz").exists()
        want_type = ov.get(a["id"])
        det = detail(a["id"])
        rhr, rhr_src = resting_hr(well, a, cfg)
        lat, lon, src = weather.location_for(det, cfg)
        if (lat, lon) not in wcache:
            wcache[(lat, lon)] = weather.load(lat, lon)
        wx = weather.conditions(a, lat, lon, src, wcache[(lat, lon)])
        if (old and old.get("v") == METRICS_VERSION and old.get("analyzed") == a.get("analyzed")
                and old.get("has_streams") == has_s and old.get("type") == (want_type or old.get("type_auto"))
                and old.get("score_key") == score_key(rhr, wx)):
            continue
        rm[a["id"]] = analyze_run(a, det, streams(a["id"]) if has_s else None, cfg, want_type, rhr, rhr_src, wx)
        changed += 1
    for k in list(rm):
        if k not in acts:
            del rm[k]
            changed += 1
    if changed:
        _save(F_METRICS, rm)
    return changed


def safe_sync(**kw):
    """Sync but fall back to cache on failure, reporting the exact reason."""
    try:
        return sync(**kw), None
    except ApiError as e:
        return state(), str(e)
