"""Relevance filtering: drops namesakes and noise so only real alerts go out."""
from __future__ import annotations

import json
import logging
import os
import re

from .matching import fold
from .sources import Hit

log = logging.getLogger(__name__)

_SIGNAL_WORDS = {
    "sanction", "sanctions", "sanctioned", "designated", "designation", "terror", "terrorist",
    "militia", "rebel", "armed group", "commander", "arrest", "arrested", "extradit", "court",
    "trial", "convicted", "killed", "smuggl", "trafficking", "launder", "frozen", "asset freeze",
    "travel ban", "security council", "panel of experts", "embargo", "weapons", "missile",
    "interpol", "wanted", "insurgent", "jihad", "isil", "isis", "al qaida", "al qaeda", "taliban",
    "al shabaab", "houthi", "m23", "fdlr", "adf", "ofac", "treasury",
}


def _profile(rec: dict) -> str:
    return json.dumps({
        "reference": rec["ref"], "type": rec["kind"], "name": rec["name"],
        "aliases": rec.get("aliases", [])[:10], "regime": rec.get("regime"),
        "nationality": rec.get("nationality"), "countries": rec.get("countries"),
        "dates_of_birth": rec.get("dobs"), "designation": rec.get("designation"),
        "listing_notes": (rec.get("comments") or "")[:1200],
    }, ensure_ascii=False)


def heuristic(rec: dict, hits: list[Hit]) -> tuple[list[Hit], list[Hit]]:
    """No-LLM fallback: name (or identifier) must appear AND a corroborating signal.

    Returns (kept, near_misses). Near misses = the name appears but nothing corroborates it.
    """
    corroborators = {fold(c) for c in rec.get("countries", []) if c}
    corroborators |= {fold(w) for d in rec.get("designation", []) for w in re.findall(r"[A-Z][A-Za-z0-9-]{2,}", d)}
    corroborators |= _SIGNAL_WORDS
    kept, near = [], []
    for h in hits:
        text = fold(f"{h.title} {h.snippet}")
        if not h.matched:
            continue
        signals = [c for c in corroborators if c and c in text]
        id_match = any(m.startswith("id:") for m in h.matched)
        if signals or id_match or len(h.matched[0].split()) >= 3:
            h.score = min(1.0, 0.5 + 0.1 * len(signals) + (0.3 if id_match else 0))
            h.summary = "Matched: " + ", ".join(h.matched) + (f" | signals: {', '.join(sorted(signals)[:5])}" if signals else "")
            kept.append(h)
        else:
            h.score = 0.3
            h.summary = "Name appears but nothing in the text corroborates it: " + ", ".join(h.matched)
            near.append(h)
    return kept, near


class LLMScorer:
    """Claude reads the listing profile + each hit and judges identity and relevance."""

    def __init__(self, model: str, threshold: float, near_floor: float = 0.3):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model
        self.threshold = threshold
        self.near_floor = near_floor

    def score(self, rec: dict, hits: list[Hit]) -> tuple[list[Hit], list[Hit]]:
        kept, near = [], []
        for i in range(0, len(hits), 20):
            k, n = self._score_batch(rec, hits[i:i + 20])
            kept += k
            near += n
        return kept, near

    def _score_batch(self, rec: dict, hits: list[Hit]) -> tuple[list[Hit], list[Hit]]:
        items = [
            {"i": i, "title": h.title, "source": h.domain or h.source, "date": h.date,
             "matched_terms": h.matched, "excerpt": (h.snippet or "")[:1200]}
            for i, h in enumerate(hits)
        ]
        prompt = (
            "You screen open-source material for a sanctions-compliance team.\n"
            "Listed party (UN Security Council Consolidated List):\n"
            f"{_profile(rec)}\n\n"
            "Candidate items that matched one of this party's names, nicknames or document numbers:\n"
            f"{json.dumps(items, ensure_ascii=False)}\n\n"
            "For each item give `confidence` (0-1) that it is about THIS listed party (not a namesake) "
            "AND reports a concrete, new development: activity, location, arrest, death, travel, "
            "business/financial dealings, legal action, designation, sanctions evasion, statements, "
            "associates. Generic background or list-reprint pages score low. Be inclusive when the "
            "identity is plausible: a compliance analyst prefers to review a borderline item over "
            "missing it.\n"
            "Reply with ONLY a JSON array, one object per item: "
            '{"i": <int>, "confidence": <0-1>, '
            '"category": "<one of: activity, location, legal, financial, death, designation, evasion, associates, other>", '
            '"summary": "<one sentence, your own words>"}'
        )
        try:
            msg = self.client.messages.create(
                model=self.model, max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            raw = raw[raw.find("["): raw.rfind("]") + 1]
            verdicts = {v["i"]: v for v in json.loads(raw)}
        except Exception as e:  # noqa: BLE001
            log.warning("LLM scoring failed for %s (%s); falling back to heuristic", rec["ref"], e)
            return heuristic(rec, hits)
        kept, near = [], []
        for i, h in enumerate(hits):
            v = verdicts.get(i)
            if not v:
                continue
            h.score = float(v.get("confidence", 0))
            h.summary = v.get("summary", "")
            h.category = v.get("category", "")
            if h.score >= self.threshold:
                kept.append(h)
            elif h.score >= self.near_floor:
                near.append(h)
        return kept, near


def make_scorer(cfg: dict):
    if os.environ.get("ANTHROPIC_API_KEY") and cfg.get("use_llm", True):
        try:
            s = LLMScorer(cfg.get("model", "claude-haiku-4-5-20251001"), float(cfg.get("threshold", 0.6)),
                          float(cfg.get("near_miss_floor", 0.3)))
            log.info("Relevance filter: Claude (%s)", s.model)
            return s.score
        except Exception as e:  # noqa: BLE001
            log.warning("Could not initialise LLM scorer: %s", e)
    log.info("Relevance filter: keyword heuristic (set ANTHROPIC_API_KEY for far fewer false positives)")
    return heuristic
