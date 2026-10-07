"""Weekly run: sync UN list -> sweep OSINT -> filter -> alert only if something new."""
from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import yaml

from . import sources, unlist
from .alerts import Report, dispatch, render_markdown
from .matching import (NameIndex, all_match_names, context_for, context_snippet, context_terms,
                       find_identifiers, find_mentions, fold, identifiers, news_match_names, plan_queries,
                       search_names)
from .relevance import Relevance
from .scoring import make_scorer
from .sources import STATS, Hit
from .state import State, canonical_url, key_of

log = logging.getLogger("monitor")
ROOT = Path(os.environ.get("MONITOR_ROOT", "."))


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# --------------------------------------------------------------------- step 1
def sync_list(cfg: dict, st: State, rep: Report, save: bool = True) -> dict[str, dict]:
    snap_path = ROOT / "state/un_list.json"
    old = unlist.load_snapshot(snap_path)
    try:
        new = unlist.parse(unlist.fetch_xml(cfg["list"]["url"]))
    except Exception as e:  # noqa: BLE001
        log.error("UN list download failed: %s", e)
        rep.health["UN Consolidated List"] = f"download/parse failed: {e}"
        if not old:
            raise SystemExit("No UN list available (first run and download failed).")
        return old

    if old and len(new) < 0.7 * len(old):
        rep.health["UN Consolidated List"] = (
            f"new file has {len(new)} records vs {len(old)} last week — treated as a bad download, "
            "previous snapshot kept")
        return old

    if old:
        d = unlist.diff(old, new)
        rep.list_added, rep.list_removed, rep.list_amended = d["added"], d["removed"], d["amended"]
    else:
        log.info("First run: baseline of %d UN records stored (no list-change alerts).", len(new))
    if save:
        unlist.save_snapshot(snap_path, new)
        unlist.export_csv(ROOT / "data/un_consolidated_list.csv", new)
    log.info("UN list: %d records (%d individuals, %d entities)", len(new),
             sum(r["kind"] == "individual" for r in new.values()),
             sum(r["kind"] == "entity" for r in new.values()))
    return new


# --------------------------------------------------------------------- step 2
def cross_lists(cfg: dict, st: State, rep: Report, records: dict[str, dict]) -> None:
    c = cfg["sources"].get("opensanctions", {})
    if not c.get("enabled"):
        return
    new = sources.opensanctions_datasets(c["url"], records)
    if not new:
        return
    old, initialised = st.get_xref(), st.get_meta("xref_init", False)
    if initialised:
        for ref, ds in new.items():
            added = sorted(set(ds) - set(old.get(ref, [])))
            if added and ref in records:
                rep.xref_added.append((records[ref], added))
    st.set_xref(new)
    st.set_meta("xref_init", True)
    log.info("OpenSanctions: %d UN parties linked to other lists", len(new))


# --------------------------------------------------------------------- step 3
def watched_feeds(cfg: dict, st: State, rep: Report, records, index: NameIndex, scorer) -> None:
    feed_rel = Relevance(cfg.get("relevance"))
    urls = list(cfg["sources"].get("feeds", []) or [])
    gn = cfg["sources"].get("google_news", {})
    for q in cfg["sources"].get("topic_queries", []) or []:
        ed = (gn.get("editions") or [{"hl": "en-US", "gl": "US", "ceid": "US:en"}])[0]
        urls.append("https://news.google.com/rss/search?" + urlencode(
            {"q": f"{q} when:{cfg['lookback_days']}d", **ed}))
    for url in urls:
        items = sources.read_feed(url)
        init_key = "init:feed:" + url
        first = not st.get_meta(init_key, False)
        for h in items:
            k = key_of("feed", canonical_url(h.url) or h.title)
            if st.is_seen(k):
                continue
            st.mark(k, "feed", url=h.url)
            if first:
                continue
            for ref, found in index.scan(f"{h.title}\n{h.snippet}").items():
                rec = records[ref]
                hit = Hit(**{**h.__dict__, "ref": ref, "matched": [n for n, _ in found],
                             "snippet": found[0][1]})
                ok, ev = feed_rel.check(hit.title, hit.snippet)
                if not ok:
                    continue
                hit.evidence = ev
                kept, near = scorer(rec, [hit])
                for k in kept:
                    rep.feed_mentions.append((rec, k))
                for n in near:
                    rep.near_misses.setdefault(ref, (rec, []))[1].append(n)
        if items:
            st.set_meta(init_key, True)


# --------------------------------------------------------------------- step 3b
def official_sources(cfg: dict, st: State, rep: Report, records, index: NameIndex) -> None:
    """New UN Security Council, US Treasury/OFAC/State/Justice/FBI and UK OFSI releases, read in
    full and checked for every listed name. Official text is high-signal: no context gate."""
    c = cfg["sources"].get("official", {})
    if not c.get("enabled"):
        return
    budget = int(c.get("max_pages_per_run", 400))
    first_n = int(c.get("first_run_items", 15))
    for src in c.get("sources", []) or []:
        items = sources.list_official_items(src)
        if items is None:
            continue
        init_key = "init:official:" + (src.get("url") or src.get("name", ""))
        first = not st.get_meta(init_key, False)
        scanned = 0
        for i, (url, title) in enumerate(items):
            k = key_of("official", canonical_url(url))
            if st.is_seen(k):
                continue
            if first and i >= first_n:            # first run: recent items only, rest is history
                st.mark(k, "official", url=url)
                continue
            if budget <= 0:
                rep.notes.append("Official-release limit reached; the rest are read next run")
                break
            budget -= 1
            try:
                text = sources.document_text(url)
                STATS.ok("official_pages")
            except Exception as e:  # noqa: BLE001
                STATS.fail("official_pages", e)
                continue                           # not marked: retried next run
            st.mark(k, "official", url=url)
            scanned += 1
            for ref, found in index.scan(f"{title}\n{text}").items():
                if any(m[2] == url and m[3]["ref"] == ref for m in rep.official_mentions):
                    continue
                rep.official_mentions.append((src.get("name", src.get("url", "")), title or url, url,
                                              records[ref], found))
        log.info("Official source %s: %d listed, %d new read", src.get("name"), len(items), scanned)
        st.set_meta(init_key, True)


# --------------------------------------------------------------------- step 4
def un_reports(cfg: dict, st: State, rep: Report, records, index: NameIndex) -> None:
    c = cfg["sources"].get("reports", {})
    if not c.get("enabled"):
        return
    budget = int(c.get("max_new_reports_per_run", 100))
    pages: list[list[str]] = []
    base = c.get("committee_base", "https://main.un.org/securitycouncil/en/sanctions/{n}/")
    for n in c.get("committees", []) or []:
        pages.append([base.format(n=n) + v for v in c.get("page_variants", [])])
    pages += [[p] for p in c.get("pages", []) or []]
    for variants in pages:
        links, page = [], variants[0]
        for v in variants:                       # UN site paths differ by committee: try each form
            links = sources.list_report_links(v, quiet=True)
            if links:
                page = v
                break
        if links:
            STATS.ok("report_pages")
        else:
            STATS.fail("report_pages", f"no reports found at {variants[0]} (or its variants)")
            log.warning("No report listing found for %s", variants[0])
            continue
        init_key = "init:report:" + variants[0]
        first = not st.get_meta(init_key, False)
        for rid, url in links:
            k = key_of("report", rid)
            if st.is_seen(k):
                continue
            if first:  # baseline: existing reports are history, not news
                st.mark(k, "report", url=url)
                continue
            if budget <= 0:
                rep.notes.append("Report limit reached; remaining new reports will be scanned next run")
                break
            budget -= 1
            try:
                text = sources.document_text(url)
                STATS.ok("report_download")
            except Exception as e:  # noqa: BLE001
                STATS.fail("report_download", e)
                log.warning("Report %s failed: %s", url, e)
                continue  # not marked seen -> retried next week
            st.mark(k, "report", url=url)
            for ref, found in index.scan(text).items():
                rep.report_mentions.append((rid, url, records[ref], found))
            log.info("Scanned report %s (%d chars)", rid, len(text))
        log.info("Reports page %s: %d documents listed", page, len(links))
        st.set_meta(init_key, True)


# --------------------------------------------------------------------- step 5a
def gdelt_bulk(cfg: dict, st: State, rep: Report, targets: list[dict]) -> dict[str, list[Hit]]:
    """All GDELT-monitored news since the last run, matched against every name at once."""
    c = cfg["sources"].get("gdelt", {})
    if not c.get("bulk", True):
        return {}
    from . import gkg

    now = gkg.now_utc()
    floor = now - timedelta(days=int(cfg.get("max_catchup_days", 30)))
    default = now - timedelta(days=int(cfg["lookback_days"]))
    # Tracked per party, so a party that is new to the sweep (newly listed, regime filter changed,
    # test runs) still gets its full look-back instead of inheriting someone else's window.
    last_by_ref = st.get_meta("gkg_last_by_ref", {}) or {}
    since = {r["ref"]: max(datetime.fromisoformat(last_by_ref[r["ref"]]) if r["ref"] in last_by_ref
                           else default, floor) for r in targets}
    if not since:
        return {}
    start = min(since.values())
    retry = st.get_meta("gkg_retry", []) or []
    streams = tuple(c.get("streams", ["english", "translated"]))
    hits, stats = gkg.scan({r["ref"]: r for r in targets}, start, now, streams,
                           workers=int(c.get("workers", os.cpu_count() or 4)),
                           min_len=int(cfg.get("matching", {}).get("min_name_length", 8)), retry=retry)
    for _ in range(stats["ok"]):
        STATS.ok("gdelt_bulk")
    for e in stats["errors"]:
        STATS.fail("gdelt_bulk", e)
    # Files that failed are re-read next run, so a GDELT/network hiccup never leaves a gap.
    st.set_meta("gkg_retry", stats["failed_jobs"][-2000:])
    for ref in since:
        last_by_ref[ref] = now.isoformat()
    st.set_meta("gkg_last_by_ref", last_by_ref)
    if stats["failed"]:
        rep.notes.append(f"GDELT: {stats['failed']} of {stats['files']} 15-minute files could not be read; "
                         "they will be re-read next run")
    rep.coverage["gdelt_articles"] = stats["rows"]

    def in_window(h) -> bool:
        try:
            when = datetime.strptime(h["date"], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return when > since[h["ref"]] - timedelta(minutes=15)

    hits = [h for h in hits if in_window(h)]
    out: dict[str, list[Hit]] = {}
    for h in hits:
        out.setdefault(h["ref"], []).append(Hit(
            "GDELT", h["title"], h["url"], h["date"], h["domain"],
            "translated" if h["stream"] == "translated" else "en", matched=[h["name"]],
            themes=h.get("themes", ""), offset=h.get("offset")))
    return out


# --------------------------------------------------------------------- step 5
def c_total(cov: dict) -> int:
    return sum(c["queries"] - c["failed"] for c in cov.values())


def editions_for(rec: dict, gn: dict) -> list[dict]:
    """Base editions + local-language editions matching the party's countries."""
    eds = list(gn.get("editions") or [{"hl": "en-US", "gl": "US", "ceid": "US:en"}])
    countries = " | ".join(fold(c) for c in rec.get("countries", []) + rec.get("nationality", []))
    for ed in (gn.get("local_editions") or {}).values():
        if any(re.search(rf"(?<!\w){re.escape(fold(k))}(?!\w)", countries) for k in ed.get("countries", [])):
            e = {k: ed[k] for k in ("hl", "gl", "ceid")}
            if e not in eds:
                eds.append(e)
    return eds


def news_sweep(cfg: dict, st: State, rep: Report, targets: list[dict], scorer, started: float,
               bulk: dict[str, list[Hit]] | None = None) -> None:
    gkg_ok = bulk is not None and rep.coverage.get("gdelt_articles", 0) > 0
    mcfg = cfg.get("matching", {})
    min_len, per_q = int(mcfg.get("min_name_length", 8)), int(mcfg.get("max_names_per_query", 4))
    gd, gn = cfg["sources"].get("gdelt", {}), cfg["sources"].get("google_news", {})
    sources.GDELT_LIMIT.interval = float(gd.get("delay_seconds", 5.5))
    sources.GNEWS_LIMIT.interval = float(gn.get("delay_seconds", 2.0))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    default_days = int(cfg["lookback_days"])
    max_days = int(cfg.get("max_catchup_days", 30))
    deadline = started + 60 * float(cfg.get("max_sweep_minutes", 270))

    # Oldest-searched first, so anything deferred by the time budget goes first next week, and its
    # window then reaches back to its last successful search: no gaps in coverage.
    last = st.get_meta("last_searched", {}) or {}
    targets = sorted(targets, key=lambda r: (last.get(r["ref"], ""), r["ref"]))

    def window(ref: str) -> datetime:
        if ref in last:
            since = datetime.fromisoformat(last[ref]) - timedelta(days=1)   # 1-day overlap
            return max(since, now - timedelta(days=max_days))
        return now - timedelta(days=default_days)

    plans = {r["ref"]: plan_queries(r, min_len, per_q) for r in targets}
    raw: dict[str, list[Hit]] = {r["ref"]: list((bulk or {}).get(r["ref"], [])) for r in targets}
    cov = {r["ref"]: {"queries": 0, "failed": 0, "raw": 0, "done": set()} for r in targets}
    lock = threading.Lock()

    def record(ref, hs, src):
        with lock:
            cov[ref]["queries"] += 1
            if hs is None:
                cov[ref]["failed"] += 1
            else:
                cov[ref]["raw"] += len(hs)
                raw[ref].extend(hs)

    def run_gdelt():
        for i, r in enumerate(targets):
            if time.time() > deadline:
                log.warning("Time budget reached: GDELT stopped at %d/%d", i, len(targets))
                return
            for q in plans[r["ref"]]:
                record(r["ref"], sources.gdelt(q.terms, window(r["ref"]), now, q.context,
                                               int(gd.get("max_records", 250))), "gdelt")
            with lock:
                cov[r["ref"]]["done"].add("gdelt")
            if i % 100 == 0:
                log.info("GDELT progress %d/%d", i, len(targets))

    base_eds = gn.get("editions") or [{"hl": "en-US", "gl": "US", "ceid": "US:en"}]
    english_names = bool(gn.get("english_names", False)) or not gkg_ok

    def run_gnews():
        for i, r in enumerate(targets):
            if time.time() > deadline:
                log.warning("Time budget reached: Google News stopped at %d/%d", i, len(targets))
                return
            if sources.STATS.is_tripped("google_news"):
                log.warning("Google News unavailable: stopped at %d/%d; the rest go first next run",
                            i, len(targets))
                return
            for ed in editions_for(r, gn):
                for j, q in enumerate(plans[r["ref"]]):
                    # English-language news for plain names is already covered by the GDELT bulk
                    # feed; Google News' limited quota goes to local languages, nicknames and IDs.
                    if q.kind == "names" and ed in base_eds and not english_names:
                        continue
                    terms = list(q.terms)
                    if j == 0 and r.get("original_script") and gn.get("include_original_script", True):
                        terms.append(r["original_script"])
                    record(r["ref"], sources.google_news(terms, window(r["ref"]), now, ed, q.context),
                           "google_news")
            if sources.STATS.is_tripped("google_news"):
                return                      # this party's searches were cut short: not done
            with lock:
                cov[r["ref"]]["done"].add("google_news")
            if i % 100 == 0:
                log.info("Google News progress %d/%d", i, len(targets))

    enabled = []
    workers = []
    if gd.get("doc_api", False):
        enabled.append("gdelt")
        workers.append(threading.Thread(target=run_gdelt, daemon=True))
    if gn.get("enabled", True):
        enabled.append("google_news")
        workers.append(threading.Thread(target=run_gnews, daemon=True))
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    # De-duplicate and drop anything already reported in earlier weeks.
    by_ref = {r["ref"]: r for r in targets}
    fresh: dict[str, list[Hit]] = {}
    for ref, hits in raw.items():
        seen_local = set()
        for h in hits:
            cu = canonical_url(h.url)
            tk = fold(h.title)[:120]
            ku, kt = key_of(ref, cu), key_of(ref, "t", tk)
            if cu in seen_local or (tk and tk in seen_local) or st.is_seen(ku) or (tk and st.is_seen(kt)):
                continue
            seen_local.update({cu, tk})
            src = "gdelt" if h.source == "GDELT" else "google_news"
            st.mark(ku, src, ref, h.url)
            if tk:
                st.mark(kt, src, ref, h.url)
            h.ref = ref
            fresh.setdefault(ref, []).append(h)
    # Context gate, part 1 (before any cap): GDELT articles must carry a security/sanctions topic
    # or keyword in the headline. GDELT tags topics in every language it reads.
    rel = Relevance(cfg.get("relevance"))
    dropped_irrelevant = 0
    for ref in list(fresh):
        keep = []
        for h in fresh[ref]:
            if h.source == "GDELT" and rel.enabled and not any(m.startswith("id:") for m in h.matched):
                ok, ev = rel.check(h.title, "", h.themes)
                if not ok:
                    dropped_irrelevant += 1
                    continue
                h.evidence = ev
            keep.append(h)
        fresh[ref] = keep
    cap = int(cfg.get("scoring", {}).get("max_hits_per_entity", 100))
    for ref in fresh:
        names_f = [fold(n) for n in news_match_names(by_ref[ref], min_len)]
        fresh[ref].sort(key=lambda h: (not any(n in fold(h.title) for n in names_f), h.source != "GDELT"))
        if len(fresh[ref]) > cap:
            rep.notes.append(f"{by_ref[ref]['name']} ({ref}): {len(fresh[ref])} new items, "
                             f"only the first {cap} were reviewed (raise scoring.max_hits_per_entity)")
            fresh[ref] = fresh[ref][:cap]
    log.info("%d new candidate items across %d parties", sum(len(v) for v in fresh.values()), len(fresh))

    # Pull article text so the filter sees the actual sentence mentioning the name.
    fcfg = cfg.get("fetch", {})
    gd_hits = [h for hs in fresh.values() for h in hs if h.source == "GDELT"]
    limit = int(fcfg.get("max_page_fetches", 3000))
    if len(gd_hits) > limit:
        rep.notes.append(f"{len(gd_hits) - limit} articles were judged on headline only (fetch.max_page_fetches)")
    to_fetch = gd_hits[:limit]
    with ThreadPoolExecutor(int(fcfg.get("workers", 16))) as ex:
        texts = list(ex.map(lambda h: sources.page_text(h.url), to_fetch))
    page = {id(h): t for h, t in zip(to_fetch, texts)}
    for ref, hs in fresh.items():
        rec = by_ref[ref]
        names, ids = news_match_names(rec, min_len), identifiers(rec)
        for h in hs:
            body = page.get(id(h), "")
            text = f"{h.title} {h.snippet} {body}"
            h.matched = find_mentions(text, names) + [f"id:{i}" for i in find_identifiers(text, ids)]
            if body:
                h.snippet = context_snippet(body, names) or h.snippet

    # Nickname/acronym matches only count if a context term (organisation, country) is present;
    # republished old articles (URL dated well before the window) are dropped.
    dropped_ctx = dropped_old = 0
    for ref in list(fresh):
        rec = by_ref[ref]
        full = {fold(n) for n in all_match_names(rec, min_len)}
        ctx_all = context_terms(rec)
        oldest = window(ref) - timedelta(days=30)
        keep = []
        for h in fresh[ref]:
            names_hit = [m for m in h.matched if not m.startswith("id:")]
            ids_hit = [m for m in h.matched if m.startswith("id:")]
            text = " " + fold(f"{h.title} {h.snippet} {page.get(id(h), '')}") + " "
            ctx = {fold(c) for n in names_hit for c in context_for(n, ctx_all)}
            if names_hit and not ids_hit and not (set(names_hit) & full) and not any(
                    f" {c} " in text for c in ctx):
                dropped_ctx += 1
                continue
            m = re.search(r"/(20\d{2})/(\d{1,2})/", h.url)
            if m and 1 <= int(m.group(2)) <= 12 and datetime(int(m.group(1)), int(m.group(2)), 28,
                                                            tzinfo=timezone.utc) < oldest:
                dropped_old += 1
                continue
            keep.append(h)
        fresh[ref] = keep
    rep.coverage["dropped_no_context"] = dropped_ctx
    rep.coverage["dropped_republished"] = dropped_old

    # Context gate, part 2: every remaining item (Google News, feeds) must mention a security or
    # sanctions topic in its headline, snippet or article text; then, for parties with heavy
    # coverage, the party must be named in the headline or the opening of the article.
    dropped_not_prominent = 0
    for ref in list(fresh):
        rec = by_ref[ref]
        names = news_match_names(rec, min_len)
        keep = []
        for h in fresh[ref]:
            if any(m.startswith("id:") for m in h.matched) or not rel.enabled:
                keep.append(h)
                continue
            ok, ev = rel.check(h.title, f"{h.snippet} {page.get(id(h), '')}", h.themes)
            if not ok:
                dropped_irrelevant += 1
                continue
            h.evidence = sorted(set(getattr(h, "evidence", []) + ev))[:6]
            keep.append(h)
        if len(keep) > rel.prominence_above:
            prominent = [h for h in keep if any(m.startswith("id:") for m in h.matched)
                         or rel.prominent(h.title, page.get(id(h), "") or h.snippet, names, h.offset)]
            dropped_not_prominent += len(keep) - len(prominent)
            keep = prominent
        fresh[ref] = keep
    rep.coverage["dropped_irrelevant"] = dropped_irrelevant
    rep.coverage["dropped_not_prominent"] = dropped_not_prominent

    def judge(item):
        ref, hs = item
        return ref, scorer(by_ref[ref], hs)

    with ThreadPoolExecutor(int(cfg.get("scoring", {}).get("workers", 4))) as ex:
        for ref, (kept, near) in ex.map(judge, fresh.items()):
            if kept:
                rep.news[ref] = (by_ref[ref], kept)
            if near:
                rep.near_misses[ref] = (by_ref[ref], near)

    # Coverage bookkeeping. GDELT bulk covers every party each run; Google News works through the
    # parties oldest-first and anything it did not reach goes first next run, with its window
    # reaching back to its last search, so nothing is skipped.
    rows, failed, deferred = [], [], []
    for r in targets:
        c, ref = cov[r["ref"]], r["ref"]
        complete = set(enabled) <= c["done"]
        gdelt_note = "GDELT bulk: scanned" if gkg_ok else "GDELT bulk: NOT scanned"
        if not complete:
            status = f"{gdelt_note}; Google News: pending, searched first next run"
            deferred.append(r)
        elif c["queries"] and c["failed"] == c["queries"]:
            status = f"{gdelt_note}; Google News: every query failed, retried first next run"
            failed.append(r)
        elif c["failed"]:
            status = f"{gdelt_note}; Google News: {c['failed']}/{c['queries']} queries failed"
        else:
            status = f"{gdelt_note}; Google News: searched"
        if not search_names(r, min_len, 50) and not any(q.kind == "nicknames" for q in plans[ref]) \
                and not identifiers(r):
            status += " (only very common names on the list — cannot be searched reliably)"
        if complete and ref not in {x["ref"] for x in failed}:
            last[ref] = now.isoformat()
        rows.append({
            "ref": ref, "name": r["name"], "regime": r.get("regime", ""),
            "window_start": window(ref).date().isoformat(),
            "queries": c["queries"], "failed": c["failed"], "raw_results": c["raw"],
            "new_items": len(fresh.get(ref, [])), "alerts": len(rep.news.get(ref, (None, []))[1]),
            "near_misses": len(rep.near_misses.get(ref, (None, []))[1]), "status": status,
        })
    st.set_meta("last_searched", last)
    rep.coverage.update({
        "parties": len(targets), "gdelt_ok": gkg_ok,
        "searched": len(targets) - len(deferred) - len(failed),
        "queries": sum(c["queries"] for c in cov.values()),
        "failed_queries": sum(c["failed"] for c in cov.values()),
        "raw_results": sum(c["raw"] for c in cov.values()),
        "new_items": sum(len(v) for v in fresh.values()),
        "deferred": [f"{r['name']} ({r['ref']})" for r in deferred],
    })
    if not gkg_ok:
        rep.health["GDELT bulk feed"] = "no articles were scanned this run; affected parties are re-scanned next run"
    if failed or (deferred and sources.STATS.is_tripped("google_news")):
        rep.health["Google News"] = (
            f"Google News refused requests (rate limiting) after {c_total(cov)} searches. "
            f"{len(failed) + len(deferred)} parties' Google News searches are carried over and done first "
            f"next run{' — GDELT bulk coverage of all parties was complete' if gkg_ok else ''}.")
    path = ROOT / "data/coverage.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["ref"])
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------------------ main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--limit", type=int, default=int(os.environ.get("MONITOR_LIMIT", "0") or 0),
                    help="only sweep the first N parties (testing)")
    ap.add_argument("--dry-run", action="store_true", help="print the report, send nothing, save no state")
    ap.add_argument("--replay", action="store_true",
                    help="dry run against an empty temporary state (re-scans the last week as if new); "
                         "the report is written to logs/replay_report.md")
    ap.add_argument("--skip-google-news", action="store_true", help="GDELT bulk only (faster)")
    args = ap.parse_args(argv)
    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(ROOT / "logs/last_run.log", mode="w", encoding="utf-8")])
    logging.getLogger("urllib3").setLevel(logging.ERROR)   # per-article fetch retries are noise
    started = time.time()

    cfg = load_config(Path(args.config))
    if args.replay:
        args.dry_run = True
    if args.skip_google_news:
        cfg["sources"].setdefault("google_news", {})["enabled"] = False
    STATS.breaker = int(cfg.get("circuit_breaker_failures", 15))
    if args.replay:
        import tempfile
        st = State(Path(tempfile.mkdtemp()) / "replay.db")
    else:
        st = State(ROOT / "state/monitor.db")
    rep = Report(near_triggers=bool(cfg.get("scoring", {}).get("near_misses_trigger_alert", True)))
    scorer = make_scorer(cfg.get("scoring", {}))

    records = sync_list(cfg, st, rep, save=not args.dry_run)
    index = NameIndex(records, cfg.get("matching", {}).get("min_name_length", 8))

    regimes = set(cfg["list"].get("regimes") or [])
    targets = [r for r in records.values() if not regimes or r.get("regime") in regimes]
    targets.sort(key=lambda r: r["ref"])
    if args.limit:
        targets = targets[: args.limit]

    steps = (
        ("Other sanctions lists", lambda: cross_lists(cfg, st, rep, records)),
        ("Watched feeds", lambda: watched_feeds(cfg, st, rep, records, index, scorer)),
        ("Official releases", lambda: official_sources(cfg, st, rep, records, index)),
        ("UN reports", lambda: un_reports(cfg, st, rep, records, index)),
        ("News sweep", lambda: news_sweep(cfg, st, rep, targets, scorer, started,
                                          gdelt_bulk(cfg, st, rep, targets))),
    )
    for name, step in steps:
        t0 = time.time()
        log.info("== %s: starting", name)
        try:
            step()
        except Exception as e:  # noqa: BLE001 — one broken source must not kill the run
            log.exception("%s step failed", name)
            rep.health[name] = f"crashed: {e}"
        log.info("== %s: finished in %.1f min", name, (time.time() - t0) / 60)

    problems = STATS.unhealthy(float(cfg.get("health_failure_ratio", 0.5)))
    if "Google News" in rep.health:          # already explained, with what happens next
        problems.pop("google_news", None)
    rep.health.update(problems)
    log.info("Done in %.1f min. %s", (time.time() - started) / 60, rep.headline() or "No new alerts.")

    if args.dry_run:
        md = render_markdown(rep) if rep.count else "No new alerts."
        print(md)
        if args.replay:
            (ROOT / "logs/replay_report.md").write_text(md, encoding="utf-8")
            from .alerts import write_report
            import shutil, tempfile
            tmp = Path(tempfile.mkdtemp()) / "reports"
            write_report(rep, md, tmp)
            shutil.copy(tmp.parent / "data" / "alerts" / f"{rep.run_date}.csv", ROOT / "logs/replay_items.csv")
        return 0

    if rep.count:
        sent = dispatch(rep, ROOT / "reports")
        log.info("Alerts delivered via: %s", ", ".join(sent))
    else:
        log.info("Nothing new this week — no alert sent.")
    st.set_meta("last_run", rep.run_date)
    st.commit()

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### Sanctions monitor {rep.run_date}\n\n{rep.headline() or 'No new alerts.'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
