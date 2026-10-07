"""Review checklists on GitHub, and syncing the reviewer's decisions into the history database.

Each run's findings are posted as GitHub issues labelled `sanctions-review`, one checkbox per finding.
- Tick a box  -> the finding is VERIFIED and enters that party's activity history.
- Close the issue -> every box still unticked is REJECTED (kept for audit, hidden from history).
- Untick a box later -> the finding goes back to pending (or rejected, if the issue is closed).

`python -m monitor.review` re-reads every review issue and applies the decisions. It is idempotent, so
it can run on every issue edit and again at the start of each weekly run.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

import requests

from .history import KIND_LABEL, History

log = logging.getLogger(__name__)
LABEL = "sanctions-review"
PER_ISSUE = 80                     # keeps each issue well inside GitHub's 65,536-character body limit
_ITEM = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s.*?<!--\s*id:([0-9a-f]{12})\s*-->", re.M)


def _api():
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return None, None
    return repo, {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def parse(body: str) -> dict[str, bool]:
    """{finding_id: ticked} for every checklist line in an issue body."""
    return {m.group(2): m.group(1).lower() == "x" for m in _ITEM.finditer(body or "")}


def render_items(rows: list[dict], records: dict[str, dict]) -> str:
    out, current = [], None
    for f in sorted(rows, key=lambda f: (records.get(f["ref"], {}).get("kind") != "individual",
                                         f["ref"], f["kind"] != "un_list", -(f["score"] or 0))):
        if f["ref"] != current:
            current = f["ref"]
            r = records.get(current, {})
            out += ["", f"### {r.get('name', current)} ({current}, {r.get('regime', '')})"]
        title = (f["title"] or "")[:180].replace("[", "(").replace("]", ")")
        link = f"[{title}]({f['url']})" if f["url"] else title
        what = f" — {f['summary'][:220]}" if f["summary"] and f["kind"] != "news" else ""
        ctx = f" · _{f['context'][:80]}_" if f["context"] else ""
        out.append(f"- [ ] **{KIND_LABEL.get(f['kind'], f['kind'])}** {f['date']}: {link} "
                   f"({f['source']}){what}{ctx} <!-- id:{f['id']} -->")
    return "\n".join(out)


def create_issues(hist: History, ids: list[str], records: dict[str, dict], run_date: str, headline: str,
                  report_path: str, mode: str = "weekly") -> list[str]:
    """Post the findings as review checklists. Returns issue URLs."""
    repo, headers = _api()
    if not repo or not ids:
        return []
    rows = {r["id"]: r for r in hist.rows("status='pending'")}
    items = [rows[i] for i in ids if i in rows]
    # Keep a party's findings together when splitting into issues.
    items.sort(key=lambda f: (records.get(f["ref"], {}).get("kind") != "individual", f["ref"]))
    chunks, cur = [], []
    for f in items:
        if len(cur) >= PER_ISSUE and cur[-1]["ref"] != f["ref"]:
            chunks.append(cur)
            cur = []
        cur.append(f)
    if cur:
        chunks.append(cur)
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    urls = []
    for n, chunk in enumerate(chunks, 1):
        what = "Baseline status review" if mode == "baseline" else "Weekly review"
        title = f"{what} {run_date} ({n}/{len(chunks)}): {len(chunk)} findings on " \
                f"{len({f['ref'] for f in chunk})} parties"
        body = "\n".join([
            f"**{headline}**" if n == 1 else f"Part {n} of {len(chunks)}.",
            "",
            "Tick each finding you have checked and accept: it is added to that party's verified "
            "activity history. **Close this issue when done** — anything left unticked is then "
            "recorded as rejected. Full report: "
            f"[{report_path}]({server}/{repo}/blob/{branch}/{report_path}) · "
            f"profiles: [data/profiles]({server}/{repo}/tree/{branch}/data/profiles)",
            render_items(chunk, records),
        ])
        r = requests.post(f"https://api.github.com/repos/{repo}/issues", headers=headers, timeout=60,
                          json={"title": title[:250], "body": body[:65000], "labels": [LABEL]})
        if r.status_code == 422:
            r = requests.post(f"https://api.github.com/repos/{repo}/issues", headers=headers, timeout=60,
                              json={"title": title[:250], "body": body[:65000]})
        r.raise_for_status()
        issue = r.json()
        hist.set_issue([f["id"] for f in chunk], issue["number"])
        urls.append(issue["html_url"])
    hist.commit()
    return urls


def sync(hist: History) -> dict[str, int]:
    """Apply the decisions in every review issue (open and closed)."""
    repo, headers = _api()
    counts = {"verified": 0, "rejected": 0, "pending": 0}
    if not repo:
        return counts
    page = 1
    while True:
        r = requests.get(f"https://api.github.com/repos/{repo}/issues", headers=headers, timeout=60,
                         params={"labels": LABEL, "state": "all", "per_page": 100, "page": page})
        r.raise_for_status()
        issues = r.json()
        if not issues:
            break
        for iss in issues:
            closed = iss.get("state") == "closed"
            for fid, ticked in parse(iss.get("body") or "").items():
                status = "verified" if ticked else ("rejected" if closed else "pending")
                if hist.review(fid, status):
                    counts[status] += 1
        page += 1
    hist.commit()
    return counts


def main() -> int:
    """Entry point for the review workflow: sync decisions, rebuild profiles and dashboard data."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(os.environ.get("MONITOR_ROOT", "."))
    from . import unlist
    from .state import State

    hist = History(root / "state/history.db")
    counts = sync(hist)
    log.info("Review sync: %s", counts)
    records = unlist.load_snapshot(root / "state/un_list.json")
    st = State(root / "state/monitor.db")
    hist.export(root, records, st.get_xref(), {"last_run": st.get_meta("last_run", "")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
