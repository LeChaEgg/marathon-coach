"""Run scoring on a VDOT-equivalent scale (跑力).

Every piece of evidence is a (grade- and heat-adjusted speed, heart rate) pair, extrapolated to VO2max
through heart-rate reserve (Swain: %HRR ≈ %VO2R):

    VO2max ≈ 3.5 + (VO2cost(v) − 3.5) / %HRR,   VO2cost = Daniels running cost (ml/kg/min)

Dimensions use different parts of a run: aerobic (steady 60 s bins), threshold (≥10 min continuous
Z3–Z4, or ≥3 reps of 4–15 min), speed (2.5–12 min reps near max HR, also checked against Daniels race formula) and endurance
(last third vs first third of ≥60 min runs). Details and limits: docs/methodology.md.
"""
import datetime as dt

from .metrics import _ffill, _rolling, vdot
from .util import mean, median, parse_date, stdev

DIMS = ("aerobic", "threshold", "speed")
DIM_LABEL = {"aerobic": "有氧", "threshold": "阈值", "speed": "速度", "endurance": "耐力", "composite": "综合"}
MIN_HRR = 0.40
W_CAP = 20.0  # cap per-run evidence weight in rolling state
RECOVERY_SKIP_S = 120  # HR stays elevated after a rep; those bins would understate aerobic ability
THR_REP_MIN_S, THR_REP_MAX_S = 240, 900  # threshold reps (4–15 min); shorter/longer are speed/continuous
THR_REP_SKIP_S = 120  # HR lags speed: leave out the first 2 min of each rep
THR_REP_MIN_N = 3
# single-run scatter of each dimension around the rolling state (VDOT points), calibrated 2026-10 on Apple-era runs
# (robust sd of run − state: aerobic ~1.4–1.8, threshold ~1.9–3.5, speed ~1.9–2.3, endurance ~0.9–2.0; small n)
NOISE_SD = {"aerobic": 1.6, "threshold": 2.5, "speed": 2.2, "endurance": 1.5}
THR_REP_DISCOUNT = 0.7  # rep-based threshold evidence extrapolates from less data than a continuous block
REP_MIN_S, REP_MAX_S = 150, 720  # shorter reps: HR lags speed, estimate inflates (2:20 reps gave ~58)


def vo2_cost(v):
    """Daniels running cost, v in m/s -> ml/kg/min."""
    v *= 60
    return -4.60 + 0.182258 * v + 0.000104 * v * v


def hrr(hr, rhr, hrmax):
    return (hr - rhr) / (hrmax - rhr) if hr and hrmax > rhr else None


def vo2_est(v, hr, rhr, hrmax):
    p = hrr(hr, rhr, hrmax)
    if not v or p is None or p < MIN_HRR or p > 1.02:
        return None
    return 3.5 + (vo2_cost(v) - 3.5) / p


def _gap(sg):
    return sg.get("gap_speed") or sg.get("speed")


# ---------------------------------------------------------------- evidence extraction

def _bins(S, skip=(), warmup=480, win=60):
    """Steady 60 s bins (mt_start, hr, gap_speed); `skip` = elapsed-time ranges to leave out."""
    out, i = [], S.idx_at_mt(warmup)
    while i < S.n - 1:
        j = i
        while j < S.n - 1 and S.mt_cum[j] - S.mt_cum[i] < win:
            j += 1
        if S.mt_cum[j] - S.mt_cum[i] < win * 0.9:
            break
        sg = S.seg(i, j)
        if any(S.t[i] < b and S.t[j] > a for a, b in skip):
            i = j
            continue
        if (sg["et"] <= win * 1.1 and sg["hr"] and sg["speed"] and sg["speed"] >= 1.8
                and abs(sg.get("grade", 0.0)) <= 0.03):
            out.append((S.mt_cum[i], sg["hr"], _gap(sg)))
        i = j
    return out


def _blocks(S, lo, hi, min_s=600, skip_s=180):
    hs = _rolling(_ffill(S.hr), 60)
    blocks, a = [], None
    for i in range(S.n):
        ok = S.hr[i] is not None and lo <= hs[i] <= hi and S.mov[i]
        if ok and a is None:
            a = i
        elif not ok and a is not None and S.dt[i] > 0:
            blocks.append((a, i - 1))
            a = None
    if a is not None:
        blocks.append((a, S.n - 1))
    out = []
    for a, b in blocks:
        if S.mt_cum[b] - S.mt_cum[a] < min_s:
            continue
        a2 = S.idx_at_mt(S.mt_cum[a] + skip_s)
        chunks, k = [], a2
        while k < b:
            k2 = S.idx_at_mt(S.mt_cum[k] + 60)
            if k2 > b:
                break
            c = S.seg(k, k2)
            if c["speed"]:
                chunks.append((k, k2, c["speed"]))
            k = k2
        sp = [c[2] for c in chunks]
        if len(sp) >= 3 and (stdev(sp) or 0) / mean(sp) > 0.08:
            # rep/recovery mix, or a steady block with a warm-up/cool-down still above the HR floor stuck to it:
            # keep the longest stretch of 60 s chunks whose pace CV is <= 8 % (at least min_s - skip_s long)
            best = None
            for i in range(len(chunks)):
                for j in range(i + 3, len(chunks) + 1):
                    w = sp[i:j]
                    if (stdev(w) or 0) / mean(w) > 0.08:
                        break
                    if best is None or j - i > best[1] - best[0]:
                        best = (i, j)
            if not best or best[1] - best[0] < (min_s - skip_s) / 60:
                continue
            out.append((chunks[best[0]][0], chunks[best[1] - 1][1]))
            continue
        out.append((a2, b))
    return out


# ---------------------------------------------------------------- per run

def score_run(S, m, cfg, rhr, rhr_src, wx):
    sc = cfg["score"]
    hrmax = cfg["hr"]["max"]
    adj = 0.0
    if wx:
        from .weather import heat_adj
        adj = heat_adj(wx.get("temp"), wx.get("dew"), sc.get("heat_scale", 1.0))
    f = 1 + adj
    dims, ev = {}, {}

    # aerobic
    lo, hi = sc["aerobic_hr"]
    skip = [(S.t[r["i"][0]], S.t[min(r["i"][1], S.n - 1)] + RECOVERY_SKIP_S)
            for r in (m.get("reps") or []) if r.get("i") and r["i"][0] < S.n and (r.get("hr_max") or 0) > hi]
    interval_like = len(skip) >= 3
    if interval_like:  # HR stays elevated through recoveries and cool-down
        skip = [(skip[0][0], S.t[-1] + 1)]
    bins = _bins(S, skip)
    wide_bins = _bins(S)
    est = [(t, h, vo2_est(v * f, h, rhr, hrmax)) for t, h, v in bins if lo <= h <= hi]
    est = [x for x in est if x[2]]
    if len(est) >= 5:
        dims["aerobic"] = {"v": median([x[2] for x in est]), "min": len(est),
                           "hrr": mean([hrr(x[1], rhr, hrmax) for x in est]), "hr": mean([x[1] for x in est])}

    # threshold
    lo, hi = sc["threshold_hr"]
    blks = []
    for a, b in _blocks(S, lo, hi):
        sg = S.seg(a, b)
        v = vo2_est(_gap(sg) * f, sg["hr"], rhr, hrmax)
        if v:
            blks.append({"start_s": S.t[a], "mt": sg["mt"], "pace": 1000 / sg["speed"], "hr": sg["hr"], "v": v})
    # threshold from repeated reps (e.g. 4×5 min, 5×1.6 km): measured after the first THR_REP_SKIP_S of each rep,
    # only reps whose measured HR stays below the speed floor (otherwise they count as speed evidence)
    cont = [(b["start_s"], b["start_s"] + b["mt"] + 180) for b in blks]
    rep_blks = []
    for r in m.get("reps") or []:
        idx = r.get("i")
        if not idx or not (THR_REP_MIN_S <= (r.get("mt") or 0) <= THR_REP_MAX_S):
            continue
        a, b = idx
        if b >= S.n or b <= a:
            continue
        if any(S.t[a] < e and S.t[b] > c for c, e in cont):
            continue
        a2 = S.idx_at_mt(S.mt_cum[a] + THR_REP_SKIP_S)
        if b - a2 < 30:
            continue
        sg = S.seg(a2, b)
        if not sg["hr"] or not sg["speed"] or not (lo <= sg["hr"] < sc["speed_hr_floor"]):
            continue
        v = vo2_est(_gap(sg) * f, sg["hr"], rhr, hrmax)
        if v:
            rep_blks.append({"start_s": S.t[a2], "mt": sg["mt"], "pace": 1000 / sg["speed"], "hr": sg["hr"], "v": v, "rep": True})
    if len(rep_blks) >= THR_REP_MIN_N:
        blks += rep_blks
    if m.get("type") == "race" and m.get("dist") and m.get("et") and m["et"] >= 720:
        rv = vdot(m["dist"] * f, m["et"])
        blks.append({"start_s": 0, "mt": m["et"], "pace": m["et"] / m["dist"] * 1000, "hr": m.get("hr"), "v": rv, "race": True})
    if blks:
        tot = sum(x["mt"] for x in blks)
        dims["threshold"] = {"v": sum(x["v"] * x["mt"] for x in blks) / tot, "min": tot / 60,
                             "hrr": mean([hrr(x["hr"], rhr, hrmax) for x in blks if x.get("hr")]) or 0.85,
                             "blocks": blks}

    # speed
    segs = []
    for r in m.get("reps") or []:
        idx = r.get("i")
        if not idx or not (REP_MIN_S <= (r.get("mt") or 0) <= REP_MAX_S):
            continue
        a, b = idx
        if b >= S.n or b <= a:
            continue
        late = [S.hr[k] for k in range(a + int(0.4 * (b - a)), b + 1) if S.hr[k]]
        hr_late = mean(late)
        if not hr_late or hr_late < sc["speed_hr_floor"]:
            continue
        sg = S.seg(a, b)
        v_eq = (_gap(sg) or r["speed"]) * f
        e = vo2_est(v_eq, hr_late, rhr, hrmax)
        perf = vdot(v_eq * sg["mt"], sg["mt"]) if sg["mt"] else None
        v = max(x for x in (e, perf) if x) if (e or perf) else None
        if v:
            segs.append({"start_s": S.t[a], "mt": sg["mt"], "pace": 1000 / sg["speed"], "hr": hr_late,
                         "est": e, "perf": perf, "v": v})
    if segs:
        tot = sum(x["mt"] for x in segs)
        dims["speed"] = {"v": sum(x["v"] * x["mt"] for x in segs) / tot, "min": tot / 60,
                         "hrr": mean([hrr(x["hr"], rhr, hrmax) for x in segs]), "segs": segs}

    for d in dims.values():
        d["w"] = d["min"] * (d["hrr"] or 0) ** 2
    thr = dims.get("threshold")
    if thr and all(b.get("rep") for b in thr["blocks"]):
        thr["w"] *= THR_REP_DISCOUNT

    # endurance (not part of composite)
    total = S.mt_cum[-1] if S.n else 0
    if total >= 3600 and not interval_like:
        w0 = 300
        t1, t2 = w0 + (total - w0) / 3, w0 + (total - w0) * 2 / 3
        wide = [(t, vo2_est(v * f, h, rhr, hrmax)) for t, h, v in wide_bins if 120 <= h <= 180]
        first = [v for t, v in wide if v and t < t1]
        last = [v for t, v in wide if v and t >= t2]
        if len(first) >= 5 and len(last) >= 5:
            fv, lv = median(first), median(last)
            ev["endurance"] = {"v": lv, "first": fv, "retention": lv / fv * 100, "mt": total}

    comp, wsum = None, sum(d["w"] for d in dims.values())
    if wsum > 0:
        comp = sum(d["v"] * d["w"] for d in dims.values()) / wsum
    conf = "高" if wsum >= 20 else "中" if wsum >= 8 else "低"
    if m.get("treadmill") or rhr_src == "默认值":
        conf = "低"
    elif conf == "高" and (not wx or rhr_src != "wellness"):
        conf = "中"

    dyn = m.get("dyn") or {}
    wkg = (dyn.get("watts") or 0) / cfg["athlete"]["weight_kg"]
    out = {"dims": dims, **ev, "composite": comp, "conf": conf, "wsum": wsum,
           "rhr": rhr, "rhr_src": rhr_src, "hrmax": hrmax, "heat_adj": adj, "wx": wx,
           "re": (m["speed"] / wkg) if (wkg and m.get("speed") and not m.get("treadmill")) else None}
    return out


# ---------------------------------------------------------------- rolling state

def dim_value(sc, dim):
    if not sc:
        return None
    if dim == "composite":
        return sc.get("composite")
    if dim == "endurance":
        return (sc.get("endurance") or {}).get("v")
    return ((sc.get("dims") or {}).get(dim) or {}).get("v")


def rolling_state(metrics, d, cfg):
    """Exponentially weighted state per dimension as of date d (runs on or before d)."""
    sc_cfg = cfg["score"]
    hl, win = sc_cfg.get("half_life_days", 14), sc_cfg.get("window_days", 42)
    d = parse_date(d)
    runs = [m for m in metrics if m.get("score") and m["score"]["conf"] != "低" and parse_date(m["date"]) <= d]
    state = {}
    for dim in DIMS + ("endurance",):
        for span, stale in ((win, False), (90, True)):
            num = den = 0.0
            last, n = None, 0
            wts_by_run = {}
            for m in runs:
                age = (d - parse_date(m["date"])).days
                if age > span:
                    continue
                v = dim_value(m["score"], dim)
                if v is None:
                    continue
                if dim == "endurance":
                    w = min(W_CAP, m["score"]["endurance"]["mt"] / 600)
                else:
                    w = min(W_CAP, m["score"]["dims"][dim]["w"])
                w *= 0.5 ** (age / hl)
                wts_by_run[m["id"]] = w
                num += v * w
                den += w
                n += 1
                last = max(last or m["date"], m["date"])
            if den:
                state[dim] = {"v": num / den, "n": n, "last": last, "stale": stale,
                              "age": (d - parse_date(last)).days,
                              "coef": {k: x / den for k, x in wts_by_run.items()}}
                break
    wts = sc_cfg.get("weights", {"aerobic": 0.4, "threshold": 0.3, "speed": 0.3})
    have = [k for k in DIMS if k in state]
    if have:
        tw = sum(wts.get(k, 0) for k in have)
        comp = sum(state[k]["v"] * wts.get(k, 0) for k in have) / tw if tw else None
        state["composite"] = {"v": comp, "dims": have}
    return state


def state_series(metrics, cfg, start, end, step=7):
    """Rolling state sampled every `step` days from start to end (inclusive)."""
    out, d = [], parse_date(start)
    end = parse_date(end)
    while d <= end:
        out.append((str(d), rolling_state(metrics, d, cfg)))
        d += dt.timedelta(days=step)
    return out


def noise_sd(cfg, dim):
    return (cfg["score"].get("noise_sd") or {}).get(dim, NOISE_SD.get(dim, 2.0))


def state_change(metrics, d0, d1, cfg):
    """State change per dimension between two dates, with its standard error.

    The state is a weighted mean of run values, so with independent single-run scatter sigma the change
    d = Σ a_i v_i has SE = sigma * sqrt(Σ a_i²), a_i = run i's weight at d1 minus at d0. Weekly changes of
    ~0.3–0.7 points are usually below this SE: that is the honest limit of a weekly read."""
    d0, d1 = parse_date(d0), parse_date(d1)
    s0 = rolling_state([m for m in metrics if parse_date(m["date"]) <= d0], d0, cfg)
    s1 = rolling_state([m for m in metrics if parse_date(m["date"]) <= d1], d1, cfg)
    out = {}
    for dim in DIMS + ("endurance",):
        a, b = s0.get(dim), s1.get(dim)
        if not a or not b:
            continue
        ids = set(a["coef"]) | set(b["coef"])
        var = sum((b["coef"].get(i, 0) - a["coef"].get(i, 0)) ** 2 for i in ids)
        new = [i for i in b["coef"] if i not in a["coef"]]
        out[dim] = {"v0": a["v"], "v1": b["v"], "delta": b["v"] - a["v"], "se": noise_sd(cfg, dim) * var ** 0.5,
                    "new_runs": len(new), "stale": b["stale"]}
    return out


def change_verdict(delta, se):
    """Plain-language reading of a state change against its standard error."""
    if delta is None or not se:
        return "-"
    z = delta / se
    if abs(z) < 1:
        return "看不出变化"
    arrow = "上升" if z > 0 else "下降"
    return ("倾向" if abs(z) < 2 else "明确") + arrow
