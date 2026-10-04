"""
python3 -m coach today            晨间准备度报告（先增量同步）
python3 -m coach run [ID|日期]     跑后分析，默认最近一次跑步
python3 -m coach week [日期]       周复盘，默认最近一个完整周（周日当天=本周）
python3 -m coach runs [--days 42] [--group easy|long|tempo|interval|race]
python3 -m coach serve [--port 8787] [--open]   训练中心（本地网页，实时读取报告与教练结论，可直接同步/生成报告/提交反馈）
python3 -m coach dash             静态看板 reports/dashboard.html（today/run/week/log 后自动更新）
python3 -m coach log ...          记录主观反馈（RPE、疼痛等）
python3 -m coach sync [--full] [--rebuild]
所有报告命令加 --offline 可跳过同步，只用本地缓存。
"""
import argparse
import datetime as dt
import sys

from . import dashboard, reports, store


def _sync(args):
    if getattr(args, "offline", False):
        return None
    _, err = store.safe_sync(quiet=False)
    if err:
        print(f"⚠️ 同步失败，改用本地缓存：{err}", file=sys.stderr)
    return err


def main():
    p = argparse.ArgumentParser(prog="python3 -m coach", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync")
    s.add_argument("--full", action="store_true")
    s.add_argument("--no-streams", action="store_true")
    s.add_argument("--rebuild", action="store_true", help="重算所有跑步指标")

    for name in ("today", "run", "week", "runs", "dash"):
        sp = sub.add_parser(name)
        sp.add_argument("--offline", action="store_true")
        if name in ("today", "week"):
            sp.add_argument("date", nargs="?")
        if name == "run":
            sp.add_argument("ref", nargs="?", default="latest", help="活动ID、日期YYYY-MM-DD 或 latest")
        if name == "runs":
            sp.add_argument("--days", type=int, default=42)
            sp.add_argument("--group")

    sv = sub.add_parser("serve", help="本地训练中心网页")
    sv.add_argument("--port", type=int, default=8787)
    sv.add_argument("--host", default="127.0.0.1", help="0.0.0.0 可让同一局域网的手机访问")
    sv.add_argument("--open", action="store_true", help="启动后打开浏览器")

    lg = sub.add_parser("log", help="记录主观反馈")
    lg.add_argument("--date", default=str(dt.date.today()))
    lg.add_argument("--activity", help="活动ID；跑后反馈必填，不填则为当天整体反馈")
    lg.add_argument("--rpe", type=float)
    lg.add_argument("--pain", type=float, help="0–10 最大疼痛")
    lg.add_argument("--pain-loc")
    lg.add_argument("--pain-when", help="跑中/跑后/次日/晨起")
    lg.add_argument("--gait", help="正常/改变")
    lg.add_argument("--fatigue")
    lg.add_argument("--fuel")
    lg.add_argument("--type", help="修正跑步类型：easy recovery long long_quality tempo threshold interval race short")
    lg.add_argument("--red-flag", action="store_true", help="骨性压痛/跳跃痛/跛行/持续到次日/全身病症")
    lg.add_argument("--note")

    args = p.parse_args()
    if args.cmd == "serve":
        from . import server
        server.serve(args.host, args.port, args.open)
        return
    if args.cmd == "sync":
        store.sync(full=args.full, fetch_streams=not args.no_streams)
        if args.rebuild:
            print(f"重算 {store.rebuild_metrics(force=True)} 个", file=sys.stderr)
        return
    if args.cmd == "log":
        entry = {k: v for k, v in {
            "date": args.date, "activity": args.activity, "rpe": args.rpe, "pain": args.pain,
            "pain_loc": args.pain_loc, "pain_when": args.pain_when, "gait": args.gait, "fatigue": args.fatigue,
            "fuel": args.fuel, "type": args.type, "red_flag": args.red_flag or None, "note": args.note,
            "logged_at": dt.datetime.now().isoformat(timespec="seconds"),
        }.items() if v is not None}
        store.add_feedback(entry)
        if args.type:
            store.rebuild_metrics()
        print("已记录：", entry)
        _refresh()
        return

    err = _sync(args)
    if args.cmd == "dash":
        for p in dashboard.write(err):
            print(f"[已生成 {p}]")
        return
    if args.cmd == "today":
        text, path = reports.morning(args.date, err)
    elif args.cmd == "run":
        text, path = reports.run_report(args.ref, err)
    elif args.cmd == "week":
        text, path = reports.weekly(args.date, err)
    else:
        text, path = reports.list_runs(args.days, args.group, err)
    print(text)
    if path:
        print(f"\n[已保存 {path.relative_to(path.parents[2])}]")
    if args.cmd in ("today", "run", "week"):
        _refresh(err)


def _refresh(err=None):
    try:
        print("[看板已更新 " + "、".join(p.name for p in dashboard.write(err)) + "]")
    except Exception as e:  # dashboard must never block a report
        print(f"⚠️ 看板生成失败：{e}", file=sys.stderr)


if __name__ == "__main__":
    main()
