"""Per-party history database: every finding, its review status, and the exports built from it.

state/history.db (SQLite, committed to the repo)
  findings  one row per finding (news article, official release, UN report mention, list change,
            new listing by another authority). status: pending -> verified | rejected.
            'unreviewed' = stored for search but not put on a review checklist (baseline overflow).
  checks    when each party was last checked and what that check found.

Exports (rebuilt after every run and every review):
  data/history.csv          verified findings, all parties
  data/profiles/<ref>.md    one page per party: listing details, UN status notes, verified timeline
  data/profiles/README.md   index of all parties
  data/dashboard.json       everything the search dashboard needs
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .state import canonical_url, key_of

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY, ref TEXT, run_date TEXT, kind TEXT, date TEXT, title TEXT, url TEXT,
    source TEXT, summary TEXT, context TEXT, score REAL, locations TEXT,
    status TEXT DEFAULT 'pending', reviewed_at TEXT, issue INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS findings_ref ON findings(ref);
CREATE INDEX IF NOT EXISTS findings_status ON findings(status);
CREATE TABLE IF NOT EXISTS checks (
    ref TEXT PRIMARY KEY, last_checked TEXT, window_start TEXT, found INTEGER, mode TEXT);
"""

KIND_LABEL = {
    "news": "News", "official": "Official release", "un_report": "UN report", "un_list": "UN list change",
    "other_list": "Listed by another authority", "feed": "Sanctions news feed", "borderline": "News (borderline)",
}

# Status hints taken from the UN listing's own narrative notes (not from news).
_FLAGS = {
    "deceased": r"\b(died|deceased|death|dead|killed)\b",
    "detained": r"\b(prison|detained|detention|custody|arrested|imprisoned|incarcerat\w*|jail)\b",
    "convicted": r"\b(convicted|sentenced|found guilty)\b",
    "released": r"\b(released|freed)\b",
}


def un_status_flags(rec: dict) -> list[str]:
    text = (rec.get("comments") or "").lower()
    return [k for k, pat in _FLAGS.items() if re.search(pat, text)]


def _date(d: str) -> str:
    """Normalise source dates to YYYY-MM-DD where possible."""
    if not d:
        return ""
    if re.fullmatch(r"\d{14}", d):
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(d.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"\d{4}-\d{2}-\d{2}", d)
    return m.group(0) if m else ""


class History:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    # ------------------------------------------------------------------ write
    def add(self, ref: str, run_date: str, kind: str, title: str, url: str, source: str = "",
            date: str = "", summary: str = "", context: str = "", score: float = 0.0,
            locations: str = "", status: str = "pending", fid: str | None = None) -> str:
        fid = fid or key_of(ref, kind, canonical_url(url) if url else title)[:12]
        self.db.execute(
            "INSERT OR IGNORE INTO findings (id, ref, run_date, kind, date, title, url, source, summary,"
            " context, score, locations, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, ref, run_date, kind, _date(date) or run_date, title[:500], url, source[:200],
             summary[:1000], context[:500], score, locations[:300], status))
        return fid

    def record_check(self, ref: str, when: str, window_start: str, found: int, mode: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO checks VALUES (?,?,?,?,?)",
                        (ref, when, window_start, found, mode))

    def set_issue(self, ids: list[str], issue: int) -> None:
        self.db.executemany("UPDATE findings SET issue=? WHERE id=?", [(issue, i) for i in ids])

    def review(self, fid: str, status: str, when: str | None = None) -> bool:
        """pending/unreviewed -> verified|rejected; a verified item can be rejected later and vice
        versa (the reviewer's latest decision wins)."""
        when = when or datetime.now(timezone.utc).isoformat()
        cur = self.db.execute("UPDATE findings SET status=?, reviewed_at=? WHERE id=? AND status<>?",
                              (status, when, fid, status))
        return cur.rowcount > 0

    def commit(self) -> None:
        self.db.commit()

    # ------------------------------------------------------------------- read
    def rows(self, where: str = "1=1", params=()) -> list[dict]:
        cur = self.db.execute(f"SELECT * FROM findings WHERE {where} ORDER BY date DESC, id", params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def checks(self) -> dict[str, dict]:
        cur = self.db.execute("SELECT * FROM checks")
        cols = [c[0] for c in cur.description]
        return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}

    # ---------------------------------------------------------- from a run
    def add_report(self, rep, run_date: str, mode: str = "weekly", top_individual: int = 0,
                   top_entity: int = 0) -> list[str]:
        """Store everything a run found. Returns the ids to put on review checklists.

        In baseline mode only the strongest `top_*` news items per party go on the checklist; the
        rest are stored as 'unreviewed' (searchable on the dashboard, reviewable later).
        """
        to_review: list[str] = []
        for r in rep.list_added:
            to_review.append(self.add(r["ref"], run_date, "un_list", f"Added to the UN list ({r.get('listed_on')})",
                                      "", "UN Consolidated List", run_date, "New designation", score=1))
        for r in rep.list_removed:
            to_review.append(self.add(r["ref"], run_date, "un_list", "Removed from the UN list", "",
                                      "UN Consolidated List", run_date, "Delisted", score=1))
        for a in rep.list_amended:
            r = a["record"]
            to_review.append(self.add(r["ref"], run_date, "un_list",
                                      f"UN list entry amended: {', '.join(a['changed_fields'])}", "",
                                      "UN Consolidated List", run_date, (r.get("comments") or "")[:600],
                                      fid=key_of(r["ref"], "un_list", run_date, *a["changed_fields"])[:12],
                                      score=1))
        for r, ds in rep.xref_added:
            to_review.append(self.add(r["ref"], run_date, "other_list", f"Also listed by: {', '.join(ds)}",
                                      "", "OpenSanctions", run_date, score=1,
                                      fid=key_of(r["ref"], "other_list", *ds)[:12]))
        for src, title, url, r, snippets in rep.official_mentions:
            to_review.append(self.add(r["ref"], run_date, "official", title, url, src, run_date,
                                      (snippets[0][1][:600] if snippets else ""), score=1))
        for rid, url, r, snippets in rep.report_mentions:
            to_review.append(self.add(r["ref"], run_date, "un_report", f"Named in UN report {rid}", url,
                                      "UN", run_date, (snippets[0][1][:600] if snippets else ""), score=1,
                                      fid=key_of(r["ref"], "un_report", rid)[:12]))
        for r, h in rep.feed_mentions:
            to_review.append(self.add(r["ref"], run_date, "feed", h.title, h.url, h.domain or h.source,
                                      h.date, h.summary, ", ".join(h.evidence), h.score))
        for ref, (r, hits) in rep.news.items():
            limit = (top_individual if r["kind"] == "individual" else top_entity) if mode == "baseline" else 0
            for i, h in enumerate(sorted(hits, key=lambda h: -h.score)):
                status = "unreviewed" if limit and i >= limit else "pending"
                fid = self.add(ref, run_date, "news", h.title, h.url, h.domain or h.source, h.date,
                               h.summary, ", ".join(h.evidence), h.score, getattr(h, "locations", ""),
                               status=status)
                if status == "pending":
                    to_review.append(fid)
        for ref, (r, hits) in rep.near_misses.items():
            for h in hits:
                self.add(ref, run_date, "borderline", h.title, h.url, h.domain or h.source, h.date,
                         h.summary, ", ".join(h.evidence), h.score, getattr(h, "locations", ""),
                         status="unreviewed")
        self.commit()
        # Only ids that are still pending (a re-found, already-reviewed item stays reviewed).
        pending = {r["id"] for r in self.rows("status='pending'")}
        return [i for i in dict.fromkeys(to_review) if i in pending]

    # ------------------------------------------------------------- exports
    def export(self, root: Path, records: dict[str, dict], xref: dict[str, list[str]] | None = None,
               meta: dict | None = None) -> None:
        xref = xref or {}
        checks = self.checks()
        allrows = self.rows()
        by_ref: dict[str, list[dict]] = {}
        for f in allrows:
            by_ref.setdefault(f["ref"], []).append(f)

        # verified history CSV
        data = root / "data"
        data.mkdir(parents=True, exist_ok=True)
        with (data / "history.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["ref", "name", "regime", "date", "type", "title", "source", "url", "summary", "reviewed_at"])
            for f in allrows:
                if f["status"] == "verified" and f["ref"] in records:
                    r = records[f["ref"]]
                    w.writerow([f["ref"], r["name"], r.get("regime", ""), f["date"], KIND_LABEL.get(f["kind"], f["kind"]),
                                f["title"], f["source"], f["url"], f["summary"], f["reviewed_at"]])

        # profiles
        prof = data / "profiles"
        prof.mkdir(parents=True, exist_ok=True)
        index_rows = []
        for ref, r in sorted(records.items()):
            fs = by_ref.get(ref, [])
            verified = [f for f in fs if f["status"] == "verified"]
            pending = [f for f in fs if f["status"] in ("pending", "unreviewed") and f["kind"] != "borderline"]
            chk = checks.get(ref, {})
            flags = un_status_flags(r)
            lines = [f"# {r['name']}", "",
                     f"**UN reference:** {ref} · **Type:** {r['kind']} · **Regime:** {r.get('regime', '')} · "
                     f"**Listed:** {r.get('listed_on', '')}", ""]
            if r.get("aliases"):
                lines.append(f"**Also known as:** {'; '.join(r['aliases'][:15])}")
            if r.get("original_script"):
                lines.append(f"**Original script:** {r['original_script']}")
            for label, key in (("Nationality", "nationality"), ("Countries", "countries"),
                               ("Date(s) of birth", "dobs"), ("Designation", "designation"),
                               ("Documents", "documents")):
                if r.get(key):
                    lines.append(f"**{label}:** {'; '.join(r[key])}")
            if xref.get(ref):
                lines.append(f"**Also sanctioned by:** {', '.join(xref[ref])}")
            lines += ["", "## Status", ""]
            lines.append(f"- Last checked: {chk.get('last_checked', 'not yet')}"
                         + (f" (news since {chk['window_start']})" if chk.get("window_start") else ""))
            lines.append(f"- Verified activity records: {len(verified)}; awaiting review: {len(pending)}")
            if flags:
                lines.append(f"- UN listing notes mention: {', '.join(flags)} (see notes below)")
            if r.get("comments"):
                lines += ["", "**UN listing notes:**", "", f"> {r['comments']}"]
            lines += ["", "## Verified activity history", ""]
            if verified:
                lines += ["| Date | Type | What | Source |", "|---|---|---|---|"]
                for f in sorted(verified, key=lambda f: f["date"], reverse=True):
                    what = (f["summary"] or f["title"]).replace("|", "/").replace("\n", " ")[:200]
                    link = f"[{f['source'] or 'link'}]({f['url']})" if f["url"] else (f["source"] or "")
                    lines.append(f"| {f['date']} | {KIND_LABEL.get(f['kind'], f['kind'])} | {what} | {link} |")
            else:
                lines.append("_No verified activity recorded yet._")
            (prof / f"{ref}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
            last_v = max((f["date"] for f in verified), default="")
            index_rows.append(f"| [{ref}]({ref}.md) | {r['name']} | {r.get('regime', '')} | {len(verified)} | "
                              f"{len(pending)} | {last_v} | {chk.get('last_checked', '')} |")
        (prof / "README.md").write_text(
            "# Party profiles\n\n| Ref | Name | Regime | Verified | Awaiting review | Last verified activity | "
            "Last checked |\n|---|---|---|---|---|---|---|\n" + "\n".join(index_rows) + "\n", encoding="utf-8")

        # dashboard data
        parties = []
        for ref, r in sorted(records.items()):
            fs = by_ref.get(ref, [])
            parties.append({
                "ref": ref, "kind": r["kind"], "name": r["name"], "aliases": r.get("aliases", [])[:20],
                "low": r.get("low_aliases", [])[:10], "script": r.get("original_script", ""),
                "regime": r.get("regime", ""), "listed": r.get("listed_on", ""),
                "nat": r.get("nationality", []), "countries": r.get("countries", []),
                "dobs": r.get("dobs", []), "desig": r.get("designation", [])[:5],
                "notes": (r.get("comments") or "")[:1500], "flags": un_status_flags(r),
                "xref": xref.get(ref, []), "checked": checks.get(ref, {}).get("last_checked", ""),
                "since": checks.get(ref, {}).get("window_start", ""),
            })
        findings = [{
            "id": f["id"], "ref": f["ref"], "kind": f["kind"], "date": f["date"], "title": f["title"],
            "url": f["url"], "source": f["source"], "summary": f["summary"], "context": f["context"],
            "score": round(f["score"] or 0, 2), "loc": f["locations"], "status": f["status"],
            "run": f["run_date"], "issue": f["issue"],
        } for f in allrows if f["ref"] in records]
        payload = {"generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                   **(meta or {}), "parties": parties, "findings": findings}
        (data / "dashboard.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                                             encoding="utf-8")
