"""Ability dashboard data: served live by server.py, and rendered as an offline HTML file."""
import bisect
import datetime as dt
import json
from pathlib import Path

from .config import REPORTS
from .metrics import TYPE_GROUP, aei, tanda, time_for_vdot, vdot
from .reports import WD, Ctx, _baseline, label_type, light, readiness_flags, week_summary, zone_minutes
from .readiness import assess, goal_vdot
from .scoring import DIMS, rolling_state
from .util import iso_week_label, mean, median, pace_s, parse_date, today, week_start

TEMPLATES = Path(__file__).resolve().parent / "templates"
HTML_OUT = REPORTS / "dashboard.html"
STATE_DIMS = ("composite",) + DIMS + ("endurance",)


def _r(v, nd=1):
    return round(v, nd) if isinstance(v, (int, float)) else None


def _run_rec(m, slope, ref_hr, pain):
    sc = m.get("score") or {}
    dims = sc.get("dims") or {}
    dyn = m.get("dyn") or {}
    wx = sc.get("wx") or {}
    end = sc.get("endurance") or {}
    a = aei(m, slope, ref_hr) if m.get("bins_for_fit") else None
    return {
        "id": m["id"], "d": m["date"], "t": m["start"][11:16], "type": m["type"], "grp": TYPE_GROUP.get(m["type"], "easy"),
        "label": label_type(m), "km": _r(m["dist"] / 1000, 2), "mt": round(m["mt"] or 0), "pace": _r(pace_s(m["speed"]), 0),
        "hr": _r(m.get("hr"), 0), "comp": _r(sc.get("composite")), "conf": sc.get("conf"),
        "A": _r((dims.get("aerobic") or {}).get("v")), "T": _r((dims.get("threshold") or {}).get("v")),
        "S": _r((dims.get("speed") or {}).get("v")), "E": _r(end.get("v")), "ret": _r(end.get("retention"), 0),
        "temp": _r(wx.get("temp")), "dew": _r(wx.get("dew")), "rh": _r(wx.get("rh"), 0), "heat": _r((sc.get("heat_adj") or 0) * 100),
        "rhr": sc.get("rhr"), "aei": _r(pace_s(a), 0), "cad": _r(dyn.get("cad"), 0), "gct": _r(dyn.get("gct"), 0),
        "step": _r(dyn.get("step"), 2), "re": _r(sc.get("re"), 3), "dec": _r(m.get("decoupling")), "load": _r(m.get("load"), 0),
        "era": m.get("era"), "tm": bool(m.get("treadmill")), "pain": pain.get(m["id"]),
        "segs": [{"s": round(x["start_s"]), "mt": round(x["mt"]), "pace": round(x["pace"]), "hr": round(x["hr"]), "v": _r(x["v"])}
                 for x in (dims.get("speed") or {}).get("segs", [])],
        "blocks": [{"s": round(x["start_s"]), "mt": round(x["mt"]), "pace": round(x["pace"]), "hr": _r(x.get("hr"), 0), "v": _r(x["v"])}
                   for x in (dims.get("threshold") or {}).get("blocks", [])],
    }


def _state_rec(st):
    out = {}
    for k in STATE_DIMS:
        if k in st and st[k].get("v") is not None:
            out[k] = _r(st[k]["v"])
            if st[k].get("stale"):
                out[k + "_stale"] = True
    return out


def build(ctx):
    cfg = ctx.cfg
    runs = ctx.runs()
    t = today()
    scored = [m for m in runs if m.get("score") and m["score"].get("composite") is not None]
    start = parse_date(scored[0]["date"]) if scored else t - dt.timedelta(days=90)
    slope = ctx.slope(t)
    ref_hr = ctx.ref_hr
    pain_by_act, pain_by_day = {}, {}
    for f in ctx.fb:
        if f.get("pain") is None:
            continue
        if f.get("activity"):
            pain_by_act[f["activity"]] = max(pain_by_act.get(f["activity"], 0), f["pain"])
        if f.get("date"):
            pain_by_day[f["date"]] = max(pain_by_day.get(f["date"], 0), f["pain"])

    # daily rolling state (only runs within 90 days matter)
    dates = [m["date"] for m in runs]
    daily = []
    d = start
    while d <= t:
        lo = bisect.bisect_left(dates, str(d - dt.timedelta(days=90)))
        hi = bisect.bisect_right(dates, str(d))
        daily.append({"d": str(d), **_state_rec(rolling_state(runs[lo:hi], d, cfg))})
        d += dt.timedelta(days=1)
    by_day = {x["d"]: x for x in daily}

    weeks = []
    ws = week_start(start)
    while ws <= t:
        W = week_summary(ctx, ws)
        we = min(W["we"], t)
        z = zone_minutes(W["runs"])
        well = [ctx.well.get(str(ws + dt.timedelta(days=k))) or {} for k in range(7)]
        ea = [aei(m, slope, ref_hr) for m in W["runs"] if m.get("bins_for_fit") and TYPE_GROUP.get(m["type"]) in ("easy", "long")]
        easy = [m for m in W["runs"] if TYPE_GROUP.get(m["type"]) in ("easy", "long") and not m.get("treadmill")]
        pains = [pain_by_day[str(ws + dt.timedelta(days=k))] for k in range(7) if str(ws + dt.timedelta(days=k)) in pain_by_day]
        weeks.append({
            "w": iso_week_label(ws), "ws": str(ws), "km": _r(W["km"]), "n": W["n"], "long": _r(W["long"]),
            "low": _r(sum(z[:2]) / 60, 0), "mid": _r(z[2] / 60, 0), "high": _r(sum(z[3:]) / 60, 0),
            "hrv": _r(mean([w.get("hrv") for w in well]), 0), "rhr": _r(mean([w.get("restingHR") for w in well]), 0),
            "sleep": _r((mean([w.get("sleepSecs") for w in well]) or 0) / 3600 or None),
            "aei": _r(pace_s(median(ea)), 0), "cad": _r(median([(m.get("dyn") or {}).get("cad") for m in easy]), 0),
            "gct": _r(median([(m.get("dyn") or {}).get("gct") for m in easy]), 0),
            "pain": max(pains) if pains else None,
            "state": {k: v for k, v in by_day.get(str(we), {}).items() if k != "d"},
            "best": _r(max([m["score"]["composite"] for m in W["runs"] if m.get("score") and m["score"].get("composite")
                            and m["score"]["conf"] != "低"], default=None)),
        })
        ws += dt.timedelta(days=7)

    wellness = []
    d = max(start, parse_date(ctx.apple_since))
    while d <= t:
        w = ctx.well.get(str(d)) or {}
        if w.get("hrv") or w.get("restingHR"):
            wellness.append({"d": str(d), "hrv": w.get("hrv"), "rhr": w.get("restingHR")})
        d += dt.timedelta(days=1)

    now = rolling_state(runs, t, cfg)
    prev = rolling_state(runs, t - dt.timedelta(days=28), cfg)
    goal = cfg["race"]["goal_s"]
    need = goal_vdot(goal)
    r56 = ctx.runs(t - dt.timedelta(days=55), t)
    K = sum(m["dist"] for m in r56) / 1000 / 8
    tot_mt, tot_d = sum(m["mt"] for m in r56), sum(m["dist"] for m in r56)
    comp_now = (now.get("composite") or {}).get("v")
    dims_now = {}
    for k in STATE_DIMS:
        if k in now:
            x = now[k]
            dims_now[k] = {"v": _r(x["v"]), "age": x.get("age"), "stale": x.get("stale", False),
                           "last": x.get("last"), "delta": _r(x["v"] - prev[k]["v"]) if k in prev else None}
    pbs = [{"label": f"PB {k}", "v": _r(vdot({"5km": 5000, "10km": 10000, "半马": 21097.5}[k], s))}
           for k, s in cfg.get("pb", {}).items() if k in ("5km", "10km", "半马")]
    return {
        "generated": dt.datetime.now().isoformat(timespec="minutes"),
        "synced": ctx.state.get("last_sync"), "weather_error": ctx.state.get("weather_error"),
        "race": {"name": cfg["race"]["name"], "date": cfg["race"]["date"], "goal_s": goal, "days": (ctx.race - t).days},
        "need": need, "pbs": pbs, "apple_since": ctx.apple_since,
        "season_start": cfg["race"].get("season_start") or str(ctx.race - dt.timedelta(weeks=23)),
        "location": cfg["location"].get("name"),
        "now": {"dims": dims_now, "marathon_daniels": round(time_for_vdot(comp_now, 42195)) if comp_now else None,
                "marathon_tanda": round(tanda(K, tot_mt / tot_d * 1000)) if tot_d else None, "k8": _r(K)},
        "runs": [_run_rec(m, slope, ref_hr, pain_by_act) for m in runs if parse_date(m["date"]) >= start],
        "daily": daily, "weeks": weeks, "wellness": wellness, "readiness": _readiness(assess(ctx, t)),
    }


CONCLUSION = "\n## 教练结论"


def _conclusion(path):
    if not path.exists():
        return None
    text = path.read_text()
    i = text.find(CONCLUSION)
    if i < 0:
        return None
    return text[i + len(CONCLUSION):].strip() or None


def _runs_label(ctx, d):
    return [f"{m['dist'] / 1000:.1f}km {label_type(m)}" for m in ctx.runs(d, d)]


def _week_plan(ctx, t):
    ws = week_start(t)
    wk = iso_week_label(t)
    text = ctx.week_plan(wk)
    days, notes, head = [], [], None
    if text:
        section = None
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("## "):
                section = s[3:]
                continue
            if s.startswith("阶段"):
                head = s
            elif section is None and s.startswith("- 周"):
                body = s[2:]
                if days and "：" in body and body.split("：", 1)[0].endswith("实际"):
                    days[-1]["extra"].append(body)
                    continue
                label, _, rest = body.partition("：")
                days.append({"label": label, "text": rest, "extra": []})
            elif section and s.startswith("- "):
                notes.append(s[2:])
    for k in range(7):
        d = ws + dt.timedelta(days=k)
        wd = f"周{WD[k]}"
        day = next((x for x in days if x["label"].startswith(wd)), None)
        if day is None:
            day = {"label": f"{wd} {d.strftime('%m-%d')}", "text": "", "extra": []}
            days.insert(k, day)
        day.update({"date": str(d), "actual": _runs_label(ctx, d), "today": d == t, "past": d < t})
    days = [x for x in days if "date" in x]
    return {"label": wk, "head": head, "days": days, "notes": notes, "exists": bool(text)}


def _today(ctx, t):
    flags, ld = readiness_flags(ctx, t)
    well = []
    for key, name, sc, nd, good in (("hrv", "HRV", 1, 0, 1), ("restingHR", "静息心率", 1, 0, -1),
                                    ("sleepSecs", "睡眠 h", 1 / 3600, 1, 1), ("readiness", "Readiness", 1, 0, 1)):
        v = ctx.w(t, key)
        bm, bsd, _, _ = _baseline(ctx, t, key, sc)
        v = v * sc if v is not None else None
        z = (v - bm) / bsd if (v is not None and bm is not None and bsd) else None
        well.append({"name": name, "v": _r(v, nd), "base": _r(bm, nd), "z": _r(z * good if z is not None else None),
                     "nd": nd})
    return {
        "date": str(t), "wd": WD[t.weekday()], "light": light(flags).split()[-1],
        "flags": [{"s": s, "msg": msg} for s, msg in sorted(flags, reverse=True)],
        "wellness": well, "km7": _r(ld["km7"]), "acwr": _r(ld["acwr"], 2),
        "conclusion": _conclusion(REPORTS / "daily" / f"{t}.md"),
        "report": (REPORTS / "daily" / f"{t}.md").exists(),
    }


def _latest_run(ctx, data):
    m = next((x for x in reversed(ctx.runs()) if x["type"] != "short"), None)
    if not m:
        return None
    rec = next((r for r in data["runs"] if r["id"] == m["id"]), None)
    path = REPORTS / "runs" / f"{m['date']}_{m['id']}.md"
    fbs = ctx.feedback_for(m["id"])
    return {"run": rec, "conclusion": _conclusion(path), "report": path.exists(),
            "feedback": [{k: f.get(k) for k in ("rpe", "pain", "pain_loc", "pain_when", "gait", "fatigue", "fuel", "note")}
                         for f in fbs]}


def _readiness(rd):
    out = {}
    for k, v in rd.items():
        if k == "required":
            continue
        out[k] = {a: _r(b, 2) for a, b in v.items()} if isinstance(v, dict) else _r(v, 2) if isinstance(v, float) else v
    return out


def _season(ctx, data):
    start = parse_date(data["season_start"])
    by_ws = {w["ws"]: w for w in data["weeks"]}
    t = today()
    required = assess(ctx, t).get("required")
    out = []
    ws = week_start(start)
    while ws <= ctx.race:
        lbl = iso_week_label(ws)
        row = ctx.plan_row(lbl) or {}
        w = by_ws.get(str(ws), {})
        plan_km = row.get("目标跑量km", "")
        try:
            plan_km = float(plan_km.split("+")[0])
        except ValueError:
            plan_km = None
        out.append({"w": lbl, "ws": str(ws), "phase": row.get("阶段"), "plan_km": plan_km, "long_plan": row.get("长距离"),
                    "key": row.get("关键课"), "km": w.get("km") if ws <= t else None,
                    "state": w.get("state") if ws <= t else None, "best": w.get("best"), "current": ws == week_start(t),
                    "req": _r(required(ws + dt.timedelta(days=6))) if required else None})
        ws += dt.timedelta(days=7)
    cps = [{"w": x["w"], "label": x["key"].replace("*", "")} for x in out
           if x.get("key") and "测试" in (x["key"] + (x.get("phase") or ""))]
    return {"start": str(start), "weeks": out, "checkpoints": cps}


def write(sync_err=None):
    ctx = Ctx(sync_err)
    data = build(ctx)
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    html = (TEMPLATES / "dashboard.html").read_text().replace("/*__DATA__*/null", blob.replace("</", "<\\/"))
    HTML_OUT.parent.mkdir(parents=True, exist_ok=True)
    HTML_OUT.write_text(html)
    out = [HTML_OUT]
    return out
