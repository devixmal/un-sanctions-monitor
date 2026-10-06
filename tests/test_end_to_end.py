"""Two simulated weekly runs with all network calls stubbed."""
import importlib

from monitor import sources
from monitor.sources import Hit
from tests.test_monitor import SAMPLE


def _stub(monkeypatch, tmp_path, xml, news, feed_items=(), reports=(), report_text="", xref=None, extra_cfg=""):
    monkeypatch.setenv("MONITOR_ROOT", str(tmp_path))
    for k in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "SMTP_HOST", "SLACK_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    import monitor.main as m
    m = importlib.reload(m)
    monkeypatch.setattr(m.unlist, "fetch_xml", lambda url: xml)
    calls = []

    def fake_gdelt(terms, start, end, context=None, n=250):
        calls.append(("gdelt", tuple(terms), tuple(context or ())))
        return [Hit(**h.__dict__) for h in news if h.source == "GDELT" and any("MAKENGA" in t for t in terms)]

    def fake_gnews(terms, start, end, ed, context=None):
        calls.append(("gnews", ed["hl"], tuple(terms), tuple(context or ())))
        if tuple(terms) == ("ADF",) and ed["hl"] == "en-US":
            return [Hit("Google News", "ADF troops in joint drills with US marines", "https://au.example/1",
                        snippet="Australian Defence Force exercise"),
                    Hit("Google News", "ADF rebels kill 12 near Beni, Congo", "https://cd.example/2",
                        snippet="ADF militia attack in North Kivu, Congo")]
        return []

    from monitor import gkg

    def fake_scan(records, start, end, streams, workers=4, min_len=8, retry=None):
        calls.append(("gkg", tuple(sorted(records)), tuple(retry or [])))
        hits = [{"ref": "CDi.008", "name": "sultani makenga", "url": h.url, "domain": "news.example",
                 "date": h.date, "title": h.title, "stream": "english"} for h in news if "CDi.008" in records]
        return hits, {"files": 10, "ok": 9, "missing": 0, "failed": 1, "rows": 5000,
                      "errors": ["english 1: HTTP 500"], "failed_jobs": [["english", "20261005000000"]]}
    monkeypatch.setattr(gkg, "scan", fake_scan)
    monkeypatch.setattr(sources, "gdelt", fake_gdelt)
    monkeypatch.setattr(sources, "google_news", fake_gnews)
    m.CALLS = calls
    monkeypatch.setattr(sources, "read_feed", lambda url: list(feed_items))
    monkeypatch.setattr(sources, "list_report_links", lambda page, quiet=False: list(reports))
    monkeypatch.setattr(sources, "document_text", lambda url: report_text)
    monkeypatch.setattr(sources, "opensanctions_datasets", lambda url, recs: xref or {})
    monkeypatch.setattr(sources, "page_text", lambda url: "Rebel commander Sultani Makenga of the M23 was seen in Goma, DRC.")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(open("config.yaml").read() + extra_cfg)
    return m, ["--config", str(cfg)]


def test_two_runs(monkeypatch, tmp_path):
    report_link = [("S/2026/900", "https://example.org/r.pdf")]
    # Week 1: baseline. News exists, so it IS alerted (real recent activity), list is baseline only.
    news = [Hit("GDELT", "M23 leader Makenga in Goma talks", "https://news.example/a?utm_source=x", "20261005")]
    m, args = _stub(monkeypatch, tmp_path, SAMPLE, news, reports=report_link,
                    xref={"CDi.008": ["us_ofac_sdn"]})
    assert m.main(args) == 0
    # Every name variant, the nickname-with-context and the passport number were searched,
    # and the French edition was used for these Congolese parties.
    terms = {t for c in m.CALLS if c[0] == "gnews" for t in c[2]}
    assert {"SULTANI MAKENGA", "EMMANUEL SULTANI MAKENGA", "OB0243318", "ADF"} <= terms
    assert ("gnews", "fr", ("ADF",), ("Congo",)) in m.CALLS
    assert {c[1] for c in m.CALLS if c[0] == "gnews"} == {"en-US", "fr"}
    assert any(c[0] == "gkg" for c in m.CALLS)
    cov = (tmp_path / "data/coverage.csv").read_text()
    assert cov.count("searched") == 3
    rep1 = next((tmp_path / "reports").glob("*.md")).read_text()
    assert "ADF rebels kill 12" in rep1 and "joint drills" not in rep1   # acronym needs Congo context
    assert (tmp_path / "data/alerts").exists()
    first = list((tmp_path / "reports").glob("*.md"))
    assert len(first) == 1 and "Makenga in Goma" in first[0].read_text()
    assert "Changes to the UN Consolidated List" not in first[0].read_text()
    assert (tmp_path / "data/un_consolidated_list.csv").exists()
    first[0].unlink()

    # Week 2: same article again (must NOT re-alert) + list change + new report + new xref + feed item.
    xml2 = SAMPLE.replace(b"<LISTED_ON>2005-11-01</LISTED_ON>", b"<LISTED_ON>2005-11-01</LISTED_ON><COMMENTS1>Released.</COMMENTS1>")
    feed = [Hit("Feed: SC", "Committee amends entry of Sultani Makenga", "https://un.example/f1",
                snippet="The Committee amended the entry of Sultani Makenga, M23 sanctions.")]
    m, args = _stub(monkeypatch, tmp_path, xml2, news, feed_items=feed,
                    reports=report_link + [("S/2026/950", "https://example.org/r2.pdf")],
                    report_text="Panel of Experts: the Allied Democratic Forces expanded operations.",
                    xref={"CDi.008": ["us_ofac_sdn", "gb_hmt_sanctions"]})
    assert m.main(args) == 0
    md = next((tmp_path / "reports").glob("*.md")).read_text()
    assert "Amended" in md and "CDi.005" in md
    assert "S/2026/950" in md and "S/2026/900" not in md        # only the NEW report
    assert "gb_hmt_sanctions" in md
    assert "Committee amends entry" not in md   # first week a feed returns items = baseline only
    assert "Makenga in Goma talks" not in md                      # already reported last week
    gk = [c for c in m.CALLS if c[0] == "gkg"][0]
    assert [list(x) for x in gk[2]] == [["english", "20261005000000"]]   # failed GDELT file retried
    (tmp_path / "reports" / next((tmp_path / "reports").glob("*.md")).name).unlink()

    # Week 3: nothing new anywhere -> no report written at all.
    m, args = _stub(monkeypatch, tmp_path, xml2, news, feed_items=feed,
                    reports=report_link + [("S/2026/950", "https://example.org/r2.pdf")],
                    xref={"CDi.008": ["us_ofac_sdn", "gb_hmt_sanctions"]})
    assert m.main(args) == 0
    assert not list((tmp_path / "reports").glob("*.md"))


def test_time_budget_defers_without_gaps(monkeypatch, tmp_path):
    m, args = _stub(monkeypatch, tmp_path, SAMPLE, [], extra_cfg="\nmax_sweep_minutes: 0\n")
    assert m.main(args) == 0
    cov = (tmp_path / "data/coverage.csv").read_text()
    assert cov.count("deferred") == 3
    from monitor.state import State
    assert State(tmp_path / "state/monitor.db").get_meta("last_searched", {}) == {}   # retried first next run
