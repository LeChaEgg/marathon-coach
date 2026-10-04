"""破3 readiness: how far the current state is from the goal, weighted by how hard the remaining points are.

Model (see docs/methodology.md):
- Points up to the athlete's own historical peak state are "regain" points (cost 1 each).
- Points above that peak are new ground and cost `above_peak_cost` each (default 3).
- Progress = cost covered since the season start / total cost from season start to the goal.
- Required rate = remaining cost / weeks left until `ready_weeks_before` the race, compared with the
  athlete's fastest observed 8-week rise in state score.
- Volume is judged separately with Tanda (2011), which ties marathon pace to 8-week volume and training pace.
"""
import datetime as dt
import math

from .metrics import tanda, time_for_vdot
from .scoring import rolling_state
from .util import iso_week_label, parse_date, week_start

_HIST = {}


def goal_vdot(goal_s, dist=42195):
    return next(v / 10 for v in range(300, 850) if time_for_vdot(v / 10, dist) <= goal_s)


def history(metrics, cfg, d):
    """Weekly (Sunday) composite state from the first scored run up to d."""
    key = (len(metrics), metrics[-1]["id"] if metrics else None, str(d))
    if key in _HIST:
        return _HIST[key]
    scored = [m for m in metrics if m.get("score") and m["score"].get("composite") is not None]
    pts = []
    if scored:
        x = week_start(parse_date(scored[0]["date"])) + dt.timedelta(days=6)
        while x <= d:
            c = rolling_state(metrics, x, cfg).get("composite")
            if c and c.get("v") is not None:
                pts.append((x, c["v"]))
            x += dt.timedelta(days=7)
    _HIST[key] = pts
    return pts


def best_rate(pts, weeks=8):
    by = dict(pts)
    best = None
    for x, v in pts:
        y = x + dt.timedelta(days=7 * weeks)
        if y in by:
            r = (by[y] - v) / weeks
            if best is None or r > best[0]:
                best = (r, x, y)
    return best


def _cost(v, peak, k):
    return v if v <= peak else peak + k * (v - peak)


def _inv(c, peak, k):
    return c if c <= peak else peak + (c - peak) / k


def _k8(ctx, d):
    r56 = ctx.runs(d - dt.timedelta(days=55), d)
    dist = sum(m["dist"] for m in r56)
    return dist / 1000 / 8, (sum(m["mt"] for m in r56) / dist * 1000 if dist else None)


def tanda_need_pace(pm, k):
    return (pm - 17.1 - 140 * math.exp(-0.0053 * k)) / 0.55


def assess(ctx, d=None):
    cfg = ctx.cfg
    rc = cfg.get("readiness", {})
    d = parse_date(d) if d else dt.date.today()
    runs = ctx.runs()
    goal = cfg["race"]["goal_s"]
    need = goal_vdot(goal)
    k = rc.get("above_peak_cost", 3.0)
    season = parse_date(cfg["race"].get("season_start") or str(ctx.race - dt.timedelta(weeks=23)))
    ready_by = ctx.race - dt.timedelta(weeks=rc.get("ready_weeks_before", 2))

    pts = history(runs, cfg, d)
    if rc.get("peak"):
        peak, peak_d = rc["peak"], None
    elif pts:
        peak_d, peak = max(pts, key=lambda p: p[1])
    else:
        peak, peak_d = need, None
    br = best_rate(pts)
    now = (rolling_state(runs, d, cfg).get("composite") or {}).get("v")
    s0 = (rolling_state(runs, season, cfg).get("composite") or {}).get("v")
    out = {"need": need, "now": now, "gap": now - need if now else None, "peak": peak, "peak_date": str(peak_d) if peak_d else None,
           "above_peak_cost": k, "ready_by": str(ready_by), "season_start": str(season), "season_base": s0,
           "best_rate": br[0] if br else None, "best_from": str(br[1]) if br else None, "best_to": str(br[2]) if br else None}
    if now is None or s0 is None:
        return out

    C = lambda v: _cost(v, peak, k)
    total = C(need) - C(s0)
    weeks_left = max((ready_by - d).days / 7, 0.5)
    W0 = max((ready_by - season).days / 7, 1)
    r0 = total / W0

    def required(x):
        w = min(max((parse_date(x) - season).days / 7, 0), W0)
        return _inv(C(s0) + r0 * w, peak, k)

    req = (C(need) - C(now)) / weeks_left
    exp_now = required(d)
    out.update({
        "progress": (C(now) - C(s0)) / total * 100 if total > 0 else None,
        "progress_expected": (C(exp_now) - C(s0)) / total * 100 if total > 0 else None,
        "expected_now": exp_now, "ahead": now - exp_now,
        "cost_left": C(need) - C(now), "regain_left": max(0, min(peak, need) - now), "new_left": max(0, need - max(now, peak)),
        "weeks_left": weeks_left, "req_rate": req, "req_ratio": req / br[0] if br and br[0] > 0 else None,
        "plan_rate": r0, "required": required,
    })

    K, P = _k8(ctx, d)
    pk = _k8(ctx, peak_d) if peak_d else (None, None)
    pm = goal / 42.195
    race_ws = week_start(ctx.race)
    plan_km = []
    for i in range(1, 9):
        row = ctx.plan_row(iso_week_label(race_ws - dt.timedelta(days=7 * i))) or {}
        try:
            plan_km.append(float(row.get("目标跑量km", "").split("+")[0]))
        except ValueError:
            pass
    Kp = sum(plan_km) / len(plan_km) if len(plan_km) == 8 else None
    out["volume"] = {
        "k8": K, "pace8": P, "tanda_now": tanda(K, P) if P else None,
        "peak_k8": pk[0], "peak_pace8": pk[1], "tanda_peak": tanda(*pk) if pk[1] else None,
        "plan_k8": Kp, "tanda_plan": tanda(Kp, P) if (Kp and P) else None,
        "need_pace_at_plan": tanda_need_pace(pm, Kp) if Kp else None,
    }
    return out
