import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
JOURNAL = ROOT / "journal"
PLAN = ROOT / "plan"
CRED_FILE = ROOT / "intervals_icu_api.txt"


def load_config():
    path = ROOT / "athlete.toml"
    if not path.exists():
        raise SystemExit("找不到 athlete.toml。先运行 `python3 -m coach init` 从 athlete.example.toml 生成，再按自己的情况修改。")
    with open(path, "rb") as f:
        return tomllib.load(f)


# ---- 与运动员相关的文案/日期都从 athlete.toml 读取，代码里不写死个人信息

def goal_label(cfg):
    """目标的简称，用在报告和网页里（“距破3”“距目标”）。"""
    return cfg["race"].get("goal_label") or "目标"


def baseline_since(cfg):
    """wellness（静息心率/HRV 等）基线的起点：换设备后，之前的数据口径不同，不参与比较。"""
    d = cfg.get("devices") or {}
    return str(d.get("baseline_since") or d.get("apple_watch_since") or cfg["sync"]["history_start"])


def baseline_label(cfg):
    return (cfg.get("devices") or {}).get("baseline_label") or "当前设备"


def previous_label(cfg):
    return (cfg.get("devices") or {}).get("previous_label") or "旧设备"


def injury_focus(cfg):
    """重点防范的伤病部位，用在提示语里。"""
    return (cfg.get("injury") or {}).get("focus") or "伤病部位"


def other_sports_text(cfg):
    sports = (cfg.get("athlete") or {}).get("other_sports") or []
    return "、".join(sports)


def load_credentials():
    """Returns (athlete_id, token). Never print the token.
    环境变量 INTERVALS_ICU_ID / INTERVALS_ICU_TOKEN 优先，其次是 intervals_icu_api.txt。"""
    env_id, env_tok = os.environ.get("INTERVALS_ICU_ID"), os.environ.get("INTERVALS_ICU_TOKEN")
    if env_id and env_tok:
        return env_id.strip(), env_tok.strip()
    if not CRED_FILE.exists():
        raise SystemExit("找不到 Intervals.icu 凭据：复制 intervals_icu_api.example.txt 为 intervals_icu_api.txt 并填入，"
                         "或设置环境变量 INTERVALS_ICU_ID / INTERVALS_ICU_TOKEN。")
    kv = {}
    for line in CRED_FILE.read_text().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            kv[k.strip().lower()] = v.strip()
    aid = kv.get("id") or kv.get("athlete_id")
    tok = kv.get("token") or kv.get("api_key") or kv.get("key")
    if not aid or not tok:
        raise SystemExit("intervals_icu_api.txt 需要两行：`id: iXXXX` 和 `token: XXXX`")
    return aid, tok
