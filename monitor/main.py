"""Weekly run: sync UN list -> sweep OSINT -> filter -> alert only if something new."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

import yaml

from . import sources, unlist
from .alerts import Report, dispatch, render_markdown
from .matching import NameIndex, all_match_names, context_snippet, find_mentions, fold, search_names
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
                for kept in scorer(rec, [hit]):
                    rep.feed_mentions.append((rec, kept))
        if items:
            st.set_meta(init_key, True)


# --------------------------------------------------------------------- step 4
def un_reports(cfg: dict, st: State, rep: Report, records, index: NameIndex) -> None:
    c = cfg["sources"].get("reports", {})
    if not c.get("enabled"):
        return
    budget = int(c.get("max_new_reports_per_run", 15))
    for page in c.get("pages", []):
        links = sources.list_report_links(page)
        init_key = "init:report:" + page
        first = not st.get_meta(init_key, False)
        for rid, url in links:
            k = key_of("report", rid)
            if st.is_seen(k):
                continue
            if first:  # baseline: existing reports are history, not news
                st.mark(k, "report", url=url)
                continue
            if budget <= 0:
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
        if links:
            st.set_meta(init_key, True)


# --------------------------------------------------------------------- step 5
def news_sweep(cfg: dict, st: State, rep: Report, targets: list[dict], scorer) -> None:
    days = int(cfg["lookback_days"])
    mcfg = cfg.get("matching", {})
    gd, gn = cfg["sources"].get("gdelt", {}), cfg["sources"].get("google_news", {})
    raw: dict[str, list[Hit]] = {r["ref"]: [] for r in targets}
    lock = threading.Lock()

    def run_gdelt():
        for i, r in enumerate(targets):
            names = search_names(r, mcfg.get("min_name_length", 8), mcfg.get("max_names_per_query", 4))
            hs = sources.gdelt(names, days, gd.get("max_records", 75)) if names else []
            with lock:
                raw[r["ref"]].extend(hs)
            if i % 100 == 0:
                log.info("GDELT progress %d/%d", i, len(targets))
            sources.polite_sleep(float(gd.get("delay_seconds", 5.5)))

    def run_gnews():
        eds = gn.get("editions") or [{"hl": "en-US", "gl": "US", "ceid": "US:en"}]
        for i, r in enumerate(targets):
            names = search_names(r, mcfg.get("min_name_length", 8), mcfg.get("max_names_per_query", 4))
            if r.get("original_script") and gn.get("include_original_script", True):
                names = names + [r["original_script"]]
            hs = sources.google_news(names, days, eds) if names else []
            with lock:
                raw[r["ref"]].extend(hs)
            if i % 100 == 0:
                log.info("Google News progress %d/%d", i, len(targets))
            sources.polite_sleep(float(gn.get("delay_seconds", 2.0)))

    workers = []
    if gd.get("enabled", True):
        workers.append(threading.Thread(target=run_gdelt, daemon=True))
    if gn.get("enabled", True):
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
            if cu in seen_local or tk in seen_local or st.is_seen(ku) or (tk and st.is_seen(kt)):
                continue
            seen_local.update({cu, tk})
            st.mark(ku, "gdelt" if h.source == "GDELT" else "google_news", ref, h.url)
            if tk:
                st.mark(kt, "gdelt" if h.source == "GDELT" else "google_news", ref, h.url)
            h.ref = ref
            fresh.setdefault(ref, []).append(h)
    cap = int(cfg.get("scoring", {}).get("max_hits_per_entity", 25))
    for ref in fresh:
        fresh[ref] = fresh[ref][:cap]
    total = sum(len(v) for v in fresh.values())
    log.info("%d new candidate items across %d parties", total, len(fresh))

    # Pull article text so the filter sees the actual sentence mentioning the name.
    fcfg = cfg.get("fetch", {})
    to_fetch = [h for hs in fresh.values() for h in hs if h.source == "GDELT"][: int(fcfg.get("max_page_fetches", 600))]
    with ThreadPoolExecutor(int(fcfg.get("workers", 8))) as ex:
        texts = list(ex.map(lambda h: sources.page_text(h.url), to_fetch))
    page = {id(h): t for h, t in zip(to_fetch, texts)}
    for ref, hs in fresh.items():
        names = all_match_names(by_ref[ref], mcfg.get("min_name_length", 8))
        for h in hs:
            body = page.get(id(h), "")
            h.matched = find_mentions(f"{h.title} {h.snippet} {body}", names)
            if body:
                h.snippet = context_snippet(body, names) or h.snippet

    def judge(item):
        ref, hs = item
        return ref, scorer(by_ref[ref], hs)

    with ThreadPoolExecutor(int(cfg.get("scoring", {}).get("workers", 4))) as ex:
        for ref, kept in ex.map(judge, fresh.items()):
            if kept:
                rep.news[ref] = (by_ref[ref], kept)


# ------------------------------------------------------------------------ main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--limit", type=int, default=int(os.environ.get("MONITOR_LIMIT", "0") or 0),
                    help="only sweep the first N parties (testing)")
    ap.add_argument("--dry-run", action="store_true", help="print the report, send nothing, save no state")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = time.time()

    cfg = load_config(Path(args.config))
    st = State(ROOT / "state/monitor.db")
    rep = Report()
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
        ("UN reports", lambda: un_reports(cfg, st, rep, records, index)),
        ("News sweep", lambda: news_sweep(cfg, st, rep, targets, scorer)),
    )
    for name, step in steps:
        try:
            step()
        except Exception as e:  # noqa: BLE001 — one broken source must not kill the run
            log.exception("%s step failed", name)
            rep.health[name] = f"crashed: {e}"

    rep.health.update(STATS.unhealthy(float(cfg.get("health_failure_ratio", 0.5))))
    log.info("Done in %.1f min. %s", (time.time() - started) / 60, rep.headline() or "No new alerts.")

    if args.dry_run:
        print(render_markdown(rep) if rep.count else "No new alerts.")
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
