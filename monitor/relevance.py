"""Context gate: a name match only counts if the article is about security / sanctions topics.

Two independent signals, either is enough:
- GDELT's own topic codes for the article (TERROR, ARMEDCONFLICT, ARREST…), assigned in every
  language GDELT reads, so a Pashto or Arabic article is judged the same way as an English one;
- a multilingual keyword list (terror, designated, jihad, sanctions, militia, arrested…) matched in
  the headline, snippet and article text.

For parties that draw heavy coverage, the party must also be prominent: named in the headline or
near the top of the article, not in passing in paragraph twenty.
"""
from __future__ import annotations

import re

from .matching import fold


def _is_spaced_script(word: str) -> bool:
    """Latin/Greek/Cyrillic words get word-boundary matching. Arabic (attached prefixes such as
    al-/wa-/bi-), CJK and Hangul use plain substring matching."""
    return all(ord(c) < 0x0590 for c in word)


class Relevance:
    def __init__(self, cfg: dict | None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.theme_patterns = [t.upper() for t in cfg.get("gdelt_themes", [])]
        words = [w for lang in (cfg.get("keywords") or {}).values() for w in (lang or [])]
        self.keywords = sorted({fold(w) for w in words if fold(w)})
        # Stems (5+ letters) match at a word start ("terror" -> terrorist, terrorisme); short words
        # must match whole ("arms" must not match "Armstrong").
        spaced = []
        for k in self.keywords:
            if _is_spaced_script(k):
                pat = re.escape(k).replace(r"\ ", r"\s+")
                spaced.append(pat if len(k) >= 5 else pat + r"(?!\w)")
        self._spaced = re.compile(r"(?<!\w)(?:" + "|".join(spaced) + ")") if spaced else None
        self._plain = [k for k in self.keywords if not _is_spaced_script(k)]
        p = cfg.get("prominence") or {}
        self.prominence_above = int(p.get("applies_above_items", 15))
        self.prominence_chars = int(p.get("within_chars", 1500))

    def themes(self, codes: str) -> list[str]:
        if not codes or not self.theme_patterns:
            return []
        found = []
        for code in codes.split(";"):
            up = code.upper()
            if any(p in up for p in self.theme_patterns):
                found.append(code)
        return found

    def keywords_in(self, text: str) -> list[str]:
        f = fold(text)
        out = []
        if self._spaced:
            out += sorted({" ".join(m.group(0).split()) for m in self._spaced.finditer(f)})
        out += [k for k in self._plain if k in f]
        return out

    def check(self, title: str, text: str, themes: str = "") -> tuple[bool, list[str]]:
        """(passes, evidence) — evidence is shown in the report so the reason is visible."""
        if not self.enabled:
            return True, []
        th = self.themes(themes)
        kw = self.keywords_in(f"{title} {text}")
        evidence = ([f"topic:{t}" for t in th[:3]] + kw[:4])
        return bool(th or kw), evidence

    def prominent(self, title: str, body: str, names: list[str], offset: int | None) -> bool:
        """Named in the headline, or in the first part of the article."""
        ft = fold(title)
        if any(fold(n) and fold(n) in ft for n in names):
            return True
        if offset is not None and offset <= self.prominence_chars:
            return True
        head = fold(body[: self.prominence_chars * 2])[: self.prominence_chars]
        return any(fold(n) and fold(n) in head for n in names)
