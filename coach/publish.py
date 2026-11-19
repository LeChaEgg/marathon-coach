"""Publish the static ability dashboard to GitHub Pages (branch gh-pages, single orphan commit, force-pushed).

The page is generated locally (data/ is not in git), so the branch only ever holds index.html + .nojekyll.
Nothing in the working tree or on `main` is touched.
"""
import datetime as dt
import re
import subprocess
import sys

from . import dashboard
from .config import ROOT
from .reports import Ctx

BRANCH = "gh-pages"


def _git(*args, input=None):
    r = subprocess.run(["git", *args], cwd=ROOT, input=input, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"git {' '.join(args)} 失败：{r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def repo_slug():
    """owner/name of origin on github.com, or None."""
    url = _git("remote", "get-url", "origin")
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def pages_url(slug):
    owner, name = slug.split("/")
    return f"https://{owner.lower()}.github.io/{name}/"


def publish(sync_err=None, dry_run=False):
    slug = repo_slug()
    if not slug:
        raise SystemExit("origin 不是 GitHub 仓库，无法发布到 GitHub Pages。")
    html = dashboard.render(Ctx(sync_err), report_base=f"https://github.com/{slug}/blob/main/reports/runs/", published=True)
    if dry_run:
        out = ROOT / "reports" / "dashboard.published.html"
        out.write_text(html)
        print(f"[dry-run] 已写入 {out.relative_to(ROOT)}，未推送")
        return
    idx = _git("hash-object", "-w", "--stdin", input=html)
    nojekyll = _git("hash-object", "-w", "--stdin", input="")
    tree = _git("mktree", input=f"100644 blob {nojekyll}\t.nojekyll\n100644 blob {idx}\tindex.html\n")
    msg = "Update dashboard " + dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    commit = _git("-c", "user.name=coach-publish", "-c", "user.email=coach-publish@users.noreply.github.com",
                  "commit-tree", tree, "-m", msg)
    _git("push", "--force", "origin", f"{commit}:refs/heads/{BRANCH}")
    print(f"[已发布 {pages_url(slug)}]", file=sys.stderr)
    print(pages_url(slug))
