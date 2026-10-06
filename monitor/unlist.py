"""Download, parse and diff the UN Security Council Consolidated List."""
from __future__ import annotations

import csv
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from .http import get

DEFAULT_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"

# Fields whose change counts as an "amendment" worth alerting on.
DIFF_FIELDS = (
    "name", "aliases", "low_aliases", "original_script", "regime", "nationality",
    "countries", "dobs", "documents", "designation", "comments", "last_updated",
)


def _t(el, tag: str) -> str:
    x = el.find(tag)
    return " ".join((x.text or "").split()) if x is not None and x.text else ""


def _vals(el, path: str) -> list[str]:
    return [" ".join(v.text.split()) for v in el.findall(path) if v.text and v.text.strip()]


def fetch_xml(url: str = DEFAULT_URL) -> bytes:
    resp = get(url, timeout=180)
    resp.raise_for_status()
    if b"CONSOLIDATED_LIST" not in resp.content[:2000]:
        raise ValueError("Downloaded file does not look like the UN consolidated list")
    return resp.content


def parse(xml_bytes: bytes) -> dict[str, dict]:
    """Return {reference_number: record} for every individual and entity."""
    root = ET.fromstring(xml_bytes)
    records: dict[str, dict] = {}
    groups = (
        ("individual", "INDIVIDUALS/INDIVIDUAL", "INDIVIDUAL_ALIAS", "INDIVIDUAL_ADDRESS"),
        ("entity", "ENTITIES/ENTITY", "ENTITY_ALIAS", "ENTITY_ADDRESS"),
    )
    for kind, path, alias_tag, addr_tag in groups:
        for el in root.findall(path):
            ref = _t(el, "REFERENCE_NUMBER")
            if not ref:
                continue
            name = " ".join(
                p for p in (_t(el, f) for f in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")) if p
            )
            good, low = [], []
            for a in el.findall(alias_tag):
                n = _t(a, "ALIAS_NAME")
                if n:
                    (low if _t(a, "QUALITY").lower() == "low" else good).append(n)
            dobs = []
            for d in el.findall("INDIVIDUAL_DATE_OF_BIRTH"):
                rng = "-".join(filter(None, [_t(d, "FROM_YEAR"), _t(d, "TO_YEAR")]))
                v = _t(d, "DATE") or _t(d, "YEAR") or rng or _t(d, "NOTE")
                if v:
                    dobs.append(v)
            docs = []
            for d in el.findall("INDIVIDUAL_DOCUMENT"):
                v = " ".join(filter(None, [_t(d, "TYPE_OF_DOCUMENT"), _t(d, "NUMBER"), _t(d, "ISSUING_COUNTRY")]))
                if v:
                    docs.append(v)
            nationality = _vals(el, "NATIONALITY/VALUE")
            countries = set(nationality)
            for a in el.findall(addr_tag):
                c = _t(a, "COUNTRY")
                if c:
                    countries.add(c)
            rec = {
                "ref": ref,
                "kind": kind,
                "name": name,
                "original_script": _t(el, "NAME_ORIGINAL_SCRIPT"),
                "aliases": good,
                "low_aliases": low,
                "regime": _t(el, "UN_LIST_TYPE"),
                "listed_on": _t(el, "LISTED_ON"),
                "last_updated": _vals(el, "LAST_DAY_UPDATED/VALUE"),
                "nationality": nationality,
                "countries": sorted(countries),
                "dobs": dobs,
                "documents": docs,
                "designation": _vals(el, "DESIGNATION/VALUE"),
                "comments": _t(el, "COMMENTS1"),
            }
            rec["hash"] = hashlib.sha256(
                json.dumps({k: rec[k] for k in DIFF_FIELDS}, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            records[ref] = rec
    return records


def diff(old: dict[str, dict], new: dict[str, dict]) -> dict[str, list]:
    added = [new[r] for r in sorted(set(new) - set(old))]
    removed = [old[r] for r in sorted(set(old) - set(new))]
    amended = []
    for r in sorted(set(old) & set(new)):
        if old[r].get("hash") != new[r].get("hash"):
            changed = [f for f in DIFF_FIELDS if old[r].get(f) != new[r].get(f)]
            amended.append({"record": new[r], "old": old[r], "changed_fields": changed})
    return {"added": added, "removed": removed, "amended": amended}


def load_snapshot(path: Path) -> dict[str, dict]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_snapshot(path: Path, records: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def export_csv(path: Path, records: dict[str, dict]) -> None:
    """Human-readable copy of the full list (the 'list everyone' deliverable)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["ref", "kind", "name", "original_script", "aliases", "low_aliases", "regime", "listed_on",
            "nationality", "countries", "dobs", "documents", "designation", "last_updated", "comments"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for rec in sorted(records.values(), key=lambda r: r["ref"]):
            w.writerow(["; ".join(rec[c]) if isinstance(rec[c], list) else rec[c] for c in cols])
