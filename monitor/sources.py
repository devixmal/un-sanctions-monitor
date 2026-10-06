"""OSINT sources: GDELT, Google News, watched feeds, UN reports, OpenSanctions."""
from __future__ import annotations

import csv
import io
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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

    def __init__(self, breaker: int = 15):
        self.calls: dict[str, int] = {}
        self.fails: dict[str, int] = {}
        self.errors: dict[str, str] = {}
        self.streak: dict[str, int] = {}
        self.breaker = breaker            # consecutive failures before a source is switched off
        self.tripped: set[str] = set()

    def ok(self, src):
        self.calls[src] = self.calls.get(src, 0) + 1
        self.streak[src] = 0

    def fail(self, src, err):
        self.calls[src] = self.calls.get(src, 0) + 1
        self.fails[src] = self.fails.get(src, 0) + 1
        self.errors[src] = str(err)[:300]
        self.streak[src] = self.streak.get(src, 0) + 1
        if self.breaker and self.streak[src] >= self.breaker and src not in self.tripped:
            self.tripped.add(src)
            log.error("%s switched off for this run after %d consecutive failures (last: %s)",
                      src, self.streak[src], self.errors[src])

    def is_tripped(self, src) -> bool:
        return src in self.tripped

    def unhealthy(self, threshold: float = 0.5) -> dict[str, str]:
        out = {
            s: f"{self.fails.get(s, 0)}/{n} requests failed (last error: {self.errors.get(s, '')})"
            for s, n in self.calls.items()
            if n and self.fails.get(s, 0) / n >= threshold
        }
        for s in self.tripped:
            out[s] = (f"switched off after {self.breaker} consecutive failures (last error: "
                      f"{self.errors.get(s, '')}); affected parties are retried first next run")
        return out


STATS = SourceStats()


class RateLimiter:
    """Minimum interval between calls to one service, shared across threads."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


GDELT_LIMIT = RateLimiter(5.5)    # GDELT allows ~1 request / 5 s
GNEWS_LIMIT = RateLimiter(2.0)


def _q(term: str) -> str:
    return f'"{term}"' if " " in term or not term.isalnum() else term


def _or_query(terms: list[str]) -> str:
    t = [_q(x) for x in terms]
    return t[0] if len(t) == 1 else "(" + " OR ".join(t) + ")"


def build_query(terms: list[str], context: list[str] | None = None, ascii_only: bool = False) -> str:
    if ascii_only:
        terms = [fold(t) if not t.isascii() else t for t in terms]
        terms = [t for t in terms if t.isascii() and len(t) >= 3]
    if not terms:
        return ""
    q = _or_query(terms)
    if context:
        q += " " + _or_query(context)
    return q


# --------------------------------------------------------------------------- GDELT
def gdelt(terms: list[str], start: datetime, end: datetime, context=None,
          max_records: int = 250, depth: int = 0) -> list[Hit] | None:
    """GDELT DOC 2.0: global news in 65+ languages (machine-translated), 15-min refresh.

    If a window returns the maximum number of records, it is split in two and re-queried,
    so busy names are not truncated. Returns None if the source failed.
    """
    if STATS.is_tripped("gdelt"):
        return None
    query = build_query(terms, context, ascii_only=True)
    if not query:
        return []
    params = {
        "query": query, "mode": "ArtList", "format": "json", "maxrecords": max_records,
        "startdatetime": start.strftime("%Y%m%d%H%M%S"), "enddatetime": end.strftime("%Y%m%d%H%M%S"),
        "sort": "DateDesc",
    }
    GDELT_LIMIT.wait()
    try:
        r = get("https://api.gdeltproject.org/api/v2/doc/doc", params=params, timeout=45,
                rate_limit_waits=(20, 40))
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        if not r.text.strip().startswith("{"):
            # Plain-text message, e.g. a term is too short/common: not an outage.
            log.debug("GDELT message for %s: %s", terms, r.text[:200])
            STATS.ok("gdelt")
            return []
        data = r.json()
        STATS.ok("gdelt")
    except Exception as e:  # noqa: BLE001
        STATS.fail("gdelt", e)
        log.warning("GDELT failed for %s: %s", terms, e)
        return None
    arts = data.get("articles", [])
    hits = [Hit("GDELT", a.get("title", ""), a.get("url", ""), a.get("seendate", ""),
                a.get("domain", ""), a.get("language", "")) for a in arts if a.get("url")]
    if len(arts) >= max_records and depth < 4 and (end - start) > timedelta(hours=6):
        mid = start + (end - start) / 2
        a = gdelt(terms, start, mid, context, max_records, depth + 1)
        b = gdelt(terms, mid, end, context, max_records, depth + 1)
        hits = (a or []) + (b or []) or hits
    return hits


# --------------------------------------------------------------------- Google News
def google_news(terms: list[str], start: datetime, end: datetime, edition: dict, context=None,
                depth: int = 0) -> list[Hit] | None:
    """Google News RSS (max ~100 items per query). Full windows are split by date."""
    if STATS.is_tripped("google_news"):
        return None
    query = build_query(terms, context)
    if not query:
        return []
    q = f"{query} after:{start:%Y-%m-%d} before:{(end + timedelta(days=1)):%Y-%m-%d}"
    url = "https://news.google.com/rss/search?" + urlencode(
        {"q": q, "hl": edition["hl"], "gl": edition["gl"], "ceid": edition["ceid"]})
    GNEWS_LIMIT.wait()
    try:
        r = get(url, timeout=30, rate_limit_waits=(20, 40))
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        feed = feedparser.parse(r.content)
        STATS.ok("google_news")
    except Exception as e:  # noqa: BLE001
        STATS.fail("google_news", e)
        log.warning("Google News failed for %s: %s", terms, e)
        return None
    hits = []
    for e in feed.entries:
        src = getattr(e, "source", {}) or {}
        desc = BeautifulSoup(getattr(e, "summary", "") or "", "html.parser").get_text(" ")
        hits.append(Hit("Google News", e.get("title", ""), e.get("link", ""),
                        e.get("published", ""), src.get("title", ""), edition["hl"], desc))
    if len(feed.entries) >= 95 and depth < 3 and (end - start) >= timedelta(days=2):
        mid = start + (end - start) / 2
        a = google_news(terms, start, mid, edition, context, depth + 1)
        b = google_news(terms, mid + timedelta(days=1), end, edition, context, depth + 1)
        hits = hits + (a or []) + (b or [])
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


def ocr_pdf(data: bytes, max_pages: int = 400) -> str:
    """OCR for scanned PDFs (needs tesseract + poppler, installed by the workflow)."""
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
    except ImportError:
        log.warning("OCR libraries missing; scanned PDF text not extracted")
        return ""
    out = []
    try:
        for first in range(1, max_pages + 1, 20):
            pages = convert_from_bytes(data, dpi=200, first_page=first, last_page=first + 19)
            if not pages:
                break
            out += [pytesseract.image_to_string(p) for p in pages]
    except Exception as e:  # noqa: BLE001
        log.warning("OCR stopped: %s", e)
    return "\n".join(out)


def document_text(url: str, max_pages: int = 1500) -> str:
    """Download a PDF (or HTML page) and return its plain text, OCR-ing scanned PDFs."""
    from pypdf import PdfReader

    r = get(url, timeout=300, allow_redirects=True)
    r.raise_for_status()
    if r.content[:4] == b"%PDF":
        reader = PdfReader(io.BytesIO(r.content))
        pages = reader.pages[:max_pages]
        text = "\n".join((p.extract_text() or "") for p in pages)
        if len(text.strip()) < 200 * max(1, len(pages)) * 0.25:   # little text -> scanned image PDF
            log.info("Low text yield from %s; running OCR", url)
            text = (text + "\n" + ocr_pdf(r.content)).strip()
        return text
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


