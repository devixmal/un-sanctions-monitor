"""OSINT sources: GDELT, Google News, watched feeds, UN reports, OpenSanctions."""
from __future__ import annotations

import csv
import io
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urljoin

import feedparser
from bs4 import BeautifulSoup

from .http import get
from .matching import fold

log = logging.getLogger(__name__)


@dataclass
class Hit:
    source: str
    title: str
    url: str
    date: str = ""
    domain: str = ""
    language: str = ""
    snippet: str = ""
    ref: str = ""                      # listed party this hit is about
    matched: list[str] = field(default_factory=list)
    score: float = 0.0
    summary: str = ""
    category: str = ""


class SourceStats:
    """Counts calls/failures per source so silent breakage becomes an alert."""

    def __init__(self):
        self.calls: dict[str, int] = {}
        self.fails: dict[str, int] = {}
        self.errors: dict[str, str] = {}

    def ok(self, src):
        self.calls[src] = self.calls.get(src, 0) + 1

    def fail(self, src, err):
        self.ok(src)
        self.fails[src] = self.fails.get(src, 0) + 1
        self.errors[src] = str(err)[:300]

    def unhealthy(self, threshold: float = 0.5) -> dict[str, str]:
        return {
            s: f"{self.fails.get(s, 0)}/{n} requests failed (last error: {self.errors.get(s, '')})"
            for s, n in self.calls.items()
            if n and self.fails.get(s, 0) / n >= threshold
        }


STATS = SourceStats()


def _or_query(names: list[str], quote: str = '"') -> str:
    terms = [f"{quote}{n}{quote}" for n in names]
    return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"


# --------------------------------------------------------------------------- GDELT
def gdelt(names: list[str], days: int, max_records: int = 75) -> list[Hit]:
    """GDELT DOC 2.0: global news in 65+ languages (machine-translated), 15-min refresh."""
    ascii_names = [fold(n) for n in names if fold(n).isascii() and len(fold(n)) >= 5]
    if not ascii_names:
        return []
    params = {
        "query": _or_query(ascii_names),
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max_records,
        "timespan": f"{days * 24}h",
        "sort": "DateDesc",
    }
    try:
        r = get("https://api.gdeltproject.org/api/v2/doc/doc", params=params, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        if not r.text.strip().startswith("{"):
            # GDELT returns plain-text errors (e.g. query too short); not a source outage.
            log.debug("GDELT message for %s: %s", names, r.text[:200])
            STATS.ok("gdelt")
            return []
        data = r.json()
        STATS.ok("gdelt")
    except Exception as e:  # noqa: BLE001
        STATS.fail("gdelt", e)
        log.warning("GDELT failed for %s: %s", names, e)
        return []
    return [
        Hit("GDELT", a.get("title", ""), a.get("url", ""), a.get("seendate", ""),
            a.get("domain", ""), a.get("language", ""))
        for a in data.get("articles", [])
        if a.get("url")
    ]


# --------------------------------------------------------------------- Google News
def google_news(names: list[str], days: int, editions: list[dict]) -> list[Hit]:
    hits = []
    q = _or_query(names) + f" when:{days}d"
    for ed in editions:
        url = "https://news.google.com/rss/search?" + urlencode(
            {"q": q, "hl": ed["hl"], "gl": ed["gl"], "ceid": ed["ceid"]})
        try:
            r = get(url, timeout=45)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            feed = feedparser.parse(r.content)
            STATS.ok("google_news")
        except Exception as e:  # noqa: BLE001
            STATS.fail("google_news", e)
            log.warning("Google News failed for %s: %s", names, e)
            continue
        for e in feed.entries:
            src = getattr(e, "source", {}) or {}
            desc = BeautifulSoup(getattr(e, "summary", "") or "", "html.parser").get_text(" ")
            hits.append(Hit("Google News", e.get("title", ""), e.get("link", ""),
                            e.get("published", ""), src.get("title", ""), ed["hl"], desc))
    return hits


# ------------------------------------------------------------------ Watched feeds
def read_feed(url: str) -> list[Hit]:
    """Any RSS/Atom feed; items are later matched against every listed name."""
    try:
        r = get(url, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        feed = feedparser.parse(r.content)
        STATS.ok("feeds")
    except Exception as e:  # noqa: BLE001
        STATS.fail("feeds", e)
        log.warning("Feed %s failed: %s", url, e)
        return []
    title = feed.feed.get("title", url)
    out = []
    for e in feed.entries:
        body = " ".join(filter(None, [
            e.get("summary", ""),
            *[c.get("value", "") for c in e.get("content", [])],
        ]))
        text = BeautifulSoup(body, "html.parser").get_text(" ")
        out.append(Hit("Feed: " + title, e.get("title", ""), e.get("link", ""),
                       e.get("published", e.get("updated", "")), title, "", text))
    return out


# -------------------------------------------------------------------- UN reports
_SYMBOL = re.compile(r"\bS/(?:19|20)\d{2}/\d{1,4}\b")


def list_report_links(page_url: str) -> list[tuple[str, str]]:
    """[(id, url)] for every report on a listing page (PDF links or S/YYYY/NNN symbols)."""
    try:
        r = get(page_url, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        STATS.ok("report_pages")
    except Exception as e:  # noqa: BLE001
        STATS.fail("report_pages", e)
        log.warning("Report page %s failed: %s", page_url, e)
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        label = " ".join(a.get_text(" ").split())
        sym = _SYMBOL.search(label) or _SYMBOL.search(href)
        if sym:
            symbol = sym.group(0)
            doc_url = href if href.lower().endswith(".pdf") else (
                "https://documents.un.org/api/symbol/access?" + urlencode({"s": symbol, "l": "en", "t": "pdf"}))
            found.setdefault(symbol, doc_url)
        elif href.lower().endswith(".pdf"):
            found.setdefault(href, href)
    return list(found.items())


def document_text(url: str, max_pages: int = 800) -> str:
    """Download a PDF (or HTML page) and return its plain text."""
    from pypdf import PdfReader

    r = get(url, timeout=180, allow_redirects=True)
    r.raise_for_status()
    if r.content[:4] == b"%PDF":
        reader = PdfReader(io.BytesIO(r.content))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:max_pages])
    return BeautifulSoup(r.text, "html.parser").get_text(" ")


# --------------------------------------------------------------- Page context
def page_text(url: str) -> str:
    try:
        r = get(url, timeout=25, rate_limit_waits=())
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            t.decompose()
        return " ".join(soup.get_text(" ").split())
    except Exception:  # noqa: BLE001
        return ""


# ----------------------------------------------------------- OpenSanctions xref
def opensanctions_datasets(url: str, records: dict[str, dict]) -> dict[str, list[str]]:
    """For each UN-listed party, the set of other sanctions/watch lists that also carry it.

    Streams the OpenSanctions 'sanctions' collection (targets.simple.csv) and links rows that
    come from the UN dataset to our records by exact (folded) name/alias match.
    """
    from .matching import all_match_names

    by_name: dict[str, str] = {}
    for ref, rec in records.items():
        for n in [rec["name"], *rec.get("aliases", []), *all_match_names(rec)]:
            by_name.setdefault(fold(n), ref)

    out: dict[str, set[str]] = {}
    try:
        r = get(url, timeout=600, stream=True)
        r.raise_for_status()
        lines = (ln.decode("utf-8", "replace") for ln in r.iter_lines())
        reader = csv.DictReader(lines)
        for row in reader:
            datasets = [d for d in (row.get("dataset") or "").split(";") if d]
            if "un_sc_sanctions" not in datasets:
                continue
            names = [row.get("name", "")] + (row.get("aliases") or "").split(";")
            ref = next((by_name[fold(n)] for n in names if fold(n) in by_name), None)
            if ref:
                out.setdefault(ref, set()).update(d for d in datasets if d != "un_sc_sanctions")
        STATS.ok("opensanctions")
    except Exception as e:  # noqa: BLE001
        STATS.fail("opensanctions", e)
        log.warning("OpenSanctions download failed: %s", e)
        return {}
    return {k: sorted(v) for k, v in out.items()}


def polite_sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)
