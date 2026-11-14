"""Quantitative run analysis from Intervals.icu activity + streams."""
import math

from .util import linreg, mean, median, stdev

METRICS_VERSION = 9

RUN_TYPES = {"Run", "VirtualRun", "TrailRun"}
QUALITY_TYPES = {"interval", "threshold", "tempo", "long_quality", "race"}

TYPE_LABEL = {
    "race": "比赛/测试",
    "interval": "间歇",
    "threshold": "乳酸阈",
    "tempo": "节奏/马配",
    "long_quality": "长距离+质量段",
    "long": "长距离",
    "easy": "轻松跑",
    "recovery": "恢复跑",
    "short": "短跑/热身",
}
# which types are compared with each other
TYPE_GROUP = {
    "race": "race", "interval": "interval",
    "threshold": "tempo", "tempo": "tempo",
    "long_quality": "long", "long": "long",
    "easy": "easy", "recovery": "easy", "short": "short",
}
GROUP_LABEL = {"race": "比赛/测试", "interval": "间歇课", "tempo": "节奏/阈值课",
               "long": "长距离", "easy": "轻松/恢复跑", "short": "短跑"}

BEST_DISTANCES = [(400, "400m"), (1000, "1km"), (1609, "1mile"), (3000, "3km"), (5000, "5km"),
                  (10000, "10km"), (15000, "15km"), (21097.5, "半马"), (30000, "30km"), (42195, "全马")]
BEST_DURATIONS = [(600, "10min"), (1200, "20min"), (1800, "30min"), (3600, "60min")]

DEFAULT_SLOPE = 0.022  # m/s per bpm, used when pooled fit is not reliable


def is_run(a):
    return a.get("type") in RUN_TYPES


def is_placeholder(a):
    return bool(a.get("_note")) or (a.get("source") == "STRAVA" and not a.get("moving_time"))


def device_era(a):
    name = (a.get("device_name") or "").lower()
    if name.startswith("watch") or any(k in name for k in ("apple", "ultra", "intervals companion")):
        return "apple"
    if any(k in name for k in ("forerunner", "fenix", "garmin", "epix", "venu", "enduro", "instinct", "vivo")):
        return "garmin"
    return "other"


# ---------------------------------------------------------------- helpers

def _ffill(xs):
    out, last = [], None
    for x in xs:
        if x is not None:
            last = x
        out.append(last)
    return out


def _rolling(xs, w):
    out, acc, q = [], 0.0, []
    for x in xs:
        x = 0.0 if x is None else x
        q.append(x)
        acc += x
        if len(q) > w:
            acc -= q.pop(0)
        out.append(acc / len(q))
    return out


class Series:
    def __init__(self, s):
        self.t = s.get("time") or []
        self.n = len(self.t)
        self.d = _ffill(s.get("distance") or [0.0] * self.n)
        self.d = [x or 0.0 for x in self.d]
        self.hr = s.get("heartrate") or [None] * self.n
        self.v = s.get("velocity_smooth") or [None] * self.n
        cad = s.get("cadence") or [None] * self.n
        med = median([c for c in cad if c])
        self.cad = [c * 2 if (c and med and med < 120) else c for c in cad]  # -> steps/min
        alt = s.get("fixed_altitude") or s.get("altitude")
        self.alt = _rolling(_ffill(alt), 15) if alt and any(a is not None for a in alt) else None
        self.watts = s.get("watts") or [None] * self.n
        self.gct = s.get("stance_time") or [None] * self.n
        self.vo = s.get("vertical_oscillation") or [None] * self.n
        self.step = s.get("step_length") or [None] * self.n
        self.dt = [0.0] + [max(0.0, self.t[i] - self.t[i - 1]) for i in range(1, self.n)]
        self.dd = [0.0] + [max(0.0, self.d[i] - self.d[i - 1]) for i in range(1, self.n)]
        if any(self.dd[i] > 7.0 * max(self.dt[i], 1) for i in range(self.n)):  # GPS jumps
            self.dd = [0.0 if dd > 7.0 * max(dt_, 1) else dd for dd, dt_ in zip(self.dd, self.dt)]
            acc, self.d = 0.0, []
            for dd in self.dd:
                acc += dd
                self.d.append(acc)
        self.mov = [self.dt[i] > 0 and self.dd[i] / self.dt[i] >= 0.8 for i in range(self.n)]
        self.running = [self.mov[i] and (self.v[i] or 0) >= 2.2 for i in range(self.n)]
        acc, self.mt_cum = 0.0, []
        for i in range(self.n):
            if self.mov[i]:
                acc += self.dt[i]
            self.mt_cum.append(acc)

    def wavg(self, xs, a, b, positive=True, mask=None):
        mask = mask or self.mov
        num = den = 0.0
        for i in range(a + 1, b + 1):
            x = xs[i]
            if x is None or not mask[i] or (positive and x <= 0):
                continue
            num += x * self.dt[i]
            den += self.dt[i]
        return num / den if den else None

    def vmax(self, xs, a, b):
        vals = [xs[i] for i in range(a, b + 1) if xs[i] is not None]
        return max(vals) if vals else None

    def seg(self, a, b):
        dist = self.d[b] - self.d[a]
        mt = self.mt_cum[b] - self.mt_cum[a]
        out = {
            "dist": dist, "mt": mt, "et": self.t[b] - self.t[a],
            "speed": dist / mt if mt else None,
            "hr": self.wavg(self.hr, a, b), "hr_max": self.vmax(self.hr, a, b),
            "cad": self.wavg(self.cad, a, b), "watts": self.wavg(self.watts, a, b),
        }
        if self.alt:
            gain = loss = 0.0
            for i in range(a + 1, b + 1):
                dz = self.alt[i] - self.alt[i - 1]
                if dz > 0:
                    gain += dz
                else:
                    loss -= dz
            out["gain"], out["loss"] = gain, loss
            grade = (self.alt[b] - self.alt[a]) / dist if dist > 50 else 0.0
            out["grade"] = grade
            if out["speed"]:
                g = grade * 100
                factor = 1 + 0.033 * g if g >= 0 else 1 + 0.018 * g
                out["gap_speed"] = out["speed"] * factor
        return out

    def idx_at_mt(self, sec):
        for i, m in enumerate(self.mt_cum):
            if m >= sec:
                return i
        return self.n - 1


# ---------------------------------------------------------------- pieces

def km_splits(S):
    splits, start, k = [], 0, 1
    for i in range(S.n):
        if S.d[i] >= k * 1000:
            sg = S.seg(start, i)
            sg["km"] = k
            splits.append(sg)
            start, k = i, k + 1
    if S.n and S.d[-1] - S.d[start] >= 200:
        sg = S.seg(start, S.n - 1)
        sg["km"] = round(S.d[-1] / 1000, 2)
        sg["partial"] = True
        splits.append(sg)
    return splits


def best_by_distance(S, D):
    if not S.n or S.d[-1] < D:
        return None
    best, i = None, 0
    for j in range(S.n):
        while i + 1 < j and S.d[j] - S.d[i + 1] >= D:
            i += 1
        span = S.d[j] - S.d[i]
        if span >= D:
            el = (S.t[j] - S.t[i]) * D / span
            if best is None or el < best[0]:
                best = (el, i, j)
    if not best:
        return None
    el, i, j = best
    return {"time": el, "hr": S.wavg(S.hr, i, j), "start_km": S.d[i] / 1000}


def best_by_duration(S, W):
    if not S.n or S.t[-1] < W:
        return None
    best, i = None, 0
    for j in range(S.n):
        while i + 1 < j and S.t[j] - S.t[i + 1] >= W:
            i += 1
        if S.t[j] - S.t[i] >= W:
            sp = (S.d[j] - S.d[i]) / (S.t[j] - S.t[i])
            if best is None or sp > best[0]:
                best = (sp, i, j)
    if not best:
        return None
    sp, i, j = best
    return {"speed": sp, "hr": S.wavg(S.hr, i, j), "start_km": S.d[i] / 1000}


def aerobic_bins(S, warmup=480, win=60):
    bins, i = [], S.idx_at_mt(warmup)
    while i < S.n - 1:
        j = i
        while j < S.n - 1 and S.mt_cum[j] - S.mt_cum[i] < win:
            j += 1
        if S.mt_cum[j] - S.mt_cum[i] < win * 0.9:
            break
        sg = S.seg(i, j)
        ok = (sg["et"] <= win * 1.1 and sg["hr"] and sg["hr"] >= 90 and sg["speed"] and sg["speed"] >= 1.8
              and abs(sg.get("grade", 0.0)) <= 0.03)
        if ok:
            bins.append([round(sg["hr"], 1), round(sg["speed"], 3)])
        i = j
    return bins


def _quantile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def detect_surges(S, thr_speed=None):
    """Stream-based detection of faster segments (reps and strides).

    The reference is the 30th percentile of running speed (the jog/easy part), so sessions where
    reps fill half the run are still detected. Hysteresis: enter at +10%, leave below 93% of that.
    """
    vs = [S.v[i] if (S.v[i] is not None and S.mov[i]) else 0.0 for i in range(S.n)]
    base = _quantile([x for x in vs if x > 1.8], 0.30)
    if not base:
        return [], []
    enter = max(base * 1.10, 0.75 * thr_speed if thr_speed else 0)
    leave = enter * 0.93
    sm = _rolling(vs, 5)
    segs, a = [], None
    for i, x in enumerate(sm):
        if a is None and x >= enter:
            a = i
        elif a is not None and x < leave:
            segs.append([a, i - 1])
            a = None
    if a is not None:
        segs.append([a, S.n - 1])
    merged = []
    for sg in segs:
        if merged and S.t[sg[0]] - S.t[merged[-1][1]] < 20:
            merged[-1][1] = sg[1]
        else:
            merged.append(sg)
    reps, strides = [], []
    for a, b in merged:
        dur = S.t[b] - S.t[a]
        if dur < 8:
            continue
        sg = S.seg(a, b)
        if not sg["speed"] or sg["speed"] < enter * 0.98:
            continue
        sg["end_s"] = S.t[b]
        sg["i"] = [a, b]
        sg["start_s"] = S.t[a]
        j = min(S.n - 1, next((k for k in range(b, S.n) if S.t[k] - S.t[b] >= 60), S.n - 1))
        if S.hr[j] and sg["hr_max"]:
            sg["hrr60"] = sg["hr_max"] - S.hr[j]
        (reps if dur >= 45 else strides).append(sg)
    return reps, strides


def icu_reps(detail):
    ivs = (detail or {}).get("icu_intervals") or []
    work = [x for x in ivs if x.get("type") == "WORK" and x.get("average_speed")]
    rec = [x for x in ivs if x.get("type") in ("RECOVERY", "REST") and x.get("average_speed")]
    if len(work) < 3 or len(rec) < 2:
        return None
    ws, rs = median([x["average_speed"] for x in work]), median([x["average_speed"] for x in rec])
    if not rs or ws < rs * 1.15:
        return None
    return [{"dist": x.get("distance") or 0, "mt": x.get("moving_time") or 0, "speed": x["average_speed"],
             "hr": x.get("average_heartrate"), "hr_max": x.get("max_heartrate"),
             "cad": (x.get("average_cadence") or 0) * (2 if (x.get("average_cadence") or 0) < 120 else 1) or None,
             "i": [x["start_index"], x["end_index"]] if x.get("end_index") else None}
            for x in work]


def longest_block_above(S, hr_floor, smooth=60):
    hs = _rolling(_ffill(S.hr), smooth)
    best = cur = 0.0
    for i in range(S.n):
        if S.hr[i] is not None and hs[i] >= hr_floor and S.mov[i]:
            cur += S.dt[i]
            best = max(best, cur)
        elif S.dt[i] > 0:
            cur = 0.0
    return best


def dynamics(S, a, b, act=None):
    """Running dynamics on running samples only (walk breaks excluded)."""
    mk = S.running if sum(S.running[a:b + 1]) > 60 else S.mov
    out = {k: S.wavg(xs, a, b, mask=mk) for k, xs in
           (("cad", S.cad), ("watts", S.watts), ("gct", S.gct), ("vo", S.vo), ("step", S.step))}
    if out["step"] and out["step"] > 10:
        out["step"] /= 1000.0
    if not out["step"]:
        seg = S.seg(a, b)
        if seg["speed"] and out["cad"]:
            out["step"] = seg["speed"] / (out["cad"] / 60)
    if out["vo"] and out["step"]:
        out["vr"] = out["vo"] / 10 / (out["step"] * 100) * 100  # VO(cm)/step(cm) %
    return out


# ---------------------------------------------------------------- main

def classify(act, dist_km, mt_min, zone_secs, sustained_min, reps, cfg):
    name = (act.get("name") or "").lower()
    if act.get("race") or any(k in name for k in ("race", "比赛", "测试", "test")):
        return "race"
    if dist_km < 3:
        return "short"
    thr_speed = act.get("threshold_pace") or 1000 / 240
    hard = [r for r in reps or [] if (r.get("speed") or 0) >= 0.9 * thr_speed
            or (r.get("hr") or 0) > cfg["hr"]["zones"][2]]
    is_interval = len(hard) >= 3 and sum(r["mt"] for r in hard) >= 240
    z = zone_secs or [0] * 7
    z3p, z4p = sum(z[2:]) / 60, sum(z[3:]) / 60
    long_ = dist_km >= 16 or mt_min >= 90
    if long_:
        return "long_quality" if (sustained_min >= 20 or is_interval) else "long"
    if is_interval:
        return "interval"
    if sustained_min >= 15 or z3p >= 20:
        return "threshold" if z4p >= 12 else "tempo"
    lthr = cfg["hr"]["lthr"]
    if (act.get("average_heartrate") or 999) <= 0.78 * lthr and dist_km < 12:
        return "recovery"
    return "easy"


def analyze_run(act, detail, streams, cfg, override_type=None, rhr=None, rhr_src=None, wx=None):
    S = Series(streams or {})
    zones = cfg["hr"]["zones"]
    dist = act.get("distance") or (S.d[-1] if S.n else 0)
    mt = act.get("moving_time") or (S.mt_cum[-1] if S.n else 0)
    m = {
        "v": METRICS_VERSION, "id": act["id"], "date": act["start_date_local"][:10],
        "start": act["start_date_local"], "name": act.get("name"), "sport": act.get("type"),
        "treadmill": act.get("type") == "VirtualRun" or bool(act.get("trainer")),
        "era": device_era(act), "device": act.get("device_name"),
        "dist": dist, "mt": mt, "et": act.get("elapsed_time"),
        "speed": dist / mt if mt else None, "gap_speed": act.get("gap"),
        "hr": act.get("average_heartrate"), "hr_max": act.get("max_heartrate"),
        "load": act.get("icu_training_load"), "trimp": act.get("trimp"),
        "rpe": act.get("icu_rpe"), "feel": act.get("feel"),
        "icu_decoupling": act.get("decoupling"), "elev_gain": act.get("total_elevation_gain"),
        "zone_secs": act.get("icu_hr_zone_times"), "has_streams": S.n > 0,
        "analyzed": act.get("analyzed"),
    }
    if m["speed"] and m["hr"]:
        m["ef"] = m["speed"] * 60 / m["hr"]  # m/min per bpm
    reps, strides = ([], [])
    if S.n > 10:
        m["splits"] = km_splits(S)
        full = [s for s in m["splits"] if not s.get("partial") and s["speed"]]
        if len(full) >= 3:
            paces = [1000 / s["speed"] for s in full]
            m["split_cv"] = stdev(paces) / mean(paces) * 100 if stdev(paces) else 0.0
        # halves & thirds after 5-min warmup
        w = S.idx_at_mt(300)
        total_mt = S.mt_cum[-1]
        if total_mt - 300 > 900:
            mid = S.idx_at_mt(300 + (total_mt - 300) / 2)
            h1, h2 = S.seg(w, mid), S.seg(mid, S.n - 1)
            m["halves"] = [h1, h2]
            if h1["speed"] and h1["hr"] and h2["speed"] and h2["hr"]:
                ef1, ef2 = h1["speed"] / h1["hr"], h2["speed"] / h2["hr"]
                m["decoupling"] = (ef1 - ef2) / ef1 * 100
            t1 = S.idx_at_mt(300 + (total_mt - 300) / 3)
            t2 = S.idx_at_mt(300 + (total_mt - 300) * 2 / 3)
            m["thirds"] = [S.seg(w, t1), S.seg(t1, t2), S.seg(t2, S.n - 1)]
            m["dyn_thirds"] = [dynamics(S, w, t1), dynamics(S, t2, S.n - 1)]
        m["dyn"] = dynamics(S, 0, S.n - 1)
        pb_speed = max([{"5km": 5000, "10km": 10000, "半马": 21097.5}.get(k, 0) / v
                        for k, v in cfg.get("pb", {}).items() if v] or [4.2])
        m["best_dist"] = {}
        for D, lbl in BEST_DISTANCES:
            r = best_by_distance(S, D)
            cap = pb_speed * (1.45 if D <= 400 else 1.25 if D <= 1000 else 1.2 if D <= 1609 else 1.12 if D <= 3000 else 1.08)
            if r and D / r["time"] <= cap:
                m["best_dist"][lbl] = r
        m["best_dur"] = {lbl: r for W, lbl in BEST_DURATIONS if (r := best_by_duration(S, W))}
        reps, strides = detect_surges(S, act.get("threshold_pace"))
        ir = icu_reps(detail)
        if ir:
            reps = ir
        m["reps"], m["strides"] = reps, strides
        m["sustained_z3_min"] = longest_block_above(S, zones[1] + 1) / 60
        m["sustained_z4_min"] = longest_block_above(S, zones[2] + 1) / 60
        m["bins"] = aerobic_bins(S)
    m["type_auto"] = classify(act, dist / 1000, mt / 60, m["zone_secs"], m.get("sustained_z3_min", 0), reps, cfg)
    m["type"] = override_type or m["type_auto"]
    if m["type"] == "interval" or m["treadmill"]:
        m["bins_for_fit"] = False
    else:
        m["bins_for_fit"] = True
    m["score_key"] = score_key(rhr, wx)
    if S.n > 10 and rhr:
        from .scoring import score_run
        m["score"] = score_run(S, m, cfg, rhr, rhr_src, wx)
    return m


def score_key(rhr, wx):
    return f"{rhr}|{round(wx['temp'], 1) if wx else '-'}|{round(wx['dew'], 1) if wx else '-'}"


def basic_run_metrics(act, cfg, override_type=None):
    """For runs without streams (placeholders excluded)."""
    return analyze_run(act, None, None, cfg, override_type)


# ---------------------------------------------------------------- aerobic model

def pooled_fit(metrics_list):
    xs, ys = [], []
    for m in metrics_list:
        if m.get("bins_for_fit") and m.get("bins"):
            for h, s in m["bins"]:
                xs.append(h)
                ys.append(s)
    if len(xs) < 30 or (max(xs) - min(xs)) < 12:
        return None
    r = linreg(xs, ys)
    if not r:
        return None
    a, b, r2 = r
    return {"a": a, "b": b, "r2": r2, "n": len(xs), "hr_min": min(xs), "hr_max": max(xs)}


def slope_from(fit):
    if fit and 0.010 <= fit["b"] <= 0.040:
        return fit["b"]
    return DEFAULT_SLOPE


def aei(m, slope, ref_hr=150):
    """Aerobic index: speed normalized to ref_hr using a fixed HR-speed slope. Returns m/s."""
    bins = m.get("bins") or []
    if len(bins) < 5:
        return None
    return mean([s + slope * (ref_hr - h) for h, s in bins])


# ---------------------------------------------------------------- predictions

def vdot(dist_m, sec):
    t = sec / 60
    v = dist_m / t
    vo2 = -4.60 + 0.182258 * v + 0.000104 * v * v
    pct = 0.8 + 0.1894393 * math.exp(-0.012778 * t) + 0.2989558 * math.exp(-0.1932605 * t)
    return vo2 / pct


def time_for_vdot(vd, dist_m):
    lo, hi = 60.0, 60 * 60 * 8.0
    for _ in range(60):
        midt = (lo + hi) / 2
        if vdot(dist_m, midt) > vd:
            lo = midt
        else:
            hi = midt
    return (lo + hi) / 2


def riegel(sec, d1, d2, k=1.06):
    return sec * (d2 / d1) ** k


def tanda(weekly_km, mean_pace_s):
    """Tanda 2011: marathon pace (s/km) from 8-week mean weekly km and mean training pace."""
    pm = 17.1 + 140.0 * math.exp(-0.0053 * weekly_km) + 0.55 * mean_pace_s
    return pm * 42.195
