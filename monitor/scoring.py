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


def heuristic(rec: dict, hits: list[Hit]) -> list[Hit]:
    """No-LLM fallback: name must appear AND at least one corroborating signal."""
    corroborators = {fold(c) for c in rec.get("countries", []) if c}
    corroborators |= {fold(w) for d in rec.get("designation", []) for w in re.findall(r"[A-Z][A-Za-z0-9-]{2,}", d)}
    corroborators |= _SIGNAL_WORDS
    kept = []
    for h in hits:
        text = fold(f"{h.title} {h.snippet}")
        if not h.matched:
            continue
        signals = [c for c in corroborators if c and c in text]
        if signals or len(h.matched[0].split()) >= 3:
            h.score = min(1.0, 0.5 + 0.1 * len(signals))
            h.summary = "Matched: " + ", ".join(h.matched) + (f" | signals: {', '.join(sorted(signals)[:5])}" if signals else "")
            kept.append(h)
    return kept


class LLMScorer:
    """Claude reads the listing profile + each hit and judges identity and relevance."""

    def __init__(self, model: str, threshold: float):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model
        self.threshold = threshold

    def score(self, rec: dict, hits: list[Hit]) -> list[Hit]:
        items = [
            {"i": i, "title": h.title, "source": h.domain or h.source, "date": h.date,
             "excerpt": (h.snippet or "")[:1200]}
            for i, h in enumerate(hits)
        ]
        prompt = (
            "You screen open-source news for a sanctions-compliance team.\n"
            "Listed party (UN Security Council Consolidated List):\n"
            f"{_profile(rec)}\n\n"
            "Candidate items mentioning a name that matches this party:\n"
            f"{json.dumps(items, ensure_ascii=False)}\n\n"
            "For each item decide: (1) is it plausibly about THIS listed party rather than a namesake, "
            "and (2) does it report a new, concrete development (activity, location, arrest, death, "
            "travel, business/financial dealings, legal action, new designation, sanctions evasion, "
            "statements)? Generic background or list-reprint pages are not relevant.\n"
            "Reply with ONLY a JSON array, one object per item: "
            '{"i": <int>, "relevant": <bool>, "confidence": <0-1>, '
            '"category": "<one of: activity, location, legal, financial, death, designation, evasion, other>", '
            '"summary": "<one sentence, your own words>"}'
        )
        try:
            msg = self.client.messages.create(
                model=self.model, max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            raw = raw[raw.find("["): raw.rfind("]") + 1]
            verdicts = {v["i"]: v for v in json.loads(raw)}
        except Exception as e:  # noqa: BLE001
            log.warning("LLM scoring failed for %s (%s); falling back to heuristic", rec["ref"], e)
            return heuristic(rec, hits)
        kept = []
        for i, h in enumerate(hits):
            v = verdicts.get(i)
            if v and v.get("relevant") and float(v.get("confidence", 0)) >= self.threshold:
                h.score = float(v["confidence"])
                h.summary = v.get("summary", "")
                h.category = v.get("category", "")
                kept.append(h)
        return kept


def make_scorer(cfg: dict):
    if os.environ.get("ANTHROPIC_API_KEY") and cfg.get("use_llm", True):
        try:
            s = LLMScorer(cfg.get("model", "claude-haiku-4-5-20251001"), float(cfg.get("threshold", 0.6)))
            log.info("Relevance filter: Claude (%s)", s.model)
            return s.score
        except Exception as e:  # noqa: BLE001
            log.warning("Could not initialise LLM scorer: %s", e)
    log.info("Relevance filter: keyword heuristic (set ANTHROPIC_API_KEY for far fewer false positives)")
    return heuristic
