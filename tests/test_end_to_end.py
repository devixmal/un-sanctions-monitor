"""Two simulated weekly runs with all network calls stubbed."""
import importlib

from monitor import sources
from monitor.sources import Hit
from tests.test_monitor import SAMPLE


def _stub(monkeypatch, tmp_path, xml, news, feed_items=(), reports=(), report_text="", xref=None):
    monkeypatch.setenv("MONITOR_ROOT", str(tmp_path))
    for k in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "SMTP_HOST", "SLACK_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    import monitor.main as m
    m = importlib.reload(m)
    monkeypatch.setattr(m.unlist, "fetch_xml", lambda url: xml)
    monkeypatch.setattr(sources, "gdelt", lambda names, days, n=75: [h for h in news if h.source == "GDELT"])
    monkeypatch.setattr(sources, "google_news", lambda names, days, eds: [])
    monkeypatch.setattr(sources, "read_feed", lambda url: list(feed_items))
    monkeypatch.setattr(sources, "list_report_links", lambda page: list(reports))
    monkeypatch.setattr(sources, "document_text", lambda url: report_text)
    monkeypatch.setattr(sources, "opensanctions_datasets", lambda url, recs: xref or {})
    monkeypatch.setattr(sources, "page_text", lambda url: "Rebel commander Sultani Makenga of the M23 was seen in Goma, DRC.")
    monkeypatch.setattr(sources, "polite_sleep", lambda s: None)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(open("config.yaml").read())
    return m, ["--config", str(cfg)]


def test_two_runs(monkeypatch, tmp_path):
    report_link = [("S/2026/900", "https://example.org/r.pdf")]
    # Week 1: baseline. News exists, so it IS alerted (real recent activity), list is baseline only.
    news = [Hit("GDELT", "M23 leader Makenga in Goma talks", "https://news.example/a?utm_source=x", "20261005")]
    m, args = _stub(monkeypatch, tmp_path, SAMPLE, news, reports=report_link,
                    xref={"CDi.008": ["us_ofac_sdn"]})
    assert m.main(args) == 0
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
    (tmp_path / "reports" / next((tmp_path / "reports").glob("*.md")).name).unlink()

    # Week 3: nothing new anywhere -> no report written at all.
    m, args = _stub(monkeypatch, tmp_path, xml2, news, feed_items=feed,
                    reports=report_link + [("S/2026/950", "https://example.org/r2.pdf")],
                    xref={"CDi.008": ["us_ofac_sdn", "gb_hmt_sanctions"]})
    assert m.main(args) == 0
    assert not list((tmp_path / "reports").glob("*.md"))
