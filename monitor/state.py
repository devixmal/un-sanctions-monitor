"""Persistent state (SQLite, committed back to the repo by the workflow)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ocid|cmpid)")


def canonical_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
        q = [(k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k.lower())]
        host = p.netloc.lower().removeprefix("www.")
        return urlunsplit((p.scheme.lower(), host, p.path.rstrip("/"), urlencode(q), ""))
    except ValueError:
        return url.strip()


def key_of(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (
                k TEXT PRIMARY KEY, source TEXT, ref TEXT, url TEXT, first_seen TEXT);
            CREATE TABLE IF NOT EXISTS xref (ref TEXT PRIMARY KEY, datasets TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            CREATE INDEX IF NOT EXISTS seen_source ON seen(source);
            """
        )
        self._pending: list[tuple] = []
        self._pending_keys: set[str] = set()

    # ---- seen items -------------------------------------------------------
    def is_seen(self, k: str) -> bool:
        if k in self._pending_keys:
            return True
        return self.db.execute("SELECT 1 FROM seen WHERE k=?", (k,)).fetchone() is not None

    def source_initialised(self, source: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE source=? LIMIT 1", (source,)).fetchone() is not None

    def mark(self, k: str, source: str, ref: str = "", url: str = "") -> None:
        """Queued; only written by commit(), i.e. after alerts were delivered."""
        self._pending.append((k, source, ref, url, datetime.now(timezone.utc).isoformat()))
        self._pending_keys.add(k)

    # ---- cross-list references ---------------------------------------------
    def get_xref(self) -> dict[str, list[str]]:
        return {r: json.loads(d) for r, d in self.db.execute("SELECT ref, datasets FROM xref")}

    def set_xref(self, data: dict[str, list[str]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.executemany(
            "INSERT OR REPLACE INTO xref VALUES (?,?,?)",
            [(r, json.dumps(sorted(d)), now) for r, d in data.items()],
        )

    # ---- meta ----------------------------------------------------------------
    def get_meta(self, k: str, default=None):
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, json.dumps(v)))

    def commit(self, prune_days: int = 400) -> None:
        self.db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?)", self._pending)
        self._pending.clear()
        self._pending_keys.clear()
        # Keep the DB small: news older than ~a year will never resurface in a 7-day window.
        self.db.execute(
            "DELETE FROM seen WHERE source IN ('gdelt','google_news','feed') "
            "AND first_seen < datetime('now', ?)", (f"-{prune_days} days",)
        )
        self.db.commit()
