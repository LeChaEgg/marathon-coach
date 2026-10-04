import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
JOURNAL = ROOT / "journal"
PLAN = ROOT / "plan"
CRED_FILE = ROOT / "intervals_icu_api.txt"


def load_config():
    with open(ROOT / "athlete.toml", "rb") as f:
        return tomllib.load(f)


def load_credentials():
    """Returns (athlete_id, token). Never print the token."""
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
