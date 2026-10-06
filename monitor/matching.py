"""Name normalisation, search-term selection and in-text matching."""
from __future__ import annotations

import re
import unicodedata

# Prefixes that appear in UN aliases but aren't part of how media write names.
_TITLES = {
    "colonel", "col", "general", "gen", "major", "brigadier", "lieutenant", "lt", "commander",
    "captain", "capt", "dr", "doctor", "sheikh", "shaykh", "mullah", "maulvi", "mawlawi", "haji",
    "hajji", "mr", "mrs", "professor", "prof", "emir", "amir", "qari", "commandant",
}

# Generic words that make a short entity alias useless as a search term.
_GENERIC = {"group", "company", "limited", "ltd", "corporation", "trading", "general", "bank",
            "foundation", "organization", "organisation", "front", "army", "brigade", "movement"}


def fold(s: str) -> str:
    """Lower-case, strip accents and punctuation, collapse whitespace."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s.lower(), flags=re.UNICODE)
    return " ".join(s.split())


def display_name(n: str) -> str:
    """'MAKENGA, Colonel SULTANI' -> 'Colonel SULTANI MAKENGA'; strips quote marks."""
    n = n.strip().strip("\"'“”‘’ ")
    if n.count(",") == 1:
        last, first = (p.strip() for p in n.split(","))
        if first and last:
            n = f"{first} {last}"
    return " ".join(n.split())


def _strip_titles(n: str) -> str:
    words = n.split()
    while words and fold(words[0]) in _TITLES:
        words = words[1:]
    return " ".join(words)


def is_searchable(n: str, kind: str, min_len: int) -> bool:
    f = fold(n)
    toks = f.split()
    if not toks:
        return False
    if kind == "entity":
        meaningful = [t for t in toks if t not in _GENERIC]
        return len(f) >= min_len and bool(meaningful)
    # Individuals: two+ tokens, or a single unusually long token (e.g. a unique surname-alias).
    return (len(toks) >= 2 and len(f) >= min_len) or (len(toks) == 1 and len(f) >= 9)


def search_names(rec: dict, min_len: int = 8, limit: int = 4, include_low: bool = False) -> list[str]:
    """Primary name + good-quality aliases, de-duplicated, best first."""
    raw = [rec["name"], *rec.get("aliases", [])]
    if include_low:
        raw += rec.get("low_aliases", [])
    out, seen = [], set()
    for n in raw:
        n = _strip_titles(display_name(n))
        key = fold(n)
        if key and key not in seen and is_searchable(n, rec["kind"], min_len):
            seen.add(key)
            out.append(n)
    return out[:limit]


def all_match_names(rec: dict, min_len: int = 8) -> list[str]:
    """Every name variant usable for matching text (wider than the query list)."""
    names = search_names(rec, min_len=min_len, limit=50)
    if rec.get("original_script"):
        names.append(rec["original_script"])
    return names


def _pattern(names: list[str]) -> re.Pattern | None:
    folded = sorted({fold(n) for n in names if fold(n)}, key=len, reverse=True)
    if not folded:
        return None
    alts = "|".join(r"\s+".join(map(re.escape, f.split())) for f in folded)
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)")


def find_mentions(text: str, names: list[str]) -> list[str]:
    """Return the folded name variants that appear as whole words in text."""
    pat = _pattern(names)
    if not pat or not text:
        return []
    return sorted({" ".join(m.group(0).split()) for m in pat.finditer(fold(text))})


def context_snippet(text: str, names: list[str], width: int = 450) -> str:
    """Folded text window around the first mention (good enough for review/LLM)."""
    pat = _pattern(names)
    ft = fold(text)
    m = pat.search(ft) if pat else None
    if not m:
        return ""
    a, b = max(0, m.start() - width), min(len(ft), m.end() + width)
    return ("…" if a else "") + ft[a:b] + ("…" if b < len(ft) else "")


class NameIndex:
    """Fast lookup of which listed parties are mentioned in a long document."""

    def __init__(self, records: dict[str, dict], min_len: int = 8):
        self.by_name: dict[str, set[str]] = {}
        for ref, rec in records.items():
            for n in all_match_names(rec, min_len):
                f = fold(n)
                if f:
                    self.by_name.setdefault(f, set()).add(ref)
        names = sorted(self.by_name, key=len, reverse=True)
        self._chunks = [names[i:i + 400] for i in range(0, len(names), 400)]
        self._pats = [
            re.compile(r"(?<!\w)(?:" + "|".join(r"\s+".join(map(re.escape, n.split())) for n in chunk) + r")(?!\w)")
            for chunk in self._chunks
        ]

    def scan(self, text: str, width: int = 400) -> dict[str, list[tuple[str, str]]]:
        """{ref: [(matched_name, snippet), ...]} for every listed party found in text."""
        ft = fold(text)
        hits: dict[str, list[tuple[str, str]]] = {}
        for pat in self._pats:
            for m in pat.finditer(ft):
                name = " ".join(m.group(0).split())
                a, b = max(0, m.start() - width), min(len(ft), m.end() + width)
                for ref in self.by_name.get(name, ()):
                    if len(hits.setdefault(ref, [])) < 3:
                        hits[ref].append((name, ft[a:b]))
        return hits
