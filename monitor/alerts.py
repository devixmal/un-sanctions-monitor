"""Render the weekly report and deliver it — only when there is something to say."""
from __future__ import annotations

import csv
import logging
import os
import re
import smtplib
from dataclasses import dataclass, field
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

log = logging.getLogger(__name__)


@dataclass
class Report:
    run_date: str = field(default_factory=lambda: date.today().isoformat())
    list_added: list = field(default_factory=list)
    list_removed: list = field(default_factory=list)
    list_amended: list = field(default_factory=list)
    xref_added: list = field(default_factory=list)       # (record, [datasets])
    news: dict = field(default_factory=dict)              # ref -> (record, [Hit])
    report_mentions: list = field(default_factory=list)  # (report_id, url, record, [(name, snippet)])
    official_mentions: list = field(default_factory=list)  # (source, title, url, record, [(name, snippet)])
    feed_mentions: list = field(default_factory=list)    # (record, Hit)
    health: dict = field(default_factory=dict)            # source -> problem
    near_misses: dict = field(default_factory=dict)       # ref -> (record, [Hit]) below threshold
    coverage: dict = field(default_factory=dict)          # run-wide search statistics
    notes: list = field(default_factory=list)             # limits hit during the run
    near_triggers: bool = True                            # do near-misses alone justify an alert?

    @property
    def near_count(self) -> int:
        return sum(len(h) for _, h in self.near_misses.values())

    @property
    def count(self) -> int:
        return (len(self.list_added) + len(self.list_removed) + len(self.list_amended)
                + len(self.xref_added) + sum(len(h) for _, h in self.news.values())
                + len(self.report_mentions) + len(self.official_mentions) + len(self.feed_mentions)
                + len(self.health)
                + (self.near_count if self.near_triggers else 0))

    def headline(self) -> str:
        parts = []
        if self.list_added or self.list_removed or self.list_amended:
            parts.append(f"UN list: +{len(self.list_added)} / -{len(self.list_removed)} / ~{len(self.list_amended)}")
        if self.news:
            parts.append(f"{sum(len(h) for _, h in self.news.values())} news items on {len(self.news)} parties")
        if self.official_mentions:
            parts.append(f"{len(self.official_mentions)} mentions in UN/US/UK official releases")
        if self.report_mentions:
            parts.append(f"{len(self.report_mentions)} mentions in new UN reports")
        if self.feed_mentions:
            parts.append(f"{len(self.feed_mentions)} official-feed mentions")
        if self.xref_added:
            parts.append(f"{len(self.xref_added)} newly listed by other authorities")
        if self.near_misses:
            parts.append(f"{self.near_count} borderline items to review")
        if self.health:
            parts.append(f"⚠ {len(self.health)} source problem(s)")
        return "; ".join(parts)


def nice_date(d: str) -> str:
    """'20261002180000' or 'Tue, 06 Oct 2026 01:15:00 GMT' -> '2026-10-02' / '06 Oct 2026'."""
    if re.fullmatch(r"\d{14}", d or ""):
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    m = re.search(r"\d{1,2} \w{3} \d{4}", d or "")
    return m.group(0) if m else (d or "")


def _who(rec: dict) -> str:
    return f"**{rec['name']}** ({rec['ref']}, {rec.get('regime') or rec['kind']})"


def render_markdown(rep: Report) -> str:
    out = [f"# UN sanctions monitor — week ending {rep.run_date}", "", rep.headline(), ""]
    if rep.health:
        out += ["## ⚠ Monitor health", "Some sources failed this week, so coverage was incomplete:", ""]
        out += [f"- **{s}**: {p}" for s, p in rep.health.items()] + [""]
    if rep.list_added or rep.list_removed or rep.list_amended:
        out += ["## Changes to the UN Consolidated List", ""]
        for r in rep.list_added:
            out.append(f"- 🆕 **Added** {_who(r)} — listed {r.get('listed_on')}; "
                       f"aliases: {', '.join(r.get('aliases', [])[:5]) or '—'}")
        for r in rep.list_removed:
            out.append(f"- ❌ **Removed / delisted** {_who(r)}")
        for a in rep.list_amended:
            out.append(f"- ✏️ **Amended** {_who(a['record'])} — fields: {', '.join(a['changed_fields'])}")
        out.append("")
    if rep.xref_added:
        out += ["## Newly listed by other authorities", ""]
        out += [f"- {_who(r)} now also on: {', '.join(ds)}" for r, ds in rep.xref_added] + [""]
    if rep.official_mentions:
        out += ["## Named in official releases (UN Security Council, US Treasury/OFAC/State/Justice/FBI, UK)", ""]
        for src, title, url, r, snippets in rep.official_mentions:
            out.append(f"- {_who(r)} — {src}: [{title[:160]}]({url})")
            for name, snip in snippets[:1]:
                out.append(f"  - _“…{snip.strip()[:400]}…”_")
        out.append("")
    if rep.report_mentions:
        out += ["## Named in new UN reports", ""]
        for rid, url, r, snippets in rep.report_mentions:
            out.append(f"- {_who(r)} in [{rid}]({url})")
            for name, snip in snippets[:2]:
                out.append(f"  - _“…{snip.strip()[:400]}…”_")
        out.append("")
    if rep.feed_mentions:
        out += ["## Mentioned in official feeds", ""]
        out += [f"- {_who(r)} — [{h.title}]({h.url}) ({h.domain}, {h.date})" for r, h in rep.feed_mentions] + [""]
    if rep.news:
        out += ["## News & web activity", ""]
        for ref, (r, hits) in sorted(rep.news.items(), key=lambda kv: (kv[1][0]["kind"] != "individual",
                                                                       -max(h.score for h in kv[1][1]))):
            out.append(f"### {r['name']} ({ref}, {r.get('regime') or r['kind']})")
            ranked = sorted(hits, key=lambda h: -h.score)
            top = TOP_PER_PARTY if r["kind"] == "individual" else TOP_PER_ORG
            for h in ranked[:top]:
                tag = f"[{h.category}] " if h.category else ""
                out.append(f"- {tag}[{h.title or h.url}]({h.url}) — {h.domain or h.source}, "
                           f"{nice_date(h.date)} (confidence {h.score:.2f})")
                if h.summary:
                    out.append(f"  - {h.summary}")
                if h.evidence:
                    out.append(f"  - Context: {', '.join(h.evidence)}")
            if len(ranked) > top:
                out.append(f"- …and {len(ranked) - top} more in `data/alerts/{rep.run_date}.csv`")
            out.append("")
    if rep.near_misses:
        out += ["## Borderline — below the confidence threshold, review if relevant", "",
                "_These matched a name, nickname or document number but the filter was not confident "
                "they concern the listed party. Listed so nothing is discarded silently._", ""]
        for ref, (r, hits) in sorted(rep.near_misses.items(), key=lambda kv: -max(h.score for h in kv[1][1])):
            ranked = sorted(hits, key=lambda h: -h.score)
            if len(ranked) > 5:
                out.append(f"- {r['name']} ({ref}): {len(ranked) - 5} more borderline items in "
                           f"`data/alerts/{rep.run_date}.csv`")
            for h in ranked[:5]:
                out.append(f"- {r['name']} ({ref}) — [{h.title or h.url}]({h.url}) "
                           f"({h.domain or h.source}, {h.score:.2f}){' — ' + h.summary if h.summary else ''}")
        out.append("")
    if rep.coverage:
        c = rep.coverage
        out += ["## Coverage this run", "",
                f"- GDELT bulk feed (all GDELT-monitored news, every party): "
                f"{'complete' if c.get('gdelt_ok') else 'NOT complete'}",
                f"- Google News per-party searches completed: {c['searched']} of {c['parties']} parties"
                + (" (the rest continue first next run)" if c['searched'] < c['parties'] else ""),
                f"- GDELT worldwide news articles scanned: {c.get('gdelt_articles', 0):,}",
                f"- Searches run: {c['queries']} ({c['failed_queries']} failed); raw results: {c['raw_results']}; "
                f"new items reviewed: {c['new_items']}",
                f"- Discarded automatically: {c.get('dropped_irrelevant', 0)} articles with no security or "
                f"sanctions context (terror, designation, jihad, militia, arrest…); "
                f"{c.get('dropped_not_prominent', 0)} passing mentions of heavily covered parties; "
                f"{c.get('dropped_no_context', 0)} nickname/acronym matches with no link to the party; "
                f"{c.get('dropped_republished', 0)} old articles re-published",
                "- Per-party detail: `data/coverage.csv`"]
        if c.get("deferred") and len(c["deferred"]) <= 20:
            out.append(f"- Google News carried over to next run: {', '.join(c['deferred'])}")
        out.append("")
    if rep.notes:
        out += ["## Limits reached", ""] + [f"- {n}" for n in rep.notes] + [""]
    out.append("_Automated OSINT screening. Verify before acting; name matches can be namesakes._")
    return "\n".join(out)


# ------------------------------------------------------------------ delivery
TOP_PER_PARTY = 10   # individuals
TOP_PER_ORG = 5      # organisations draw far more routine coverage


def write_report(rep: Report, md: str, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{rep.run_date}.md"
    path.write_text(md, encoding="utf-8")
    # Every alerted and borderline item, for filtering/sorting in a spreadsheet.
    data = folder.parent / "data" / "alerts" / f"{rep.run_date}.csv"
    data.parent.mkdir(parents=True, exist_ok=True)
    with data.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["status", "ref", "name", "regime", "confidence", "category", "date", "source",
                    "title", "url", "summary", "matched", "context"])
        for status, group in (("alert", rep.news), ("borderline", rep.near_misses)):
            for ref, (r, hits) in group.items():
                for h in sorted(hits, key=lambda h: -h.score):
                    w.writerow([status, ref, r["name"], r.get("regime", ""), f"{h.score:.2f}", h.category,
                                nice_date(h.date), h.domain or h.source, h.title, h.url, h.summary,
                                "; ".join(h.matched), "; ".join(h.evidence)])
        for src, title, url, r, snippets in rep.official_mentions:
            w.writerow(["official", r["ref"], r["name"], r.get("regime", ""), "1.00", "official", "", src,
                        title, url, (snippets[0][1][:300] if snippets else ""), "; ".join(n for n, _ in snippets), ""])
        for rid, url, r, snippets in rep.report_mentions:
            w.writerow(["un_report", r["ref"], r["name"], r.get("regime", ""), "1.00", "un_report", "", "UN",
                        rid, url, (snippets[0][1][:300] if snippets else ""), "; ".join(n for n, _ in snippets), ""])
        for r, h in rep.feed_mentions:
            w.writerow(["feed", r["ref"], r["name"], r.get("regime", ""), f"{h.score:.2f}", h.category,
                        nice_date(h.date), h.domain or h.source, h.title, h.url, h.summary, "; ".join(h.matched),
                        "; ".join(h.evidence)])
    return path


def github_issue(rep: Report, md: str, report_path: Path) -> str | None:
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return None
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    full = f"{server}/{repo}/blob/{ref}/{report_path.as_posix()}"
    body = md if len(md) < 60000 else md[:58000] + f"\n\n…truncated — full report: {full}"
    payload = {"title": f"Sanctions monitor {rep.run_date}: {rep.headline()}"[:250], "body": body,
               "labels": ["sanctions-alert"]}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    api = f"https://api.github.com/repos/{repo}/issues"
    r = requests.post(api, json=payload, headers=headers, timeout=60)
    if r.status_code == 422:  # label problems etc. — retry plain
        payload.pop("labels")
        r = requests.post(api, json=payload, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json().get("html_url")


def email(rep: Report, md: str) -> bool:
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("ALERT_EMAIL_TO")
    if not (host and to):
        return False
    import markdown as mdlib

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Sanctions monitor] {rep.headline()}"[:200]
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", "monitor@localhost")
    msg["To"] = to
    msg.attach(MIMEText(md, "plain", "utf-8"))
    msg.attach(MIMEText(mdlib.markdown(md), "html", "utf-8"))
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
        s.sendmail(msg["From"], [a.strip() for a in to.split(",")], msg.as_string())
    return True


def _short(rep: Report, link: str | None) -> str:
    lines = [f"UN sanctions monitor — {rep.run_date}", rep.headline()]
    for r in rep.list_added[:5]:
        lines.append(f"• Added: {r['name']} ({r['ref']})")
    for ref, (r, hits) in list(rep.news.items())[:8]:
        lines.append(f"• {r['name']}: {hits[0].summary or hits[0].title}"[:280])
    if link:
        lines.append(f"Full report: {link}")
    return "\n".join(lines)


def slack(rep: Report, link: str | None) -> bool:
    hook = os.environ.get("SLACK_WEBHOOK_URL")
    if not hook:
        return False
    requests.post(hook, json={"text": _short(rep, link)}, timeout=30).raise_for_status()
    return True


def telegram(rep: Report, link: str | None) -> bool:
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return False
    requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                  json={"chat_id": chat, "text": _short(rep, link)[:4000],
                        "disable_web_page_preview": True}, timeout=30).raise_for_status()
    return True


def dispatch(rep: Report, reports_dir: Path) -> list[str]:
    """Send everywhere configured. Returns channels that succeeded."""
    md = render_markdown(rep)
    path = write_report(rep, md, reports_dir)
    sent, link = ["report file"], None
    for name, fn in (("GitHub issue", lambda: github_issue(rep, md, path)),):
        try:
            link = fn()
            if link:
                sent.append(name)
        except Exception as e:  # noqa: BLE001
            log.error("%s delivery failed: %s", name, e)
    for name, fn in (("email", lambda: email(rep, md)), ("Slack", lambda: slack(rep, link)),
                     ("Telegram", lambda: telegram(rep, link))):
        try:
            if fn():
                sent.append(name)
        except Exception as e:  # noqa: BLE001
            log.error("%s delivery failed: %s", name, e)
    return sent
