import sys

if sys.version_info < (3, 11):
    raise SystemExit(f"需要 Python 3.11 或更新版本（当前 {sys.version.split()[0]}）：用到了标准库 tomllib。")
