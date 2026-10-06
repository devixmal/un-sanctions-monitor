"""GDELT Global Knowledge Graph bulk ingestion.

GDELT publishes, every 15 minutes, a file listing every news article it processed worldwide with the
people, organisations and names it found (English, plus a separate stream machine-translated from
65+ languages). Downloading these files has no rate limit, and every listed name and alias is
matched against every article in one pass, so coverage does not depend on per-name search quotas.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from .matching import fold

log = logging.getLogger(__name__)
BASE = "http://data.gdeltproject.org/gdeltv2/"
STREAMS = {"english": "{ts}.gkg.csv.zip", "translated": "{ts}.translation.gkg.csv.zip"}
_TITLE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)

# Worker-process globals (set once per process by _init).
_NAMES: dict[str, list[str]] = {}
_NICKS: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
_MAXLEN = 8


def _init(names, nicks):
    global _NAMES, _NICKS, _MAXLEN
    _NAMES, _NICKS = names, nicks
    _MAXLEN = max((len(k.split()) for k in list(names) + list(nicks)), default=1)


def slots(start: datetime, end: datetime) -> list[str]:
    """15-minute timestamps covering (start, end]."""
    t = start.replace(second=0, microsecond=0)
    t = t - timedelta(minutes=t.minute % 15) + timedelta(minutes=15)
    out = []
    while t <= end:
        out.append(t.strftime("%Y%m%d%H%M%S"))
        t += timedelta(minutes=15)
    return out


def _candidates(field: str) -> set[str]:
    """Folded names from a GKG name field ('Name,offset;Name,offset' or 'name;name')."""
    out = set()
    for part in field.split(";"):
        name = part.rsplit(",", 1)[0] if "," in part and part.rsplit(",", 1)[1].isdigit() else part
        f = fold(name)
        if f:
            out.add(f)
    return out


def _grams(name: str):
    """The name and every contiguous sub-phrase ('General Sultani Makenga' -> 'sultani makenga'…)."""
    t = name.split()
    for i in range(len(t)):
        for j in range(i + 1, min(len(t), i + _MAXLEN) + 1):
            yield " ".join(t[i:j])


def _scan_row(cols: list[str]) -> list[tuple[str, str]]:
    """[(ref, matched_name)] for one GKG record."""
    if len(cols) < 27:
        return []
    found = _candidates(cols[23]) | _candidates(cols[11]) | _candidates(cols[13])
    grams = {g for n in found for g in _grams(n)}
    hits = {}
    for g in grams:
        for ref in _NAMES.get(g, ()):
            hits.setdefault(ref, g)
    if _NICKS:
        ctx_text = None
        for g in grams:
            for ref, ctx in _NICKS.get(g, ()):
                if ref in hits:
                    continue
                if ctx_text is None:
                    ctx_text = " " + fold(" ".join([cols[23], cols[13], cols[9]])) + " "
                if any(f" {c} " in ctx_text for c in ctx):
                    hits[ref] = g
    return list(hits.items())


def process_file(stream: str, ts: str) -> tuple[str, str, int, list[dict]]:
    """Download one 15-minute file and return matches. Runs in a worker process."""
    import requests  # imported here so worker processes don't share sessions

    url = BASE + STREAMS[stream].format(ts=ts)
    try:
        r = requests.get(url, timeout=120)
    except Exception as e:  # noqa: BLE001
        return stream, ts, -1, [{"error": str(e)}]
    if r.status_code == 404:
        return stream, ts, 0, []          # GDELT occasionally skips a slot
    if r.status_code != 200:
        return stream, ts, -1, [{"error": f"HTTP {r.status_code}"}]
    out, rows = [], 0
    try:
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            with z.open(z.namelist()[0]) as f:
                for raw in io.TextIOWrapper(f, encoding="utf-8", errors="replace"):
                    rows += 1
                    cols = raw.rstrip("\n").split("\t")
                    for ref, name in _scan_row(cols):
                        m = _TITLE.search(cols[26]) if len(cols) > 26 else None
                        out.append({"ref": ref, "name": name, "url": cols[4], "domain": cols[3],
                                    "date": cols[1], "title": (m.group(1).strip() if m else ""),
                                    "stream": stream})
    except Exception as e:  # noqa: BLE001
        return stream, ts, -1, [{"error": f"parse: {e}"}]
    return stream, ts, rows, out


def scan(records: dict[str, dict], start: datetime, end: datetime, streams=("english", "translated"),
         workers: int = 4, min_len: int = 8, progress_every: int = 100, retry: list | None = None):
    """Scan every GKG file in (start, end] plus any previously failed files. Returns (hits, stats).

    stats["failed_jobs"] lists files that could not be read; the caller retries them next run.
    """
    from .matching import context_for, context_terms, nickname_terms, search_names

    names: dict[str, list[str]] = {}
    nicks: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for ref, rec in records.items():
        for n in search_names(rec, min_len=min_len, limit=100):
            names.setdefault(fold(n), []).append(ref)
        ctx = context_terms(rec)
        for n in nickname_terms(rec, limit=50):
            c = tuple(fold(x) for x in context_for(n, ctx))
            if c and fold(n) not in names:
                nicks.setdefault(fold(n), []).append((ref, c))

    jobs = [(s, ts) for s in streams for ts in slots(start, end)]
    jobs += [tuple(j) for j in (retry or []) if tuple(j) not in set(jobs)]
    stats = {"files": len(jobs), "ok": 0, "missing": 0, "failed": 0, "rows": 0, "errors": [],
             "failed_jobs": []}
    hits: list[dict] = []
    log.info("GDELT GKG: scanning %d files (%s → %s) for %d names + %d nicknames",
             len(jobs), start.isoformat(), end.isoformat(), len(names), len(nicks))
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(names, nicks)) as ex:
        futs = [ex.submit(process_file, s, ts) for s, ts in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            stream, ts, rows, out = fut.result()
            if rows < 0:
                stats["failed"] += 1
                stats["failed_jobs"].append([stream, ts])
                stats["errors"].append(f"{stream} {ts}: {out[0]['error'] if out else ''}")
            elif rows == 0 and not out:
                stats["missing"] += 1
            else:
                stats["ok"] += 1
                stats["rows"] += rows
                hits.extend(out)
            if i % progress_every == 0:
                log.info("GDELT GKG progress %d/%d files, %d articles, %d matches",
                         i, len(jobs), stats["rows"], len(hits))
    log.info("GDELT GKG done: %d files ok, %d missing, %d failed, %d articles scanned, %d matches",
             stats["ok"], stats["missing"], stats["failed"], stats["rows"], len(hits))
    return hits, stats


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
