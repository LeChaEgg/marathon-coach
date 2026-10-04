"""Local training hub: `python3 -m coach serve` → http://127.0.0.1:8787

Pure standard library. The page reads everything live from the local cache and the report files,
so coach conclusions written afterwards show up without regenerating anything. Buttons call the
same code paths as the CLI (sync, today/run/week reports, feedback log, plan aggressiveness).
"""
import datetime as dt
import json
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import dashboard, reports, store
from .config import JOURNAL, PLAN, REPORTS, ROOT
from .metrics import QUALITY_TYPES, TYPE_LABEL
from .reports import WD, Ctx, label_type, load_context, week_summary, zone_minutes
from .scoring import rolling_state
from .style import LEVELS, level as plan_level, summary as style_summary
from .util import iso_week_label, mean, pace_s, parse_date, today, week_start

APP = Path(__file__).resolve().parent / "templates" / "app.html"
LOCK = threading.Lock()  # one sync / report generation at a time
RUN_TYPES = ("easy", "recovery", "long", "long_quality", "tempo", "threshold", "interval", "race", "short")
NAME_RE = re.compile(r"^[\w.-]+\.md$")
_r = dashboard._r


# ---------------------------------------------------------------- views

def version():
    """Changes whenever anything the page shows changes on disk."""
    latest = 0.0
    for base in (REPORTS / "daily", REPORTS / "runs", REPORTS / "weekly", PLAN, PLAN / "weeks", JOURNAL, ROOT / "data"):
        if not base.exists():
            continue
        for p in base.iterdir():
            if p.is_file():
                latest = max(latest, p.stat().st_mtime)
    latest = max(latest, (ROOT / "athlete.toml").stat().st_mtime)
    return f"{latest:.3f}"


def _report_index():
    def names(kind):
        d = REPORTS / kind
        return sorted(p.name for p in d.glob("*.md")) if d.exists() else []
    plans = sorted(p.stem for p in (PLAN / "weeks").glob("*.md")) if (PLAN / "weeks").exists() else []
    runs = {n[:-3].split("_", 1)[1]: n for n in names("runs") if "_" in n}
    return {"daily": [n[:-3] for n in names("daily")], "weekly": [n[:-3] for n in names("weekly")], "runs": runs, "plans": plans,
            "concluded": [aid for aid, n in runs.items() if dashboard._conclusion(REPORTS / "runs" / n)],
            "feedback_ids": sorted({f["activity"] for f in store.feedback() if f.get("activity")})}


def _feedback(f):
    return {k: f.get(k) for k in ("date", "activity", "rpe", "pain", "pain_loc", "pain_when", "gait", "fatigue", "fuel",
                                  "type", "red_flag", "note", "logged_at") if f.get(k) is not None}


def day_view(ctx, d):
    out = dashboard._today(ctx, d)
    ld = load_context(ctx, d)
    ctl, atl = ctx.w(d, "ctl"), ctx.w(d, "atl")
    out["load"] = {"km7": _r(ld["km7"]), "chronic": _r(ld["chronic"]), "acwr": _r(ld["acwr"], 2), "km28": _r(ld["km28"]),
                   "runs7": ld["runs7"], "streak": ld["streak"], "ctl": _r(ctl), "atl": _r(atl),
                   "tsb": _r(ctl - atl) if ctl and atl else None}
    days = []
    for k in range(6, -1, -1):
        dd = d - dt.timedelta(days=k)
        sl = ctx.w(dd, "sleepSecs")
        others = [a for a in ctx.acts_on(dd) if not reports.is_run(a) and (a.get("moving_time") or 0) >= 900]
        days.append({"d": str(dd), "wd": WD[dd.weekday()], "sleep": _r(sl / 3600) if sl else None, "hrv": ctx.w(dd, "hrv"),
                     "rhr": ctx.w(dd, "restingHR"), "ready": ctx.w(dd, "readiness"), "load": _r(ctx.load_on(dd), 0),
                     "runs": [{"id": m["id"], "km": _r(m["dist"] / 1000), "label": label_type(m)} for m in ctx.runs(dd, dd)],
                     "other": [f"{a['type']} {round((a.get('moving_time') or 0) / 60)}′" for a in others]})
    out["days"] = days
    out["feedback"] = [_feedback(f) for f in ctx.fb if (f.get("date") or "") >= str(d - dt.timedelta(days=7))
                       and (f.get("date") or "") <= str(d)]
    out["missing_feedback"] = [{"id": m["id"], "d": m["date"], "km": _r(m["dist"] / 1000), "label": label_type(m)}
                               for m in ctx.runs(d - dt.timedelta(days=3), d)
                               if not ctx.feedback_for(m["id"]) and m["type"] != "short"]
    wk = dashboard._week_plan(ctx, d)
    out["plan"] = next((x for x in wk["days"] if x["date"] == str(d)), None)
    out["week_label"] = wk["label"]
    row = ctx.plan_row(wk["label"]) or {}
    out["phase"] = row.get("阶段")
    out["days_to_race"] = (ctx.race - d).days
    return out


def week_view(ctx, ws):
    ws = week_start(ws)
    t = today()
    label = iso_week_label(ws)
    plan = dashboard._week_plan(ctx, ws)
    for x in plan["days"]:
        dd = parse_date(x["date"])
        x["today"], x["past"] = dd == t, dd < t
        x["runs"] = [{"id": m["id"], "km": _r(m["dist"] / 1000), "label": label_type(m), "hr": _r(m.get("hr"), 0),
                      "pace": _r(pace_s(m["speed"]), 0)} for m in ctx.runs(dd, dd)]
    W = week_summary(ctx, ws)
    prev = [week_summary(ctx, ws - dt.timedelta(days=7 * k))["km"] for k in range(1, 5)]
    z = zone_minutes(W["runs"])
    row = ctx.plan_row(label) or {}
    try:
        plan_km = float(row.get("目标跑量km", "").split("+")[0])
    except ValueError:
        plan_km = None
    well = {}
    for key, sc in (("hrv", 1), ("restingHR", 1), ("sleepSecs", 1 / 3600)):
        vals = [ctx.w(ws + dt.timedelta(days=k), key) for k in range(7)]
        well[key] = _r(mean([v * sc for v in vals if v is not None]))
    path = REPORTS / "weekly" / f"{label}.md"
    return {
        "label": label, "ws": str(ws), "we": str(W["we"]), "current": ws == week_start(t), "future": ws > t,
        "km": _r(W["km"]), "n": W["n"], "long": _r(W["long"]), "mt": round(W["mt"]), "pace": _r(W["pace"], 0),
        "quality": [f"{m['date'][5:]} {label_type(m)}" for m in W["quality"]], "prev_km": _r(prev[0]), "avg4": _r(mean(prev)),
        "zones": [round(sum(z[:2])), round(z[2]), round(sum(z[3:]))], "run_load": round(W["run_load"]),
        "other_load": round(W["other_load"]), "ctl": _r(W["ctl"]), "atl": _r(W["atl"]), "well": well,
        "plan": plan, "plan_km": plan_km, "phase": row.get("阶段"), "long_plan": row.get("长距离"), "key": row.get("关键课"),
        "report": path.exists(), "conclusion": dashboard._conclusion(path),
        "feedback": [_feedback(f) for f in ctx.fb if str(ws) <= (f.get("date") or "") <= str(W["we"])],
    }


def run_view(ctx, aid):
    m = ctx.rm.get(aid)
    if not m:
        return None
    cfg = ctx.cfg
    d = parse_date(m["date"])
    slope = ctx.slope(d)
    pain = {aid: max([f["pain"] for f in ctx.feedback_for(aid) if f.get("pain") is not None], default=None)}
    rec = dashboard._run_rec(m, slope, ctx.ref_hr, pain)
    names = cfg["hr"]["zone_names"]
    sc = m.get("score") or {}
    st = rolling_state(ctx.runs(None, d), d, cfg)
    prior = [x for x in ctx.runs(d - dt.timedelta(days=28), d) if x["id"] != aid and x["start"] < m["start"]
             and x.get("score") and x["score"]["conf"] != "低"]
    med = {}
    for k, key in (("comp", None), ("A", "aerobic"), ("T", "threshold"), ("S", "speed"), ("E", "endurance")):
        vals = [x["score"].get("composite") if key is None else reports.dim_value(x["score"], key) for x in prior]
        vals = sorted(v for v in vals if v is not None)
        med[k] = _r(vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2) if vals else None
    order = [x["id"] for x in ctx.runs()]
    i = order.index(aid)
    path = REPORTS / "runs" / f"{m['date']}_{aid}.md"
    wk = dashboard._week_plan(ctx, d)
    wx = sc.get("wx") or {}
    halves = m.get("halves") or []
    return {
        "run": rec, "name": m.get("name"), "type_auto": m.get("type_auto"), "device": m.get("device"),
        "et": round(m.get("et") or 0), "hr_max": _r(m.get("hr_max"), 0), "elev": _r(m.get("elev_gain"), 0),
        "gap": _r(pace_s(m.get("gap_speed")), 0), "ef": _r(m.get("ef"), 2), "split_cv": _r(m.get("split_cv")),
        "rpe": m.get("rpe"), "feels": _r(wx.get("feels")), "wind": _r(wx.get("wind"), 0),
        "splits": [{"km": s["km"], "partial": bool(s.get("partial")), "pace": _r(pace_s(s["speed"]), 0), "hr": _r(s.get("hr"), 0),
                    "cad": _r(s.get("cad"), 0), "gain": _r(s.get("gain"), 0), "loss": _r(s.get("loss"), 0)}
                   for s in (m.get("splits") or []) if s.get("speed")],
        "zones": [{"name": names[k], "secs": round(s)} for k, s in enumerate((m.get("zone_secs") or [])[:7])],
        "halves": [{"pace": _r(pace_s(h["speed"]), 0), "hr": _r(h["hr"], 0)} for h in halves],
        "reps": [{"dist": round(r["dist"]), "mt": round(r["mt"]), "pace": _r(pace_s(r["speed"]), 0), "hr": _r(r.get("hr"), 0),
                  "hr_max": _r(r.get("hr_max"), 0), "hrr60": _r(r.get("hrr60"), 0)} for r in (m.get("reps") or [])],
        "strides": len(m.get("strides") or []),
        "state": dashboard._state_rec(st), "median28": med,
        "feedback": [_feedback(f) for f in ctx.feedback_for(aid)],
        "report": path.name if path.exists() else None, "conclusion": dashboard._conclusion(path),
        "plan": next((x for x in wk["days"] if x["date"] == m["date"]), None),
        "prev": order[i - 1] if i > 0 else None, "next": order[i + 1] if i + 1 < len(order) else None,
        "others": [{"id": x["id"], "t": x["start"][11:16], "km": _r(x["dist"] / 1000), "label": label_type(x)}
                   for x in ctx.runs(d, d) if x["id"] != aid],
    }


def state_view(sync_err=None):
    ctx = Ctx(sync_err)
    data = dashboard.build(ctx)
    t = today()
    data.update({
        "today": day_view(ctx, t), "latest": dashboard._latest_run(ctx, data), "week": week_view(ctx, t),
        "season": dashboard._season(ctx, data),
        "style": {"level": plan_level(ctx.cfg),
                  "levels": [{"lv": k, "name": p["name"], "params": [list(x) for x in style_summary(p)]} for k, p in LEVELS.items()]},
        "reports": _report_index(), "version": version(), "sync_error": sync_err,
        "zones": {"bounds": ctx.cfg["hr"]["zones"], "names": ctx.cfg["hr"]["zone_names"]},
        "types": [{"k": k, "name": TYPE_LABEL.get(k, k)} for k in RUN_TYPES],
        "quality_types": sorted(QUALITY_TYPES),
    })
    return data


# ---------------------------------------------------------------- actions

class BadRequest(Exception):
    pass


def _sync():
    try:
        return store.safe_sync(quiet=True)[1]
    except Exception as e:  # network / parsing problems must not take the page down
        return f"{type(e).__name__}: {e}"


def do_sync(_):
    with LOCK:
        err = _sync()
    return {"ok": err is None, "error": err, "synced": store.state().get("last_sync")}


def do_generate(body):
    kind = body.get("kind")
    if kind not in ("today", "run", "week"):
        raise BadRequest("kind 必须是 today / run / week")
    ref = body.get("ref")
    if ref is not None and not re.fullmatch(r"[\w-]{1,40}", str(ref)):
        raise BadRequest("ref 格式不对")
    with LOCK:
        err = None if body.get("offline") else _sync()
        if kind == "today":
            _, path = reports.morning(None, err)
        elif kind == "run":
            _, path = reports.run_report(ref or "latest", err)
        else:
            _, path = reports.weekly(ref, err)
    if path is None:
        raise BadRequest(f"找不到跑步：{ref}")
    return {"ok": True, "sync_error": err, "kind": kind, "name": path.name}


def _num(body, key, lo, hi):
    v = body.get(key)
    if v in (None, ""):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise BadRequest(f"{key} 需要是数字")
    if not lo <= v <= hi:
        raise BadRequest(f"{key} 需要在 {lo}–{hi} 之间")
    return v


def _text(body, key, limit=600):
    v = body.get(key)
    if v is None:
        return None
    v = str(v).strip()
    return v[:limit] or None


def do_feedback(body):
    aid = _text(body, "activity", 40)
    date = _text(body, "date", 10)
    rm = store.run_metrics()
    if aid:
        if aid not in rm:
            raise BadRequest(f"没有这个活动：{aid}")
        date = date or rm[aid]["date"]
    date = str(parse_date(date)) if date else str(today())
    typ = _text(body, "type", 20)
    if typ and typ not in RUN_TYPES:
        raise BadRequest("type 不在可选范围内")
    entry = {"date": date, "activity": aid, "rpe": _num(body, "rpe", 1, 10), "pain": _num(body, "pain", 0, 10),
             "pain_loc": _text(body, "pain_loc"), "pain_when": _text(body, "pain_when", 20), "gait": _text(body, "gait", 20),
             "fatigue": _text(body, "fatigue"), "fuel": _text(body, "fuel"), "type": typ,
             "red_flag": True if body.get("red_flag") else None, "note": _text(body, "note", 2000)}
    entry = {k: v for k, v in entry.items() if v is not None}
    if not set(entry) - {"date", "activity"}:
        raise BadRequest("没有填写任何内容")
    entry["logged_at"] = dt.datetime.now().isoformat(timespec="seconds")
    with LOCK:
        store.add_feedback(entry)
        if typ:
            store.rebuild_metrics()
        if aid and (REPORTS / "runs" / f"{rm[aid]['date']}_{aid}.md").exists():
            reports.run_report(aid)  # refresh the feedback section; the coach conclusion is preserved
    return {"ok": True, "entry": entry}


def do_style(body):
    lv = body.get("level")
    if lv not in LEVELS:
        raise BadRequest("level 需要是 1–5")
    p = ROOT / "athlete.toml"
    text, n = re.subn(r"(?m)^(aggressiveness\s*=\s*)\d+", rf"\g<1>{lv}", p.read_text())
    if n != 1:
        raise BadRequest("athlete.toml 里找不到 [plan] aggressiveness")
    with LOCK:
        p.write_text(text)
    return {"ok": True, "level": lv, "name": LEVELS[lv]["name"]}


POST = {"/api/sync": do_sync, "/api/generate": do_generate, "/api/feedback": do_feedback, "/api/style": do_style}


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "coach"

    def log_message(self, fmt, *args):
        if str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            super().log_message(fmt, *args)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, APP.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/version":
                return self._send(200, {"version": version()})
            if u.path == "/api/state":
                return self._send(200, state_view())
            if u.path == "/api/day":
                return self._send(200, day_view(Ctx(), parse_date(q.get("d") or today())))
            if u.path == "/api/week":
                return self._send(200, week_view(Ctx(), parse_date(q.get("d") or today())))
            if u.path == "/api/run":
                v = run_view(Ctx(), q.get("id", ""))
                return self._send(200, v) if v else self._send(404, {"error": "没有这个活动"})
            if u.path == "/api/report":
                kind, name = q.get("kind"), q.get("name", "")
                base = {"daily": REPORTS / "daily", "runs": REPORTS / "runs", "weekly": REPORTS / "weekly",
                        "plan": PLAN / "weeks", "season": PLAN}.get(kind)
                if base is None or not NAME_RE.match(name) or not (base / name).is_file():
                    return self._send(404, {"error": "报告不存在"})
                return self._send(200, {"text": (base / name).read_text()})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        fn = POST.get(urlparse(self.path).path)
        if fn is None:
            return self._send(404, {"error": "not found"})
        # a custom header cannot be sent cross-origin without a CORS preflight, which this server never answers
        if self.headers.get("X-Coach") != "1":
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if not isinstance(body, dict):
                raise BadRequest("请求体需要是 JSON 对象")
            return self._send(200, fn(body))
        except (BadRequest, json.JSONDecodeError, ValueError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def serve(host="127.0.0.1", port=8787, open_browser=False):
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    print(f"训练中心：{url}  （Ctrl+C 停止）", flush=True)
    if host not in ("127.0.0.1", "localhost"):
        print("⚠️ 已对局域网开放：同一网络里的设备都能查看数据、提交反馈。", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
