"""Fixed quantitative reports. Coaching judgement is added by the LLM on top of these."""
import datetime as dt

from . import store
from .config import PLAN, REPORTS, load_config
from .metrics import (BEST_DISTANCES, GROUP_LABEL, QUALITY_TYPES, TYPE_GROUP, TYPE_LABEL, aei, is_placeholder, is_run,
                      pooled_fit, riegel, slope_from, tanda, time_for_vdot, vdot)
from .readiness import assess, goal_vdot
from .scoring import DIM_LABEL, REP_MAX_S, REP_MIN_S, THR_REP_MAX_S, THR_REP_MIN_N, THR_REP_MIN_S, dim_value, rolling_state
from .style import POLICY, style
from .util import (fmt_dur, fmt_hm, fmt_pace, fmt_speed_pace, fnum, iso_week_label, linreg, md_table, mean,
                   median, pace_s, parse_date, signed, signed_pace, spark, stdev, today, week_start)

WD = "一二三四五六日"
MP_BAND = (245, 268)  # s/km, ~4:05–4:28 for MP-specific volume
DIST_KM = {lbl: dist / 1000 for dist, lbl in BEST_DISTANCES}


# ================================================================ context

class Ctx:
    def __init__(self, sync_err=None):
        self.cfg = load_config()
        self.acts = store.activities()
        self.well = store.wellness()
        self.rm = store.run_metrics()
        self.fb = store.feedback()
        self.state = store.state()
        self.sync_err = sync_err
        self.race = parse_date(self.cfg["race"]["date"])
        self.apple_since = self.cfg["devices"]["apple_watch_since"]
        self.zones = self.cfg["hr"]["zones"]
        self.ref_hr = self.cfg["hr"].get("aerobic_ref_hr", 150)

    # --- runs
    def runs(self, start=None, end=None):
        out = []
        for m in self.rm.values():
            d = m["date"]
            if (start is None or d >= str(start)) and (end is None or d <= str(end)):
                out.append(m)
        return sorted(out, key=lambda m: m["start"])

    def fit(self, end, days=90):
        start = parse_date(end) - dt.timedelta(days=days)
        ms = [m for m in self.runs(start, end) if m["date"] >= self.apple_since or m["era"] == "apple"]
        if len([b for m in ms for b in (m.get("bins") or [])]) < 60:
            ms = self.runs(start, end)
        return pooled_fit(ms)

    def slope(self, end):
        return slope_from(self.fit(end))

    def acts_on(self, d):
        d = str(d)
        return [a for a in self.acts.values() if (a.get("start_date_local") or "")[:10] == d]

    def run_km(self, start, end):
        return sum((m["dist"] or 0) for m in self.runs(start, end)) / 1000

    def load_on(self, d):
        return sum(a.get("icu_training_load") or 0 for a in self.acts_on(d))

    def w(self, d, key):
        return (self.well.get(str(d)) or {}).get(key)

    def header_line(self):
        s = self.state
        line = f"数据：本地缓存同步于 {s.get('last_sync', '-')}"
        if self.sync_err:
            line += f"｜⚠️ 本次同步失败，使用缓存：{self.sync_err}"
        if s.get("weather_error"):
            line += f"｜⚠️ 天气获取失败（缺天气的跑步未做温湿度修正）：{s['weather_error']}"
        return line

    def plan_row(self, label):
        p = PLAN / "season_plan.md"
        if not p.exists():
            return None
        header = None
        for line in p.read_text().splitlines():
            if line.startswith("| 周次"):
                header = [c.strip() for c in line.strip("|").split("|")]
            if line.startswith(f"| {label}"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                return dict(zip(header or [], cells))
        return None

    def week_plan(self, label):
        p = PLAN / "weeks" / f"{label}.md"
        return p.read_text().strip() if p.exists() else None

    def countdown(self, d):
        days = (self.race - parse_date(d)).days
        return f"距{self.cfg['race']['name']} {days} 天（{days // 7}周{days % 7}天）"

    def feedback_for(self, aid):
        return [f for f in self.fb if f.get("activity") == aid]


def fb_line(f):
    """One feedback entry, listing only the fields that were actually recorded."""
    num = lambda v: f"{v:g}" if isinstance(v, (int, float)) else str(v)
    parts = []
    if f.get("red_flag"):
        parts.append("🔴 红旗")
    if f.get("rpe") is not None:
        parts.append(f"RPE {num(f['rpe'])}")
    if f.get("pain") is not None:
        where = "，".join(x for x in (f.get("pain_loc"), f.get("pain_when")) if x)
        parts.append(f"疼痛 {num(f['pain'])}/10" + (f"（{where}）" if where else ""))
    for key, name in (("gait", "步态"), ("fatigue", "疲劳"), ("fuel", "补给"), ("type", "类型改为")):
        if f.get(key):
            parts.append(f"{name} {f[key]}")
    head = "｜".join(parts)
    return "；".join(x for x in (head, f.get("note")) if x) or "（空记录）"


def label_type(m):
    t = TYPE_LABEL.get(m["type"], m["type"])
    return t + ("·跑步机" if m.get("treadmill") else "")


def pct(x, tot):
    return f"{x / tot * 100:.0f}%" if tot else "-"


def zone_minutes(ms):
    z = [0.0] * 7
    for m in ms:
        for i, s in enumerate(m.get("zone_secs") or []):
            if i < 7:
                z[i] += s or 0
    return z


def best_in(ms, key, lbl, min_hr=None):
    best = None
    for m in ms:
        r = (m.get(key) or {}).get(lbl)
        if not r or (min_hr and (r.get("hr") or 0) < min_hr):
            continue
        better = (best is None or (key == "best_dist" and r["time"] < best[0]["time"])
                  or (key == "best_dur" and r["speed"] > best[0]["speed"]))
        if better:
            best = (r, m)
    return best


def save(kind, name, text):
    p = REPORTS / kind / name
    p.parent.mkdir(parents=True, exist_ok=True)
    marker = "\n## 教练结论"
    if p.exists():
        old = p.read_text()
        if marker in old:
            text = text.rstrip() + "\n" + old[old.index(marker):]
    p.write_text(text)
    return p


# ================================================================ morning

def _baseline(ctx, d, key, scale=1.0, days=28):
    vals = []
    for k in range(1, days + 1):
        dd = str(d - dt.timedelta(days=k))
        if dd < ctx.apple_since:
            break
        v = ctx.w(dd, key)
        if v is not None:
            vals.append(v * scale)
    last7 = [ctx.w(d - dt.timedelta(days=k), key) for k in range(0, 7)]
    last7 = [v * scale for v in last7 if v is not None]
    return mean(vals), stdev(vals), mean(last7), len(vals)


def resting_hr_est(ctx, d, days=28, min_n=7):
    """(bpm, estimated) for d. Apple only finalises resting HR later in the day, so when it is missing the
    sleeping HR of that night plus the median (resting − sleeping) gap of the previous `days` days stands in.
    Baselines and the run score keep using measured values only."""
    v = ctx.w(d, "restingHR")
    if v:
        return v, False
    sl = ctx.w(d, "avgSleepingHR")
    if not sl:
        return None, False
    gaps = []
    for k in range(1, days + 1):
        dd = d - dt.timedelta(days=k)
        if str(dd) < ctx.apple_since:
            break
        r, s = ctx.w(dd, "restingHR"), ctx.w(dd, "avgSleepingHR")
        if r and s:
            gaps.append(r - s)
    if len(gaps) < min_n:
        return None, False
    return round(sl + median(gaps)), True


def readiness_flags(ctx, d):
    """Objective signals only. severity: 1 caution, 2 yellow, 3 red."""
    flags = []
    hrv, hsd, h7, _ = _baseline(ctx, d, "hrv")
    v = ctx.w(d, "hrv")
    if v is not None and hrv and hsd:
        if v < hrv - 2 * hsd:
            flags.append((2, f"HRV {v:.0f} 低于28天基线2SD（{hrv:.0f}±{hsd:.0f}）"))
        elif v < hrv - hsd:
            flags.append((1, f"HRV {v:.0f} 低于28天基线1SD（{hrv:.0f}±{hsd:.0f}）"))
    if h7 and hrv and hsd and h7 < hrv - 0.5 * hsd:
        flags.append((1, f"HRV 7日均值 {h7:.0f} 持续低于基线"))
    rm_, rsd, _, _ = _baseline(ctx, d, "restingHR")
    thr = max(4, 1.5 * (rsd or 0))
    r0, r0_est = resting_hr_est(ctx, d)
    if r0 is None:
        r0 = ctx.w(d - dt.timedelta(days=1), "restingHR")
    r1 = ctx.w(d - dt.timedelta(days=1), "restingHR")
    r2 = ctx.w(d - dt.timedelta(days=2), "restingHR")
    if rm_ and r1 and r2 and r1 >= rm_ + thr and r2 >= rm_ + thr:
        flags.append((2, f"静息心率连续两日高于基线≥{thr:.0f}（{r2}/{r1} vs {rm_:.0f}）"))
    elif rm_ and r0 and r0 >= rm_ + thr:
        flags.append((1, f"静息心率 {r0}{'（按睡眠心率估算）' if r0_est else ''} 高于基线 {rm_:.0f}"))
    sl = ctx.w(d, "sleepSecs")
    if sl is not None and sl < 6 * 3600:
        flags.append((1, f"昨晚睡眠 {fmt_hm(sl)} < 6h"))
    last3 = [ctx.w(d - dt.timedelta(days=k), "sleepSecs") for k in range(3)]
    last3 = [x for x in last3 if x]
    if len(last3) == 3 and mean(last3) < 6.5 * 3600:
        flags.append((2, f"近3晚平均睡眠 {fmt_hm(mean(last3))}，睡眠债"))
    rd = ctx.w(d, "readiness")
    if rd is not None and rd < 60:
        flags.append((1, f"Readiness {rd:.0f} < 60"))
    ctl, atl = ctx.w(d, "ctl"), ctx.w(d, "atl")
    if ctl and atl and (ctl - atl) / ctl < -0.30:
        flags.append((1, f"TSB {ctl - atl:.0f}（{(ctl - atl) / ctl * 100:.0f}% of CTL），短期疲劳积累"))
    ld = load_context(ctx, d)
    lv, st = style(ctx.cfg)
    if ld["acwr"] and ld["acwr"] > st["acwr_yellow"]:
        flags.append((2, f"7天跑量 {ld['km7']:.1f}km 是此前4周周均 {ld['chronic']:.1f}km 的 {ld['acwr']:.1f} 倍"
                         f"（>{st['acwr_yellow']:.2f}，激进程度 {lv}；跑量突增，胫骨风险）"))
    elif ld["acwr"] and ld["acwr"] > st["acwr_caution"]:
        flags.append((1, f"7天跑量/此前周均 = {ld['acwr']:.2f}"))
    if ld["streak"] >= 6:
        flags.append((1, f"已连续跑步 {ld['streak']} 天"))
    for f in ctx.fb:
        fd = f.get("date")
        if not fd or fd < str(d - dt.timedelta(days=3)):
            continue
        if f.get("red_flag"):
            flags.append((3, f"{fd} 反馈红旗：{f.get('pain_loc') or ''} {f.get('note') or ''}".strip()))
        elif (f.get("pain") or 0) >= 3:
            flags.append((2, f"{fd} 疼痛 {f['pain']}/10 {f.get('pain_loc') or ''}"))
    return flags, ld


def light(flags):
    if any(s >= 3 for s, _ in flags):
        return "🔴 红"
    if any(s == 2 for s, _ in flags) or sum(1 for s, _ in flags if s == 1) >= 3:
        return "🟡 黄"
    return "🟢 绿"


def load_context(ctx, d):
    d = parse_date(d)
    km7 = ctx.run_km(d - dt.timedelta(days=6), d)
    prior = ctx.run_km(d - dt.timedelta(days=34), d - dt.timedelta(days=7)) / 4
    km28 = ctx.run_km(d - dt.timedelta(days=27), d)
    streak, k = 0, 0
    if not ctx.runs(d, d):
        k = 1
    while ctx.runs(d - dt.timedelta(days=k), d - dt.timedelta(days=k)):
        streak += 1
        k += 1
    r7 = ctx.runs(d - dt.timedelta(days=6), d)
    r28 = ctx.runs(d - dt.timedelta(days=27), d)
    past = ctx.runs(None, d)
    lastq = next((m for m in reversed(past) if m["type"] in QUALITY_TYPES), None)
    lastlong = next((m for m in reversed(past) if TYPE_GROUP.get(m["type"]) == "long"), None)
    return {
        "km7": km7, "chronic": prior, "km28": km28,
        "acwr": km7 / prior if prior > 1 else (None if km7 < 1 else 9.9),
        "runs7": len(r7), "streak": streak,
        "long7": max([m["dist"] for m in r7], default=0) / 1000,
        "long28": max([m["dist"] for m in r28], default=0) / 1000,
        "lastq": lastq, "lastlong": lastlong,
    }


def morning(d=None, sync_err=None):
    ctx = Ctx(sync_err)
    d = parse_date(d or today())
    wk = iso_week_label(d)
    L = []
    L.append(f"# 晨间准备度报告 {d}（周{WD[d.weekday()]}）\n")
    row = ctx.plan_row(wk)
    L.append(f"{ctx.countdown(d)}｜本周 {wk}" + (f"｜阶段：{row.get('阶段')}｜本周目标：{row.get('目标跑量km')} km，长距离 {row.get('长距离')}"
                                                if row else "｜（赛季计划未找到本周）"))
    L.append(ctx.header_line() + "\n")

    # 1. wellness vs baseline
    L.append("## 1. 今日恢复指标（Apple Watch 时代基线：前28天）\n")
    rows = []
    specs = [("hrv", "HRV rMSSD", 1, 0, 1), ("hrvSDNN", "HRV SDNN", 1, 0, 1), ("restingHR", "静息心率", 1, 0, -1),
             ("avgSleepingHR", "睡眠平均心率", 1, 0, -1), ("sleepSecs", "睡眠时长 h", 1 / 3600, 1, 1),
             ("sleepScore", "睡眠评分", 1, 0, 1), ("readiness", "Readiness", 1, 0, 1), ("respiration", "呼吸频率", 1, 1, -1),
             ("weight", "体重 kg", 1, 1, 0)]
    for key, name, sc, nd, good in specs:
        v, est = resting_hr_est(ctx, d) if key == "restingHR" else (ctx.w(d, key), False)
        bm, bsd, b7, n = _baseline(ctx, d, key, sc)
        if v is None and bm is None:
            continue
        if est:
            name += "（估：睡眠心率+近28天差值）"
        v = v * sc if v is not None else None
        z = (v - bm) / bsd if (v is not None and bm is not None and bsd) else None
        verdict = "-"
        if z is not None and good:
            zz = z * good
            verdict = "偏好" if zz >= 1 else ("偏差" if zz <= -1 else "正常")
        rows.append([name, fnum(v, nd) if v is not None else "未记录/未完结", fnum(b7, nd),
                     f"{fnum(bm, nd)} ± {fnum(bsd, nd)}（n={n}）", signed(z, 1, "σ") if z is not None else "-", verdict])
    L.append(md_table(["指标", "今日", "7天均值", "28天基线", "z", "判定"], rows))
    L.append("")

    # 2. last 7 days
    L.append("## 2. 最近7天\n")
    rows = []
    for k in range(6, -1, -1):
        dd = d - dt.timedelta(days=k)
        rs = ctx.runs(dd, dd)
        others = [a for a in ctx.acts_on(dd) if not is_run(a)]
        oth = "、".join(f"{a['type']} {fmt_hm(a.get('moving_time'))}/{a.get('icu_training_load') or 0}"
                       for a in others if (a.get("moving_time") or 0) >= 900) or "-"
        run = "；".join(f"{m['dist'] / 1000:.1f}km {label_type(m)} {fmt_speed_pace(m['speed'])} HR{fnum(m['hr'], 0)}"
                       for m in rs) or "-"
        rows.append([f"{dd.strftime('%m-%d')} {WD[dd.weekday()]}", fmt_hm(ctx.w(dd, "sleepSecs")), fnum(ctx.w(dd, "sleepScore"), 0),
                     fnum(ctx.w(dd, "hrv"), 0), fnum(ctx.w(dd, "restingHR"), 0), fnum(ctx.w(dd, "readiness"), 0),
                     run, oth, fnum(ctx.load_on(dd), 0)])
    L.append(md_table(["日期", "睡眠", "评分", "HRV", "RHR", "Ready", "跑步", "其他运动(≥15min 时长/负荷)", "总负荷"], rows))
    L.append("")

    # 3. load
    flags, ld = readiness_flags(ctx, d)
    ctl, atl = ctx.w(d, "ctl"), ctx.w(d, "atl")
    L.append("## 3. 训练负荷\n")
    L.append(f"- CTL {fnum(ctl)} ｜ ATL {fnum(atl)} ｜ TSB {fnum(ctl - atl if ctl and atl else None)} ｜ ramp {fnum(ctx.w(d, 'rampRate'))}/周（全运动，含攀岩骑行）")
    acwr = f"{ld['acwr']:.2f}" if ld["acwr"] else "-"
    L.append(f"- 跑量：近7天 **{ld['km7']:.1f} km / {ld['runs7']} 次**；此前4周周均 {ld['chronic']:.1f} km；比值 **{acwr}**；近28天 {ld['km28']:.1f} km")
    L.append(f"- 连续跑步 {ld['streak']} 天；近7天最长 {ld['long7']:.1f} km，近28天最长 {ld['long28']:.1f} km")
    if ld["lastq"]:
        q = ld["lastq"]
        L.append(f"- 最近质量课：{q['date']} {label_type(q)} {q['dist'] / 1000:.1f}km（{(d - parse_date(q['date'])).days} 天前）")
    if ld["lastlong"]:
        q = ld["lastlong"]
        L.append(f"- 最近长距离：{q['date']} {q['dist'] / 1000:.1f}km {fmt_dur(q['mt'])}（{(d - parse_date(q['date'])).days} 天前）")
    L.append("")

    # 4. subjective
    L.append("## 4. 主观反馈（journal，近7天）\n")
    recent_fb = [f for f in ctx.fb if (f.get("date") or "") >= str(d - dt.timedelta(days=7))]
    if recent_fb:
        for f in recent_fb:
            L.append(f"- {f.get('date')}{' ' + f['activity'] if f.get('activity') else ''}：{fb_line(f)}")
    else:
        L.append("- 无记录")
    nofb = [m for m in ctx.runs(d - dt.timedelta(days=3), d) if not ctx.feedback_for(m["id"]) and m["type"] != "short"]
    if nofb:
        L.append("- ⚠️ 以下跑步缺主观反馈：" + "、".join(f"{m['date']} {m['dist'] / 1000:.1f}km({m['id']})" for m in nofb))
    L.append("")

    # 5. plan
    L.append("## 5. 今日计划\n")
    wp = ctx.week_plan(wk)
    if wp:
        today_lines = [ln for ln in wp.splitlines() if f"周{WD[d.weekday()]}" in ln or d.strftime("%m-%d") in ln]
        L.append("本周计划中今天：" + ("\n" + "\n".join(today_lines) if today_lines else "（未找到今天条目，见 plan/weeks/%s.md）" % wk))
    else:
        L.append(f"- plan/weeks/{wk}.md 不存在（周复盘时生成）")
    evs = [e for e in store.events() if (e.get("start_date_local") or "")[:10] == str(d)]
    for e in evs:
        L.append(f"- Intervals 日历：{e.get('category')} {e.get('name')} {(e.get('description') or '')[:200]}")
    L.append("")

    # 6. objective light
    L.append("## 6. 客观信号初判（未含今日主观感受与疼痛）\n")
    L.append(f"**{light(flags)}**")
    for s, msg in sorted(flags, reverse=True):
        L.append(f"- {'🔴' if s == 3 else '🟡' if s == 2 else '⚪'} {msg}")
    if not flags:
        L.append("- 无异常客观信号")
    lv, st = style(ctx.cfg)
    L.append(f"\n计划激进程度 **{lv} {st['name']}**：客观黄灯时 {st['yellow_cut']}；每周质量课 ≤{st['quality']} 次。疼痛/红旗规则不随档位改变。")
    L.append("\n> 最终判断优先级：局部胫骨疼痛/跳跃痛/跛行 > 主观疲劳与次日反应 > 最近跑量 > wellness > VO₂max。")
    text = "\n".join(L) + "\n"
    p = save("daily", f"{d}.md", text)
    return text, p


# ================================================================ run analysis

DIM_ORDER = ("aerobic", "threshold", "speed", "endurance")


def comp_of(m):
    return (m.get("score") or {}).get("composite")


def fmt_score(v, pct_=None):
    if v is None:
        return "-"
    return f"{v:.1f}" + (f"（{pct_:.0f}%）" if pct_ is not None else "")


def wx_line(sc):
    wx = sc.get("wx")
    if not wx:
        return "天气：无数据，未做温湿度修正"
    return (f"天气（{wx['loc']}，Open-Meteo 逐小时{'' if wx.get('final') else '·近期值可能修订'}）：气温 {wx['temp']:.1f}°C、"
            f"露点 {wx['dew']:.1f}°C、湿度 {wx['rh']:.0f}%、体感 {wx['feels']:.1f}°C、风 {wx['wind']:.0f} km/h、"
            f"日照 {wx['rad']:.0f} W/m² → 速度热修正 +{sc['heat_adj'] * 100:.1f}%")


def _dim_evidence(k, d, sc):
    if k == "aerobic":
        return f"{d['min']:.0f} 个 60s 稳态片段，均 HR {d['hr']:.0f}（{d['hrr'] * 100:.0f}% HRR）"
    if k == "threshold":
        bl = d["blocks"]
        reps = [b for b in bl if b.get("rep")]
        out = [("比赛全程 " if b.get("race") else f"{fmt_dur(b['start_s'])} 起 ") +
               f"{fmt_dur(b['mt'])} @{fmt_pace(b['pace'])} HR{b['hr']:.0f}" for b in bl if not b.get("rep")]
        if reps:
            out.append(f"分组 {len(reps)} 组（去掉各组前 2 分钟）共 {fmt_dur(sum(b['mt'] for b in reps))}"
                       f"，均 @{fmt_pace(mean([b['pace'] for b in reps]))} HR{mean([b['hr'] for b in reps]):.0f}（权重 ×0.7）")
        return "；".join(out)
    if k == "speed":
        return f"{len(d['segs'])} 个快段共 {d['min']:.0f} 分钟，后段均 HR {mean([s['hr'] for s in d['segs']]):.0f}"
    if k == "endurance":
        return f"后1/3 vs 前1/3（{d['first']:.1f}）保持率 {d['retention']:.0f}%，移动 {fmt_dur(d['mt'])}"
    return ""


def score_section(ctx, m):
    sc = m.get("score")
    need = goal_vdot(ctx.cfg["race"]["goal_s"])
    L = [f"## 2. 本次跑力评分（VDOT 刻度；破3 ≈ {need}）\n"]
    if not sc or sc.get("composite") is None and not sc.get("endurance"):
        L.append("- 无足够的稳态/快段数据，未评分（常见于 <3km、无心率或逐秒数据缺失）\n")
        return L
    d = parse_date(m["date"])
    comp = sc.get("composite")
    L.append(f"**综合跑力 {fmt_score(comp)}**｜距破3 {signed(comp - need, 1) if comp else '-'}｜置信度 {sc['conf']}"
             + (f"｜对应全马（Daniels，乐观上限）{fmt_dur(time_for_vdot(comp, 42195))}" if comp else "") + "\n")
    prior = [x for x in ctx.runs(d - dt.timedelta(days=28), d) if x["id"] != m["id"] and x["start"] < m["start"]
             and x.get("score") and x["score"]["conf"] != "低"]
    rows = []
    for k in DIM_ORDER:
        dd = sc.get("endurance") if k == "endurance" else (sc.get("dims") or {}).get(k)
        if not dd:
            continue
        ref = median([dim_value(x["score"], k) for x in prior])
        rows.append([DIM_LABEL[k], f"{dd['v']:.1f}", signed(dd["v"] - need, 1), _dim_evidence(k, dd, sc),
                     fnum(ref, 1), signed(dd["v"] - ref, 1) if ref else "-"])
    L.append(md_table(["维度", "跑力", "距破3", "证据", "近28天中位", "差"], rows))
    segs = ((sc.get("dims") or {}).get("speed") or {}).get("segs")
    if segs:
        L.append("\n速度维度快段：\n")
        L.append(md_table(["起点", "时长", "配速", "后60%均HR", "心率法", "成绩法(Daniels)", "取值"],
                          [[fmt_dur(s["start_s"]), fmt_dur(s["mt"]), fmt_pace(s["pace"]), f"{s['hr']:.0f}",
                            fnum(s.get("est"), 1), fnum(s.get("perf"), 1), f"{s['v']:.1f}"] for s in segs]))
    L.append(f"\n{wx_line(sc)}；静息心率 {sc['rhr']}（{sc['rhr_src']}）、最大心率 {sc['hrmax']}；"
             f"未覆盖的维度留空。心率法对 RHR/HRmax 敏感：各偏差 3 bpm 约影响 1–1.5 分。")
    st = rolling_state(ctx.runs(None, d), d, ctx.cfg)
    if st.get("composite"):
        parts = [f"{DIM_LABEL[k]} {st[k]['v']:.1f}" + (f"（{st[k]['age']}天前·旧）" if st[k]["stale"] else "")
                 for k in DIM_ORDER if k in st]
        L.append(f"状态跑力（{ctx.cfg['score'].get('window_days', 42)}天加权，半衰期 {ctx.cfg['score'].get('half_life_days', 14)} 天）："
                 f"**综合 {st['composite']['v']:.1f}**（距破3 {signed(st['composite']['v'] - need, 1)}）；" + "，".join(parts) + "\n")
    else:
        L.append("")
    return L

def _row(m, slope, ref):
    d = m.get("dyn") or {}
    a = aei(m, slope, ref)
    return [m["date"][5:], label_type(m), fnum(m["dist"] / 1000, 1), fmt_dur(m["mt"]), fmt_speed_pace(m["speed"]),
            fmt_speed_pace(m.get("gap_speed")), fnum(m["hr"], 0), fnum(m.get("ef"), 2), fmt_speed_pace(a),
            fnum(comp_of(m), 1), signed(m.get("decoupling"), 1, "%"), fnum(d.get("cad"), 0), fnum(d.get("step"), 2),
            fnum(m.get("load"), 0), fnum(m.get("rpe"), 0)]


NEED_EVIDENCE = {
    "aerobic": "稳态轻松跑 ≥20 分钟（HR 120–163，热身 8 分钟后）",
    "threshold": "连续 ≥13 分钟 HR 157–177，或 ≥3 组 4–15 分钟的重复（去掉各组前 2 分钟后 HR 157–171）",
    "speed": "3–5 分钟重复、组内 HR 爬到 ≥172（如 5×1000m）",
    "endurance": "≥60 分钟的非间歇跑",
}
EXPECTED_DIMS = {"easy": ("aerobic",), "long": ("aerobic", "endurance"), "tempo": ("threshold",),
                 "interval": ("speed",), "race": ("threshold", "speed")}
SIGNAL_BAND = 1.5  # 单次波动 ±1.5 分内属正常


def _no_evidence_reason(k, m, ctx):
    cfg = ctx.cfg["score"]
    reps = m.get("reps") or []
    if k == "speed":
        short = [r for r in reps if (r.get("mt") or 0) < REP_MIN_S]
        ok = [r for r in reps if REP_MIN_S <= (r.get("mt") or 0) <= REP_MAX_S]
        if short and not ok:
            lo, hi = min(r["mt"] for r in short), max(r["mt"] for r in short)
            return (f"{len(short)} 个快段只有 {lo:.0f}–{hi:.0f} 秒，短于 {REP_MIN_S} 秒不计分"
                    "（心率跟不上速度，会高估摄氧量）；算神经肌肉/速度经济性刺激")
        if ok:
            return f"{len(ok)} 个快段时长够，但后段心率未达 {cfg['speed_hr_floor']}，外推不可靠"
        return "没有 2.5–12 分钟的快段"
    if k == "threshold":
        z3 = m.get("sustained_z3_min") or 0
        nr = len([r for r in reps if THR_REP_MIN_S <= (r.get("mt") or 0) <= THR_REP_MAX_S])
        if nr and nr < THR_REP_MIN_N:
            return f"4–15 分钟的重复只有 {nr} 组，<{THR_REP_MIN_N} 组"
        if nr:
            return "各组去掉前 2 分钟后 HR 不在 157–171（偏低或已到速度强度）"
        return (f"最长连续 HR≥157 只有 {z3:.0f} 分钟（<10），也没有 ≥{THR_REP_MIN_N} 组 4–15 分钟的重复"
                if z3 < 10 else "连续高强度段内配速波动 >8%，不算稳态阈值")
    if k == "endurance":
        if (m.get("mt") or 0) < 3600:
            return f"移动 {(m.get('mt') or 0) / 60:.0f} 分钟，<60 分钟"
        return "间歇课或前后 1/3 稳态片段不足"
    return "稳态片段不足 5 个（热身 8 分钟、间歇及其后恢复、HR 区间外都会被剔除）"


def contribution_section(ctx, m):
    """Per-run verdict for each ability dimension: improved / flat / lower / not judgeable (and why)."""
    sc = m.get("score") or {}
    d = parse_date(m["date"])
    prior = [x for x in ctx.runs(None, d) if x["start"] < m["start"]]
    before = rolling_state(prior, d, ctx.cfg)
    after = rolling_state(prior + [m], d, ctx.cfg)
    low = sc.get("conf") == "低"
    expected = EXPECTED_DIMS.get(m.get("type"), ())
    rows, missing = [], []
    for k in DIM_ORDER:
        val = dim_value(sc, k)
        b, a = before.get(k), after.get(k)
        ref = f"{b['v']:.1f}" + ("（旧）" if b["stale"] else "") if b else "-"
        if val is None:
            if k in expected:
                rows.append([DIM_LABEL[k], "-", ref, "-", "**无法判断**", _no_evidence_reason(k, m, ctx)])
            else:
                rows.append([DIM_LABEL[k], "-", ref, "-", "未涉及", _no_evidence_reason(k, m, ctx)])
            missing.append(k)
            continue
        if low:
            rows.append([DIM_LABEL[k], f"{val:.1f}", ref, "-", "**无法判断**",
                         "置信度低（证据少/跑步机/RHR 不可靠），不计入状态跑力"])
            missing.append(k)
            continue
        if not b:
            verdict, why = "首次证据", "此前 90 天无该维度证据"
        else:
            diff = val - b["v"]
            if diff >= SIGNAL_BAND:
                verdict, why = "↑ 提升信号", f"高于此前状态 {diff:+.1f}，超出 ±{SIGNAL_BAND} 正常波动；需下次同类课再确认"
            elif diff <= -SIGNAL_BAND:
                verdict, why = "↓ 低于状态", f"低于此前状态 {diff:+.1f}；先查天气、睡眠、前一天负荷，再看是否连续"
            else:
                verdict, why = "→ 持平", f"与此前状态差 {diff:+.1f}，在 ±{SIGNAL_BAND} 内，是维持不是提升"
        delta = a["v"] - b["v"] if a and b else None
        rows.append([DIM_LABEL[k], f"{val:.1f}", ref, signed(delta, 2) if delta is not None else "-", f"**{verdict}**", why])
    L = ["**本次对各能力子项的贡献**（单次值 vs 此前状态跑力；状态变化为加入本次后的变化）\n"]
    L.append(md_table(["子项", "本次", "此前状态", "状态变化", "判断", "说明"], rows))
    notes = []
    for k in missing:
        st = after.get(k)
        age = f"，状态最近证据 {st['age']} 天前" + ("（已过期，沿用旧值）" if st["stale"] else "") if st else "，近 90 天没有证据"
        stale = (not st) or st["stale"] or st["age"] >= 14
        if k not in expected and not stale:
            continue
        notes.append(f"- {DIM_LABEL[k]}未更新{age}" + (f"；要更新需要：{NEED_EVIDENCE[k]}" if stale else ""))
    if notes:
        L.append("\n" + "\n".join(notes))
    L.append("")
    return L


RUN_COLS = ["日期", "类型", "km", "时间", "配速", "GAP", "HR", "EF", "AeI", "跑力", "脱钩", "步频", "步幅m", "负荷", "RPE"]


def resolve_activity(ctx, ref):
    runs = ctx.runs()
    if not runs:
        return None
    if ref in (None, "latest", "last"):
        return runs[-1]
    if ref in ctx.rm:
        return ctx.rm[ref]
    same = [m for m in runs if m["date"] == ref]
    return max(same, key=lambda m: m["dist"] or 0) if same else None


def run_report(ref=None, sync_err=None):
    ctx = Ctx(sync_err)
    m = resolve_activity(ctx, ref)
    if not m:
        return f"找不到跑步：{ref}", None
    cfg = ctx.cfg
    lthr = cfg["hr"]["lthr"]
    d = parse_date(m["date"])
    fit = ctx.fit(m["date"])
    slope = slope_from(fit)
    ref_hr = ctx.ref_hr
    a_now = aei(m, slope, ref_hr)
    L = []
    L.append(f"# 跑步分析 {m['start'].replace('T', ' ')[:16]} · {m['name'] or ''} · {label_type(m)}\n")
    L.append(f"活动 {m['id']}｜设备 {m.get('device') or '-'}｜自动分类 `{m['type_auto']}`"
             + (f"，已人工改为 `{m['type']}`" if m["type"] != m["type_auto"] else "（可用 `log --type` 修正）"))
    others = [x for x in ctx.runs(d, d) if x["id"] != m["id"]]
    if others:
        L.append("同日其他跑步：" + "、".join(f"{x['start'][11:16]} {x['dist'] / 1000:.1f}km {label_type(x)}（{x['id']}）" for x in others))
    L.append(ctx.countdown(d) + "｜" + ctx.header_line() + "\n")

    # overview
    L.append("## 1. 概览\n")
    dyn = m.get("dyn") or {}
    rows = [
        ["距离", f"{m['dist'] / 1000:.2f} km", "移动/经过时间", f"{fmt_dur(m['mt'])} / {fmt_dur(m['et'])}"],
        ["平均配速", fmt_speed_pace(m["speed"]) + "/km", "GAP（坡度修正）", fmt_speed_pace(m.get("gap_speed")) + "/km"],
        ["平均/最高心率", f"{fnum(m['hr'], 0)} / {fnum(m['hr_max'], 0)}", "平均心率 %LTHR", fnum(m["hr"] / lthr * 100 if m["hr"] else None, 0, "%")],
        ["训练负荷 / TRIMP", f"{fnum(m['load'], 0)} / {fnum(m['trimp'], 0)}", "RPE / feel", f"{fnum(m['rpe'], 0)} / {fnum(m['feel'], 0)}"],
        ["EF（m/min/bpm）", fnum(m.get("ef"), 2), f"AeI（标准化到HR{ref_hr}的配速）", fmt_speed_pace(a_now)],
        ["脱钩 Pa:HR（本地/Intervals）", f"{signed(m.get('decoupling'), 1, '%')} / {signed(m.get('icu_decoupling'), 1, '%')}", "累计爬升", fnum(m.get("elev_gain"), 0, " m")],
        ["步频", fnum(dyn.get("cad"), 0, " spm"), "步幅", fnum(dyn.get("step"), 2, " m")],
        ["触地时间", fnum(dyn.get("gct"), 0, " ms"), "垂直振幅 / 垂直比", f"{fnum((dyn.get('vo') or 0) / 10 or None, 1, ' cm')} / {fnum(dyn.get('vr'), 1, '%')}"],
        ["功率", fnum(dyn.get("watts"), 0, " W"), "W/kg", fnum((dyn.get("watts") or 0) / cfg["athlete"]["weight_kg"] or None, 2)],
    ]
    L.append(md_table(["指标", "值", "指标", "值"], rows))
    L.append("")

    L += score_section(ctx, m)
    if m.get("score") is not None:
        L += contribution_section(ctx, m)

    # zones
    z = m.get("zone_secs") or []
    if z:
        tot = sum(z) or 1
        names = cfg["hr"]["zone_names"]
        lo = [0] + [x + 1 for x in ctx.zones[:-1]]
        L.append("## 3. 心率区间分布\n")
        L.append(md_table(["区间", "bpm", "时间", "占比"],
                          [[names[i], f"{lo[i]}–{ctx.zones[i]}", fmt_dur(z[i]), pct(z[i], tot)] for i in range(min(7, len(z))) if z[i] > 0]))
        L.append(f"\n低强度(Z1–2) {pct(sum(z[:2]), tot)}｜中(Z3) {pct(z[2] if len(z) > 2 else 0, tot)}｜高(Z4+) {pct(sum(z[3:]), tot)}；"
                 f"最长连续 Z3+ {fnum(m.get('sustained_z3_min'), 0)} 分钟，Z4+ {fnum(m.get('sustained_z4_min'), 0)} 分钟\n")

    # splits
    sp = m.get("splits") or []
    if sp:
        L.append("## 4. 公里分段\n")
        rows = []
        for s in sp:
            rows.append([s["km"] if not s.get("partial") else f"{s['km']}*", fmt_speed_pace(s["speed"]),
                         fmt_speed_pace(s.get("gap_speed")), fnum(s["hr"], 0), fnum(s["hr_max"], 0), fnum(s["cad"], 0),
                         fnum(s["speed"] / (s["cad"] / 60) if s.get("cad") and s.get("speed") else None, 2),
                         f"+{s.get('gain', 0):.0f}/-{s.get('loss', 0):.0f}", fnum(s.get("watts"), 0)])
        L.append(md_table(["km", "配速", "GAP≈", "HR", "最高", "步频", "步幅", "升/降m", "功率"], rows))
        L.append(f"\n分段配速变异系数 CV {fnum(m.get('split_cv'), 1, '%')}（稳态跑 <3% 为稳定）\n")

    # durability
    if m.get("halves"):
        h1, h2 = m["halves"]
        th = m.get("thirds") or []
        L.append("## 5. 稳定性与耐力（去掉前5分钟热身）\n")
        rows = [["前半", fmt_speed_pace(h1["speed"]), fnum(h1["hr"], 0), fnum(h1["speed"] * 60 / h1["hr"] if h1["hr"] else None, 2)],
                ["后半", fmt_speed_pace(h2["speed"]), fnum(h2["hr"], 0), fnum(h2["speed"] * 60 / h2["hr"] if h2["hr"] else None, 2)]]
        if len(th) == 3:
            rows += [[f"第{i + 1}/3", fmt_speed_pace(t["speed"]), fnum(t["hr"], 0), fnum(t["speed"] * 60 / t["hr"] if t["hr"] else None, 2)]
                     for i, t in enumerate(th)]
        L.append(md_table(["段", "配速", "HR", "EF"], rows))
        dp = (pace_s(h2["speed"]) or 0) - (pace_s(h1["speed"]) or 0)
        dh = (h2["hr"] or 0) - (h1["hr"] or 0)
        L.append(f"\n后半 vs 前半：配速 {signed_pace(dp)}，心率 {signed(dh, 0, ' bpm')}；脱钩 {signed(m.get('decoupling'), 1, '%')}"
                 f"（<5% 有氧耐力良好，5–10% 一般，>10% 该时长/强度下耐力不足或受热/脱水影响）")
        dt3 = m.get("dyn_thirds")
        if dt3:
            a, b = dt3
            L.append(f"跑姿首1/3→末1/3：步频 {fnum(a.get('cad'), 0)}→{fnum(b.get('cad'), 0)}，步幅 {fnum(a.get('step'), 2)}→{fnum(b.get('step'), 2)} m，"
                     f"触地 {fnum(a.get('gct'), 0)}→{fnum(b.get('gct'), 0)} ms，垂直振幅 {fnum((a.get('vo') or 0) / 10 or None, 1)}→{fnum((b.get('vo') or 0) / 10 or None, 1)} cm"
                     "（后程触地↑/步频↓ 是疲劳征象）")
        L.append("")

    # reps / strides
    reps, strides = m.get("reps") or [], m.get("strides") or []
    if reps:
        L.append("## 6. 快段 / 间歇\n")
        rows = [[i + 1, fnum(r["dist"], 0, "m"), fmt_dur(r["mt"]), fmt_speed_pace(r["speed"]), fnum(r.get("hr"), 0),
                 fnum(r.get("hr_max"), 0), fnum(r.get("cad"), 0), fnum(r.get("hrr60"), 0)] for i, r in enumerate(reps)]
        L.append(md_table(["#", "距离", "时间", "配速", "均HR", "最高HR", "步频", "60s心率回落"], rows))
        ps = [pace_s(r["speed"]) for r in reps if r.get("speed")]
        if len(ps) >= 2:
            L.append(f"\n快段均配速 {fmt_pace(mean(ps))}，CV {fnum((stdev(ps) or 0) / mean(ps) * 100, 1, '%')}，"
                     f"末段 vs 首段 {signed_pace(ps[-1] - ps[0])}；快段总时长 {fmt_dur(sum(r['mt'] for r in reps))}")
        L.append("")
    if strides:
        ps = [pace_s(r["speed"]) for r in strides]
        L.append(f"加速跑/短冲 {len(strides)} 次，均配速 {fmt_pace(mean(ps))}，最快 {fmt_pace(min(ps))}\n")

    # best efforts
    bd = m.get("best_dist") or {}
    if bd:
        prior90 = ctx.runs(d - dt.timedelta(days=90), d)
        prior90 = [x for x in prior90 if x["id"] != m["id"]]
        pbs = cfg.get("pb", {})
        rows = []
        for lbl, r in bd.items():
            if lbl in ("400m",) and not reps and not strides:
                continue
            b = best_in(prior90, "best_dist", lbl)
            new = b is None or r["time"] < b[0]["time"]
            rows.append([lbl, fmt_dur(r["time"]), fmt_pace(r["time"] / DIST_KM[lbl]),
                         fnum(r.get("hr"), 0), (fmt_dur(b[0]["time"]) + f"（{b[1]['date'][5:]}）") if b else "-",
                         "🔥 90天最佳" if new else "", fmt_dur(pbs[lbl]) if lbl in pbs else "-"])
        L.append("## 7. 本次最佳分段（训练中截取，非全力测试）\n")
        L.append(md_table(["距离", "本次", "配速", "HR", "此前90天最佳", "", "PB"], rows))
        L.append("")

    # comparison with same group
    grp = TYPE_GROUP.get(m["type"], "easy")
    hist = [x for x in ctx.runs(d - dt.timedelta(days=180), d) if TYPE_GROUP.get(x["type"]) == grp and x["id"] != m["id"]
            and x["start"] < m["start"]]
    same_era = [x for x in hist if x["era"] == m["era"]]
    hist = (same_era if len(same_era) >= 3 else hist)[-8:]
    L.append(f"## 8. 同类对比：{GROUP_LABEL.get(grp, grp)}（近180天最多8次，同设备优先；AeI 斜率 {slope:.4f} m/s/bpm）\n")
    if hist:
        rows = [["**本次**"] + _row(m, slope, ref_hr)[1:]] + [_row(x, slope, ref_hr) for x in reversed(hist)]
        L.append(md_table(RUN_COLS, rows))
        med = lambda f: median([f(x) for x in hist])
        comp = [
            ("配速", pace_s(m["speed"]), med(lambda x: pace_s(x["speed"])), "pace"),
            ("平均心率", m["hr"], med(lambda x: x["hr"]), "num"),
            ("EF", m.get("ef"), med(lambda x: x.get("ef")), "num2"),
            (f"AeI@{ref_hr}", pace_s(a_now), med(lambda x: pace_s(aei(x, slope, ref_hr))), "pace"),
            ("脱钩%", m.get("decoupling"), med(lambda x: x.get("decoupling")), "num"),
            ("步频", dyn.get("cad"), med(lambda x: (x.get("dyn") or {}).get("cad")), "num"),
            ("步幅", dyn.get("step"), med(lambda x: (x.get("dyn") or {}).get("step")), "num2"),
            ("触地ms", dyn.get("gct"), med(lambda x: (x.get("dyn") or {}).get("gct")), "num"),
        ]
        if grp == "interval":
            rp = lambda x: median([pace_s(r["speed"]) for r in (x.get("reps") or []) if r.get("speed")])
            rh = lambda x: median([r.get("hr") for r in (x.get("reps") or [])])
            comp += [("快段中位配速", rp(m), med(rp), "pace"), ("快段中位HR", rh(m), med(rh), "num")]
        rows = []
        for name, now, ref_v, kind in comp:
            if now is None or ref_v is None:
                continue
            if kind == "pace":
                rows.append([name, fmt_pace(now), fmt_pace(ref_v), signed_pace(now - ref_v)])
            else:
                nd = 2 if kind == "num2" else 1
                rows.append([name, fnum(now, nd), fnum(ref_v, nd), signed(now - ref_v, nd)])
        L.append("\n本次 vs 同类中位数：\n")
        L.append(md_table(["指标", "本次", "同类中位", "差值"], rows))
        pts = [((parse_date(x["date"]) - d).days, pace_s(aei(x, slope, ref_hr))) for x in hist + [m] if aei(x, slope, ref_hr)]
        if len(pts) >= 4:
            r = linreg([p[0] for p in pts], [p[1] for p in pts])
            if r:
                L.append(f"\nAeI 趋势：{r[1] * 7:+.1f} s/km/周（负数=同心率下变快；n={len(pts)}，R²={r[2]:.2f}）")
        if any(x["era"] != m["era"] for x in hist):
            L.append("⚠️ 对比样本含不同设备时代，心率/跑姿数据仅作方向参考。")
    else:
        L.append("- 近180天没有同类跑步")
    L.append("")

    # all-run aerobic context
    prior28 = [x for x in ctx.runs(d - dt.timedelta(days=28), d) if x["id"] != m["id"] and x.get("bins_for_fit")]
    base = median([aei(x, slope, ref_hr) for x in prior28])
    if a_now and base:
        L.append(f"**有氧状态**：本次 AeI {fmt_speed_pace(a_now)} vs 近28天全部非间歇跑中位 {fmt_speed_pace(base)}（{signed_pace(pace_s(a_now) - pace_s(base))}）\n")
    if fit:
        mp_hr = [155, 160, 163]
        L.append(f"近90天心率-配速曲线（n={fit['n']} 个60s片段，HR {fit['hr_min']:.0f}–{fit['hr_max']:.0f}，R²={fit['r2']:.2f}）：" +
                 "，".join(f"HR{h}→{fmt_speed_pace(fit['a'] + fit['b'] * h)}" for h in [140, 145, 150] + mp_hr) + "\n")

    # load context
    ld = load_context(ctx, d)
    ws = week_start(d)
    L.append("## 9. 负荷背景\n")
    L.append(f"- 本周（{ws}起）至今跑量 {ctx.run_km(ws, d):.1f} km；近7天 {ld['km7']:.1f} km / 此前4周周均 {ld['chronic']:.1f} km"
             f"（比值 {fnum(ld['acwr'], 2)}）；近7天 {ld['runs7']} 次")
    L.append(f"- 本次负荷 {fnum(m['load'], 0)}，近28天单次跑步负荷中位 {fnum(median([x['load'] for x in ctx.runs(d - dt.timedelta(days=28), d)]), 0)}")
    same_day = [a for a in ctx.acts_on(d) if not is_run(a) and (a.get("moving_time") or 0) >= 900]
    if same_day:
        L.append("- 当天其他运动：" + "、".join(f"{a['type']} {fmt_hm(a.get('moving_time'))} 负荷{a.get('icu_training_load') or 0}" for a in same_day))
    L.append("")

    fbs = ctx.feedback_for(m["id"])
    L.append("## 10. 主观反馈\n")
    if fbs:
        for f in fbs:
            L.append(f"- {fb_line(f)}")
        got = {k for f in fbs for k in f}
        missing = [name for key, name in (("rpe", "RPE"), ("pain", "疼痛 0–10 及位置"), ("gait", "步态"), ("fuel", "补给")) if key not in got]
        if missing:
            L.append("- 还缺：" + "、".join(missing))
    else:
        L.append("- 尚未记录。需要：实际距离/时间是否与记录一致、RPE 1–10、跑中/跑后/次日疼痛(0–10)及位置、是否改变步态、异常疲劳、补给。")
    text = "\n".join(L) + "\n"
    p = save("runs", f"{m['date']}_{m['id']}.md", text)
    return text, p


# ================================================================ weekly

def week_summary(ctx, ws):
    we = ws + dt.timedelta(days=6)
    rs = ctx.runs(ws, we)
    acts = [a for a in ctx.acts.values() if str(ws) <= (a.get("start_date_local") or "")[:10] <= str(we)]
    oth = [a for a in acts if not is_run(a)]
    km = sum(m["dist"] for m in rs) / 1000
    mt = sum(m["mt"] for m in rs)
    return {
        "ws": ws, "we": we, "runs": rs, "km": km, "mt": mt, "n": len(rs),
        "long": max([m["dist"] for m in rs], default=0) / 1000,
        "quality": [m for m in rs if m["type"] in QUALITY_TYPES],
        "run_load": sum(m["load"] or 0 for m in rs), "other_load": sum(a.get("icu_training_load") or 0 for a in oth),
        "other": oth, "pace": mt / km if km else None,
        "days_run": len({m["date"] for m in rs}),
        "ctl": ctx.w(we, "ctl") or ctx.w(we - dt.timedelta(days=1), "ctl"),
        "atl": ctx.w(we, "atl") or ctx.w(we - dt.timedelta(days=1), "atl"),
        "placeholders": [a for a in acts if is_placeholder(a)],
    }


def score_trend(ctx, weeks):
    """12-week rolling state per dimension (VDOT scale), sampled at each week end."""
    all_runs = ctx.runs()
    need = goal_vdot(ctx.cfg["race"]["goal_s"])
    rows, comps = [], []
    for x in weeks:
        st = rolling_state(all_runs, x["we"], ctx.cfg)
        best = max([comp_of(m) for m in x["runs"] if comp_of(m) and m["score"]["conf"] != "低"], default=None)
        c = st.get("composite")
        comps.append(c["v"] if c else None)
        cell = lambda k: (f"{st[k]['v']:.1f}" + ("*" if st[k].get("stale") else "")) if k in st else "-"
        rows.append([iso_week_label(x["ws"])[5:], cell("composite"), signed(c["v"] - need, 1) if c else "-",
                     cell("aerobic"), cell("threshold"), cell("speed"), cell("endurance"), fnum(best, 1)])
    out = [f"**跑力评分趋势（周末状态值，VDOT 刻度；破3 ≈ {need}）**\n",
           md_table(["周", "综合", "距破3", "有氧", "阈值", "速度", "耐力", "本周单次最高"], rows),
           f"\n综合 {spark(comps)}；* = 该维度42天内无新证据，沿用90天内的旧值。趋势图见训练中心「能力」页（`python3 -m coach serve`）。\n"]
    return out + readiness_lines(ctx, weeks[-1]["we"])


def readiness_lines(ctx, d):
    r = assess(ctx, d)
    if r.get("progress") is None:
        return []
    v = r["volume"]
    L = [f"**破3准备度（截至 {d}，模型估计）**\n",
         f"- 能力：状态综合 {r['now']:.1f}，距破3 {r['gap']:+.1f} 分。其中 {r['regain_left']:.1f} 分是回到自己的历史峰值 "
         f"{r['peak']:.1f}（{r['peak_date'] or '配置值'}）以内，{r['new_left']:.1f} 分是超过峰值的新高度（按 {r['above_peak_cost']:g} 倍代价计）。",
         f"- 难度加权进度：{r['progress']:.0f}%（赛季起点 {r['season_start']} 的状态 {r['season_base']:.1f} = 0%，破3 = 100%）；"
         f"匀速完成所需的进度线今天应在 {r['progress_expected']:.0f}%（{r['expected_now']:.1f}），{'领先' if r['ahead'] >= 0 else '落后'} {abs(r['ahead']):.1f} 分。",
         f"- 所需进步速度：距能力到位日 {r['ready_by']} 还有 {r['weeks_left']:.1f} 周，需要平均每周 {r['req_rate']:.2f} 分（超过峰值后折算为每周 "
         f"{r['req_rate'] / r['above_peak_cost']:.2f} 分）"
         + (f"，约为你历史上最快 8 周（{r['best_from']} → {r['best_to']}，{r['best_rate']:.2f} 分/周）的 {r['req_ratio'] * 100:.0f}%，并要连续保持。"
            if r.get("req_ratio") else "。"),
         f"- 跑量（Tanda 模型）：近 8 周周均 {v['k8']:.1f} km、平均配速 {fmt_pace(v['pace8'])} → {fmt_dur(v['tanda_now'])}"
         + (f"；赛季计划赛前 8 周周均 {v['plan_k8']:.1f} km，配速不变时 → {fmt_dur(v['tanda_plan'])}，要到 3:00 需平均训练配速 ≤{fmt_pace(v['need_pace_at_plan'])}"
            if v.get("plan_k8") else "")
         + (f"。同一模型在你 {r['peak_date']} 的历史峰值时给出 {fmt_dur(v['tanda_peak'])}（周均 {v['peak_k8']:.1f} km），"
            f"而同期半马 PB {fmt_dur(ctx.cfg['pb'].get('半马'))}（Daniels 等效全马约 {fmt_dur(time_for_vdot(vdot(21097.5, ctx.cfg['pb']['半马']), 42195))}），"
            "说明它对你偏保守，只用来看跑量方向。"
            if v.get("tanda_peak") and ctx.cfg["pb"].get("半马") else "。"),
         ""]
    return L


def weekly(ws=None, sync_err=None):
    ctx = Ctx(sync_err)
    cfg = ctx.cfg
    t = today()
    if ws is None:
        ws = week_start(t) if t.weekday() == 6 else week_start(t) - dt.timedelta(days=7)
    ws = week_start(ws)
    wk = iso_week_label(ws)
    W = week_summary(ctx, ws)
    we = W["we"]
    slope = ctx.slope(we)
    ref_hr = ctx.ref_hr
    L = [f"# 周复盘 {wk}（{ws} ~ {we}）\n", ctx.countdown(we) + "｜" + ctx.header_line() + "\n"]
    row = ctx.plan_row(wk)
    if row:
        L.append(f"赛季计划本周：阶段 {row.get('阶段')}｜目标 {row.get('目标跑量km')} km｜长距离 {row.get('长距离')}｜关键课 {row.get('关键课')}\n")

    # 1 summary
    prev = [week_summary(ctx, ws - dt.timedelta(days=7 * k)) for k in range(1, 5)]
    avg4 = mean([p["km"] for p in prev])
    L.append("## 1. 本周概览\n")
    rows = [
        ["跑量", f"{W['km']:.1f} km", f"上周 {prev[0]['km']:.1f}｜前4周均 {avg4:.1f}"],
        ["跑步次数 / 天数", f"{W['n']} / {W['days_run']}", ""],
        ["跑步时间", fmt_hm(W["mt"]), f"平均配速 {fmt_pace(W['pace'])}/km"],
        ["最长一次", f"{W['long']:.1f} km", f"占周跑量 {pct(W['long'], W['km'])}"],
        ["质量课", str(len(W["quality"])), "、".join(f"{m['date'][5:]} {label_type(m)}" for m in W["quality"]) or "-"],
        ["跑步负荷 / 其他运动负荷", f"{W['run_load']:.0f} / {W['other_load']:.0f}", ""],
        ["周末 CTL / ATL / TSB", f"{fnum(W['ctl'])} / {fnum(W['atl'])} / {fnum((W['ctl'] or 0) - (W['atl'] or 0))}", ""],
    ]
    if W["placeholders"]:
        rows.append(["⚠️ 无法读取的活动", str(len(W["placeholders"])), "Strava 占位，不能当作零训练"])
    L.append(md_table(["项目", "本周", "对比"], rows))
    L.append("")

    # 2 runs
    L.append("## 2. 每次跑步\n")
    L.append(md_table(RUN_COLS, [_row(m, slope, ref_hr) for m in W["runs"]]) if W["runs"] else "- 本周无跑步")
    L.append("")

    # 3 intensity
    z = zone_minutes(W["runs"])
    tot = sum(z)
    if tot:
        L.append("## 3. 强度分布（跑步心率区间时间）\n")
        L.append(f"低(Z1–2) {pct(sum(z[:2]), tot)}（{fmt_hm(sum(z[:2]))}）｜中(Z3) {pct(z[2], tot)}（{fmt_hm(z[2])}）｜高(Z4+) {pct(sum(z[3:]), tot)}（{fmt_hm(sum(z[3:]))}）"
                 "；基础/专项期参考：低 ≥75–80%，高 5–10%\n")
    # other sports
    if W["other"]:
        by = {}
        for a in W["other"]:
            b = by.setdefault(a["type"], [0, 0, 0])
            b[0] += 1
            b[1] += a.get("moving_time") or 0
            b[2] += a.get("icu_training_load") or 0
        L.append("其他运动：" + "；".join(f"{k} {v[0]}次 {fmt_hm(v[1])} 负荷{v[2]:.0f}" for k, v in sorted(by.items(), key=lambda kv: -kv[1][2])) + "\n")

    # 4 recovery
    L.append("## 4. 恢复指标（周均）\n")
    rows = []
    for key, name, sc, nd in [("hrv", "HRV", 1, 0), ("restingHR", "静息心率", 1, 0), ("sleepSecs", "睡眠 h", 1 / 3600, 1),
                              ("sleepScore", "睡眠评分", 1, 0), ("readiness", "Readiness", 1, 0)]:
        def wavg(a, b):
            vals = [ctx.w(a + dt.timedelta(days=k), key) for k in range((b - a).days + 1)]
            vals = [v * sc for v in vals if v is not None]
            return mean(vals)
        cur = wavg(ws, we)
        base = wavg(max(ws - dt.timedelta(days=28), parse_date(ctx.apple_since)), ws - dt.timedelta(days=1))
        rows.append([name, fnum(cur, nd), fnum(base, nd), signed((cur - base) if cur is not None and base is not None else None, nd)])
    short = sum(1 for k in range(7) if (ctx.w(ws + dt.timedelta(days=k), "sleepSecs") or 99999) < 6 * 3600)
    L.append(md_table(["指标", "本周", "前4周", "差"], rows))
    L.append(f"\n睡眠 <6h 的晚上：{short} 天\n")

    # 5 subjective
    fbw = [f for f in ctx.fb if str(ws) <= (f.get("date") or "") <= str(we)]
    L.append("## 5. 主观反馈\n")
    if fbw:
        pains = [f["pain"] for f in fbw if f.get("pain") is not None]
        L.append(f"- 最大疼痛 {max(pains):g}/10（{len(pains)} 条有疼痛评分）；记录 {len(fbw)} 条" if pains
                 else f"- 记录 {len(fbw)} 条，都没有疼痛评分（需向用户补问）")
        for f in fbw:
            L.append(f"  - {f['date']}：{fb_line(f)}")
    else:
        L.append("- 本周无 journal 记录（需向用户补问疼痛与疲劳情况）")
    L.append("")

    # 6 trend 12 weeks
    L.append("## 6. 12周趋势\n")
    weeks = [week_summary(ctx, ws - dt.timedelta(days=7 * k)) for k in range(11, -1, -1)]
    rows, kms, aeis, longs = [], [], [], []
    for x in weeks:
        a_list = [aei(m, slope, ref_hr) for m in x["runs"] if m.get("bins_for_fit") and TYPE_GROUP.get(m["type"]) in ("easy", "long")]
        a_med = median(a_list)
        kms.append(x["km"])
        longs.append(x["long"])
        aeis.append(-pace_s(a_med) if a_med else None)
        rows.append([iso_week_label(x["ws"])[5:], f"{x['km']:.1f}", x["n"], f"{x['long']:.1f}", len(x["quality"]),
                     f"{x['run_load']:.0f}/{x['other_load']:.0f}", fnum(x["ctl"], 0), fmt_speed_pace(a_med)])
    L.append(md_table(["周", "km", "次", "最长", "质量", "跑/其他负荷", "CTL", f"AeI@{ref_hr}"], rows))
    L.append(f"\n跑量 {spark(kms)}｜最长 {spark(longs)}｜AeI（越高越快）{spark(aeis)}\n")

    # 7 ability panel
    L.append("## 7. 能力面板\n")
    L += score_trend(ctx, weeks)
    f_now = ctx.fit(we, 28)
    f_prev = ctx.fit(we - dt.timedelta(days=28), 28)
    rows = []
    for h in (145, 150, 155, 160, 163):
        now = f_now["a"] + f_now["b"] * h if f_now else None
        pv = f_prev["a"] + f_prev["b"] * h if f_prev else None
        rows.append([f"HR {h}", fmt_speed_pace(now), fmt_speed_pace(pv),
                     signed_pace(pace_s(now) - pace_s(pv)) if now and pv else "-"])
    L.append("**有氧（心率-配速曲线，60s稳态片段回归）**\n")
    L.append(md_table(["心率", "近28天", "再往前28天", "变化"], rows))
    if f_now:
        L.append(f"\n近28天拟合 n={f_now['n']}，HR 覆盖 {f_now['hr_min']:.0f}–{f_now['hr_max']:.0f}，R²={f_now['r2']:.2f}；超出覆盖范围的值是外推。\n")
    else:
        L.append("\n近28天稳态数据不足，无法拟合。\n")

    r28 = ctx.runs(we - dt.timedelta(days=27), we)
    r90 = ctx.runs(we - dt.timedelta(days=89), we)
    longs28 = [m for m in r28 if TYPE_GROUP.get(m["type"]) == "long"]
    L.append("**耐力**\n")
    L.append(f"- 近28天最长 {max([m['dist'] for m in r28], default=0) / 1000:.1f} km；≥90分钟跑 {sum(1 for m in r28 if m['mt'] >= 5400)} 次；"
             f"≥25km {sum(1 for m in r90 if m['dist'] >= 25000)} 次（近90天）")
    if longs28:
        L.append("- 长距离脱钩：" + "，".join(f"{m['date'][5:]} {m['dist'] / 1000:.1f}km {signed(m.get('decoupling'), 1, '%')}" for m in longs28))
    L.append("")

    L.append("**阈值与速度（近90天训练中最佳分段；非全力测试时偏保守）**\n")
    rows = []
    vds = []
    hard_hr = 0.9 * cfg["hr"]["lthr"]
    for lbl, dist in (("1km", 1000), ("3km", 3000), ("5km", 5000), ("10km", 10000), ("半马", 21097.5)):
        b = best_in(r90, "best_dist", lbl)
        if b:
            vd = vdot(dist, b[0]["time"])
            hard = (b[0].get("hr") or 0) >= hard_hr
            if dist >= 3000 and hard:
                vds.append((vd, lbl, b))
            rows.append([lbl, fmt_dur(b[0]["time"]), fmt_pace(b[0]["time"] / dist * 1000), fnum(b[0].get("hr"), 0), b[1]["date"],
                         fnum(vd, 1) + ("" if hard else "（非用力，不计入预测）")])
    for lbl, secs in (("20min", 1200), ("30min", 1800), ("60min", 3600)):
        b = best_in(r90, "best_dur", lbl)
        if b:
            dist = b[0]["speed"] * secs
            rows.append([lbl, f"{dist / 1000:.2f} km", fmt_speed_pace(b[0]["speed"]), fnum(b[0].get("hr"), 0), b[1]["date"], fnum(vdot(dist, secs), 1)])
    L.append(md_table(["项目", "成绩", "配速", "HR", "日期", "VDOT"], rows) if rows else "- 无")
    L.append("")

    easy28 = [m for m in r28 if TYPE_GROUP.get(m["type"]) in ("easy", "long")]
    easy_prev = [m for m in ctx.runs(we - dt.timedelta(days=55), we - dt.timedelta(days=28)) if TYPE_GROUP.get(m["type"]) in ("easy", "long")]
    g = lambda ms, k: median([(m.get("dyn") or {}).get(k) for m in ms])
    L.append("**跑姿经济性（轻松/长距离跑中位数）**\n")
    L.append(md_table(["", "步频", "步幅m", "触地ms", "垂直振幅cm", "平均配速"],
                      [["近28天", fnum(g(easy28, "cad"), 0), fnum(g(easy28, "step"), 2), fnum(g(easy28, "gct"), 0), fnum((g(easy28, "vo") or 0) / 10 or None, 1), fmt_speed_pace(median([m["speed"] for m in easy28]))],
                       ["前28天", fnum(g(easy_prev, "cad"), 0), fnum(g(easy_prev, "step"), 2), fnum(g(easy_prev, "gct"), 0), fnum((g(easy_prev, "vo") or 0) / 10 or None, 1), fmt_speed_pace(median([m["speed"] for m in easy_prev]))]]))
    L.append("")

    # 8 predictions & gap
    goal = cfg["race"]["goal_s"]
    L.append("## 8. 全马预测与破3差距\n")
    preds = []
    if vds:
        vd, lbl, b = max(vds, key=lambda x: x[0])
        preds.append(["VDOT（近90天最佳 %s）" % lbl, fmt_dur(time_for_vdot(vd, 42195)), f"VDOT {vd:.1f}；假设耐力训练充分"])
    for lbl, dist in (("半马", 21097.5), ("10km", 10000)):
        b = best_in(r90, "best_dist", lbl, min_hr=hard_hr)
        if b:
            preds.append([f"Riegel 1.06（{lbl} {fmt_dur(b[0]['time'])}）", fmt_dur(riegel(b[0]["time"], dist, 42195)), "理想耐力换算"])
    r56 = ctx.runs(we - dt.timedelta(days=55), we)
    K = sum(m["dist"] for m in r56) / 1000 / 8
    tot_mt, tot_d = sum(m["mt"] for m in r56), sum(m["dist"] for m in r56)
    P = tot_mt / tot_d * 1000 if tot_d else None
    if P:
        preds.append([f"Tanda（8周周均 {K:.1f} km，平均配速 {fmt_pace(P)}）", fmt_dur(tanda(K, P)), "基于训练量与训练配速；低跑量时误差大"])
    if f_now:
        for h in (160, 163):
            sp = f_now["a"] + f_now["b"] * h
            preds.append([f"心率曲线：HR{h} 的配速 × 42.195", fmt_dur(42195 / sp), "只代表心肺；不含后程耐力衰减" + ("（外推）" if h > f_now["hr_max"] else "")])
    for name, pb_s, dist in (("PB 半马 1:29:33", cfg["pb"].get("半马"), 21097.5), ("PB 10km", cfg["pb"].get("10km"), 10000)):
        if pb_s:
            preds.append([f"历史 {name}（VDOT {vdot(dist, pb_s):.1f}）", fmt_dur(time_for_vdot(vdot(dist, pb_s), 42195)), "2025年春状态，非当前"])
    L.append(md_table(["模型", "全马预测", "说明"], preds))
    need_vd = next(v / 10 for v in range(400, 800) if time_for_vdot(v / 10, 42195) <= goal)
    t_pace = 3600 / next(d_ / 1000 for d_ in range(8000, 22000, 10) if time_for_vdot(need_vd, d_) >= 3600) if need_vd else None
    L.append(f"\n破3所需：VDOT ≈ **{need_vd:.1f}**；对应 10km ≈ {fmt_dur(time_for_vdot(need_vd, 10000))}、半马 ≈ {fmt_dur(time_for_vdot(need_vd, 21097.5))}、"
             f"阈值（60分钟比赛）配速 ≈ {fmt_pace(t_pace)}/km、马拉松配速 {fmt_pace(goal / 42.195)}/km")
    tneed = [f"{k}km/周→{fmt_pace((goal / 42.195 - 17.1 - 140 * 2.718281828 ** (-0.0053 * k)) / 0.55)}" for k in (70, 80, 90, 100)]
    L.append(f"Tanda 模型破3需要的8周平均训练配速：" + "，".join(tneed) + "\n")

    mp_km = sum(1 for m in r28 if m["type"] != "race" for s in (m.get("splits") or [])
                if not s.get("partial") and s.get("speed") and MP_BAND[0] <= pace_s(s["speed"]) <= MP_BAND[1])
    longest_12w = max([m["dist"] for m in ctx.runs(we - dt.timedelta(days=83), we)], default=0) / 1000
    fb_pain = [f for f in ctx.fb if (f.get("pain") or 0) >= 3]
    last_pain = max((f["date"] for f in fb_pain), default=None)
    rows = [
        ["周跑量", f"本周 {W['km']:.0f}｜8周均 {K:.0f} km", "基础期 60–75，专项期峰值 80–95 km（按胫骨耐受逐步到达）"],
        ["长距离", f"近12周最长 {longest_12w:.1f} km", "12月起 ≥30km；赛前10周内 3–5 次 30–35 km"],
        ["马配专项量", f"近28天 {mp_km} km 分段落在 {fmt_pace(MP_BAND[0])}–{fmt_pace(MP_BAND[1])}", "赛前8周累计 60–100 km，单次 16–25 km"],
        ["阈值", "见能力面板 20/30/60min", f"≈{fmt_pace(t_pace)}/km 持续 30–40 分钟（分段）"],
        ["有氧效率", f"HR160 配速 {fmt_speed_pace(f_now['a'] + f_now['b'] * 160) if f_now else '-'}", f"马拉松心率区（约158–163）下 ≤{fmt_pace(goal / 42.195)}"],
        ["连续性", f"近8周中每周跑≥3次的有 {sum(1 for x in weeks[-8:] if x['n'] >= 3)} 周", "连续 16+ 周无伤、每周 5–6 次"],
        ["伤痛", f"最近一次疼痛≥3：{last_pain or '无记录'}", "胫骨疼痛 0–2 且不上升"],
    ]
    L.append(md_table(["维度", "当前", "破3参考标准"], rows))
    L.append("")

    # 9 next week envelope
    nws = ws + dt.timedelta(days=7)
    nwk = iso_week_label(nws)
    nrow = ctx.plan_row(nwk)
    last, a4 = W["km"], avg4 or 0
    build_weeks = 0
    for i in range(len(weeks) - 1, 0, -1):
        if weeks[i]["km"] > weeks[i - 1]["km"] * 1.02:
            build_weeks += 1
        else:
            break
    lv, st = style(cfg)
    if a4 and last > st["spike"] * a4:
        env = (last * 0.85, last * 1.0, f"本周是前4周均值的 {last / a4:.1f} 倍（>{st['spike']:.2f}），下周不加量，先观察胫骨/跟腱反应")
    elif build_weeks >= st["build_weeks"]:
        env = (last * st["deload"][0], last * st["deload"][1], f"已连续 {build_weeks} 周加量，建议减量周")
    else:
        env = (last * 1.0, last * (1 + st["ramp"]), f"常规递增 ≤{st['ramp'] * 100:.0f}%")
    L.append(f"## 9. 下周（{nwk}，{nws} 起）规则建议\n")
    L.append(f"- 激进程度：**{lv} {st['name']}**（`athlete.toml [plan]`；只影响负荷规则，疼痛/红旗规则不变）")
    L.append(f"- 跑量区间：**{env[0]:.0f}–{env[1]:.0f} km**（{env[2]}）")
    if nrow:
        L.append(f"- 赛季计划：阶段 {nrow.get('阶段')}，目标 {nrow.get('目标跑量km')} km，长距离 {nrow.get('长距离')}，关键课 {nrow.get('关键课')}；"
                 f"{POLICY[st['plan_policy']]}")
    L.append(f"- 长距离上限：≤ 周跑量 {st['long_pct'] * 100:.0f}% 且 ≤ 近4周最长 {max([m['dist'] for m in r28], default=0) / 1000:.1f} km + {st['long_step']} km")
    L.append(f"- 质量课：每周 ≤{st['quality']} 次（长距离里的马配段算一次）；客观黄灯时：{st['yellow_cut']}")
    L.append("- 规则建议只是边界；具体课表由教练结合伤痛、主观疲劳、其他运动安排制定，写入 plan/weeks/%s.md" % nwk)
    text = "\n".join(L) + "\n"
    p = save("weekly", f"{wk}.md", text)
    return text, p


# ================================================================ list

def list_runs(days=42, group=None, sync_err=None):
    ctx = Ctx(sync_err)
    t = today()
    ms = ctx.runs(t - dt.timedelta(days=days), t)
    if group:
        ms = [m for m in ms if TYPE_GROUP.get(m["type"]) == group or m["type"] == group]
    slope = ctx.slope(t)
    cols = ["ID"] + RUN_COLS
    rows = [[m["id"]] + _row(m, slope, ctx.ref_hr) for m in ms]
    return f"# 近{days}天跑步（{len(ms)} 次）\n\n" + md_table(cols, rows) + "\n", None
