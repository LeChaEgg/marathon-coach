import datetime as dt
import math
import statistics

SPARK = "▁▂▃▄▅▆▇█"


def today():
    return dt.date.today()


def parse_date(s):
    if isinstance(s, dt.date):
        return s
    return dt.date.fromisoformat(str(s)[:10])


def week_start(d):
    d = parse_date(d)
    return d - dt.timedelta(days=d.weekday())


def iso_week_label(d):
    y, w, _ = parse_date(d).isocalendar()
    return f"{y}-W{w:02d}"


def pace_s(speed):
    """m/s -> sec per km"""
    if not speed or speed <= 0:
        return None
    return 1000.0 / speed


def fmt_pace(sec_per_km):
    if sec_per_km is None or sec_per_km <= 0 or sec_per_km > 1800:
        return "-"
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}"


def fmt_speed_pace(speed):
    return fmt_pace(pace_s(speed))


def fmt_dur(sec):
    if sec is None:
        return "-"
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_hm(sec):
    if sec is None:
        return "-"
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    return f"{h}h{r // 60:02d}m"


def fnum(v, nd=1, suffix=""):
    if v is None:
        return "-"
    if isinstance(v, float) and math.isnan(v):
        return "-"
    return f"{v:.{nd}f}{suffix}" if nd else f"{int(round(v))}{suffix}"


def signed(v, nd=1, suffix=""):
    if v is None:
        return "-"
    return f"{v:+.{nd}f}{suffix}"


def signed_pace(delta_s):
    """delta in sec/km; negative = faster"""
    if delta_s is None:
        return "-"
    sign = "-" if delta_s < 0 else "+"
    return f"{sign}{abs(delta_s):.0f}s/km"


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def stdev(xs):
    xs = [x for x in xs if x is not None]
    return statistics.stdev(xs) if len(xs) >= 3 else None


def spark(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    out = []
    for v in values:
        if v is None:
            out.append(" ")
        elif hi == lo:
            out.append(SPARK[3])
        else:
            out.append(SPARK[int((v - lo) / (hi - lo) * (len(SPARK) - 1))])
    return "".join(out)


def md_table(headers, rows):
    lines = ["| " + " | ".join(str(h) for h in headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        lines.append("| " + " | ".join("-" if c is None else str(c) for c in r) + " |")
    return "\n".join(lines)


def linreg(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys) or 1e-9
    return a, b, 1 - ss_res / ss_tot
