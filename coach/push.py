"""把周计划推到 Intervals.icu 日历（category=WORKOUT），之后在手机 Intervals 客户端里同步到手表。

数据来源是教练写的结构化文件 `plan/weeks/<YYYY-Www>.workouts.toml`（人读的 `<周>.md` 是自由文本，没法可靠解析成步骤）。
默认只预览（dry-run）；加 --apply 才写线上。幂等：每条用 external_id = coach-<日期> 标识，重复运行是更新，不会重复创建。
只会改/删 external_id 以 `coach-` 开头的事件，用户自己建的计划不动。

文件格式：
    week = "2026-W47"
    [[workout]]
    date = "2026-11-17"
    name = "轻松跑 10 km"
    steps = '''
    - 10km 130-142bpm
    '''
steps 是 Intervals.icu 的 workout builder 语法，只用心率目标（同一个训练里不能混用心率和配速）；
`130-142bpm` / `≤142bpm` 由脚本按 athlete.toml 的 LTHR 换成 `x-y% LTHR`。
"""
import datetime as dt
import re
import tomllib

from .api import ApiError, Intervals
from .config import PLAN, load_config

PREFIX = "coach-"


def week_dates(label):
    y, w = label.split("-W")
    mon = dt.date.fromisocalendar(int(y), int(w), 1)
    return mon, mon + dt.timedelta(days=6)


def resolve_week(arg=None):
    """arg: None=最新的 *.workouts.toml；'2026-W47'；或该周任意日期。"""
    if arg is None:
        files = sorted((PLAN / "weeks").glob("*.workouts.toml"))
        if not files:
            raise SystemExit("plan/weeks/ 下没有 *.workouts.toml：周复盘时先写结构化课表（见 coach/push.py 文件头）。")
        return files[-1].name.split(".")[0]
    if re.fullmatch(r"\d{4}-W\d{2}", arg):
        return arg
    d = dt.date.fromisoformat(arg)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def hr_to_lthr(text, lthr):
    """130-142bpm → 75-82% LTHR；≤142bpm → 60-82% LTHR。"""
    def pct(x):
        return round(float(x) / lthr * 100)

    text = re.sub(r"(\d+)\s*[-–]\s*(\d+)\s*bpm", lambda m: f"{pct(m[1])}-{pct(m[2])}% LTHR", text, flags=re.I)
    text = re.sub(r"(?:≤|<=)\s*(\d+)\s*bpm", lambda m: f"60-{pct(m[1])}% LTHR", text, flags=re.I)
    if re.search(r"\d\s*bpm", text, re.I):
        raise ValueError("bpm 只支持 `a-bpm` 区间或 `≤N bpm`：" + text)
    return text


def load_week(label, cfg):
    path = PLAN / "weeks" / f"{label}.workouts.toml"
    if not path.exists():
        raise SystemExit(f"找不到 {path.relative_to(PLAN.parent)}：周复盘时和 {label}.md 一起写结构化课表。")
    with open(path, "rb") as f:
        doc = tomllib.load(f)
    if doc.get("week") != label:
        raise SystemExit(f"{path.name} 里的 week = {doc.get('week')!r}，应为 {label!r}。")
    mon, sun = week_dates(label)
    lthr = cfg["hr"]["lthr"]
    out, seen = [], set()
    for i, w in enumerate(doc.get("workout", []), 1):
        try:
            d = dt.date.fromisoformat(str(w["date"]))
            name = w["name"].strip()
            steps = w["steps"].strip()
        except (KeyError, ValueError) as e:
            raise SystemExit(f"{path.name} 第 {i} 个 [[workout]] 缺字段或日期格式错：{e}")
        if not mon <= d <= sun:
            raise SystemExit(f"{path.name}：{d} 不在 {label}（{mon} ~ {sun}）内。")
        if d in seen:
            raise SystemExit(f"{path.name}：{d} 重复；一天一条，多段写进同一个 steps。")
        seen.add(d)
        lines = [ln.rstrip() for ln in steps.splitlines() if ln.strip()]
        if not any(ln.lstrip().startswith("-") for ln in lines):
            raise SystemExit(f"{path.name}：{d} 的 steps 里没有以 `-` 开头的步骤行。")
        # Intervals 语法：每个小节（标题行 + 若干 `-` 步骤，如 `Threshold 3x`）之间必须空一行，否则 `3x` 不会当作重复
        blocks = []
        for ln in lines:
            if not ln.lstrip().startswith("-") and blocks:
                blocks.append("")
            blocks.append(ln)
        try:
            desc = hr_to_lthr("\n".join(blocks), lthr)
        except ValueError as e:
            raise SystemExit(f"{path.name}：{d}：{e}")
        out.append({
            "category": "WORKOUT",
            "type": w.get("type", "Run"),
            "start_date_local": f"{d}T00:00:00",
            "name": name,
            "description": desc,
            "external_id": f"{PREFIX}{d}",
        })
    return out, (mon, sun)


def push(arg=None, apply=False, prune=True):
    cfg = load_config()
    label = resolve_week(arg)
    want, (mon, sun) = load_week(label, cfg)
    print(f"{label}（{mon} ~ {sun}）：{len(want)} 条课表｜{'写入 Intervals.icu' if apply else '预览（加 --apply 才会写线上）'}")
    api = Intervals()
    try:
        existing = api.events(mon, sun)
    except ApiError as e:
        raise SystemExit(f"读取线上日历失败：{e}")
    mine = {e["external_id"]: e for e in existing if str(e.get("external_id") or "").startswith(PREFIX)}
    others = {}
    for e in existing:
        if e.get("category") == "WORKOUT" and not str(e.get("external_id") or "").startswith(PREFIX):
            others.setdefault(str(e["start_date_local"])[:10], []).append(e.get("name"))

    todo = []
    for w in want:
        day = w["start_date_local"][:10]
        old = mine.get(w["external_id"])
        if old is None and day in others:
            print(f"  ⚠ {day} 已有非教练创建的计划 {others[day]}，跳过（要覆盖请先在 Intervals 里删掉它）")
            continue
        if old is None:
            todo.append(("新建", w, None))
        elif (old.get("name"), (old.get("description") or "").strip()) != (w["name"], w["description"]):
            todo.append(("更新", w, old["id"]))
        else:
            print(f"  = {day} {w['name']}（线上已是最新）")
    keep = {w["external_id"] for w in want}
    for xid, e in mine.items():
        if xid not in keep and prune:
            todo.append(("删除", {"start_date_local": e["start_date_local"], "name": e.get("name")}, e["id"]))

    for act, w, _ in todo:
        print(f"  {'+' if act == '新建' else '~' if act == '更新' else '-'} {act} {w['start_date_local'][:10]} {w['name']}")
        if act != "删除":
            print("      " + w["description"].replace("\n", "\n      "))
    if not todo:
        print("没有需要写入的变化。")
        return
    if not apply:
        return
    for act, w, eid in todo:
        try:
            if act == "新建":
                r = api.create_event(w)
            elif act == "更新":
                r = api.update_event(eid, w)
            else:
                api.delete_event(eid)
                continue
        except ApiError as e:
            raise SystemExit(f"写入失败（{act} {w['start_date_local'][:10]}）：{e}")
        steps = len((r or {}).get("workout_doc", {}).get("steps") or [])
        extra = f"，解析出 {steps} 个步骤" if r else ""
        if r and not steps:
            extra = "，⚠ 没解析出步骤（手表上会是空课表，检查 steps 语法）"
        print(f"  ✓ {act} {w['start_date_local'][:10]}{extra}")
    print("完成。到手机 Intervals 客户端里同步到 Apple Watch。")
