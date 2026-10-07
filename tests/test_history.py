"""History database, review checklists and exports."""
import json

from monitor import review, unlist
from monitor.alerts import Report
from monitor.history import History, un_status_flags
from monitor.sources import Hit
from tests.test_monitor import SAMPLE


def _report(n_news=7):
    recs = unlist.parse(SAMPLE)
    rep = Report(run_date="2026-10-07")
    hits = [Hit("GDELT", f"Makenga story {i}", f"https://n.example/{i}", "20261001120000", "n.example",
                score=0.9 - i * 0.05, summary=f"s{i}", evidence=["topic:TERROR"], locations="Congo")
            for i in range(n_news)]
    rep.news["CDi.008"] = (recs["CDi.008"], hits)
    rep.list_added.append(recs["CDi.005"])
    rep.official_mentions.append(("US Treasury press releases", "Treasury sanctions ADF financiers",
                                  "https://t.example/sb1", recs["CDe.001"], [("allied democratic forces", "…")]))
    return recs, rep


def test_add_report_and_baseline_limits(tmp_path):
    recs, rep = _report()
    h = History(tmp_path / "h.db")
    ids = h.add_report(rep, "2026-10-07", mode="baseline", top_individual=5, top_entity=3)
    news = h.rows("kind='news'")
    assert len(news) == 7
    assert sum(r["status"] == "pending" for r in news) == 5          # top 5 to review
    assert sum(r["status"] == "unreviewed" for r in news) == 2       # rest searchable only
    assert len(ids) == 5 + 2                                         # + list addition + official release
    # a re-run finding the same items does not reset reviewed ones
    h.review(ids[0], "verified")
    ids2 = h.add_report(rep, "2026-10-14")
    assert ids[0] not in ids2


def test_review_parse_and_sync(tmp_path, monkeypatch):
    recs, rep = _report(3)
    h = History(tmp_path / "h.db")
    ids = h.add_report(rep, "2026-10-07")
    body = review.render_items(h.rows("status='pending'"), recs)
    assert body.count("- [ ]") == len(ids) and all(f"<!-- id:{i} -->" in body for i in ids)
    ticked = body.replace("- [ ]", "- [x]", 2)               # reviewer ticks the first two
    parsed = review.parse(ticked)
    assert sum(parsed.values()) == 2 and len(parsed) == len(ids)

    class R:
        def __init__(self, data):
            self.data = data
        def raise_for_status(self):
            pass
        def json(self):
            return self.data
    pages = {1: [{"number": 5, "state": "closed", "body": ticked}], 2: []}
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(review.requests, "get", lambda url, headers, timeout, params: R(pages[params["page"]]))
    counts = review.sync(h)
    assert counts["verified"] == 2 and counts["rejected"] == len(ids) - 2
    assert review.sync(h) == {"verified": 0, "rejected": 0, "pending": 0}   # idempotent


def test_create_issues_splits_and_links(tmp_path, monkeypatch):
    recs, rep = _report(3)
    h = History(tmp_path / "h.db")
    ids = h.add_report(rep, "2026-10-07")
    posted = []

    class R:
        status_code = 201
        def __init__(self, n):
            self.n = n
        def raise_for_status(self):
            pass
        def json(self):
            return {"number": self.n, "html_url": f"https://github.com/o/r/issues/{self.n}"}
    def fake_post(url, headers, timeout, json):
        posted.append(json)
        return R(len(posted))
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(review.requests, "post", fake_post)
    monkeypatch.setattr(review, "PER_ISSUE", 2)
    urls = review.create_issues(h, ids, recs, "2026-10-07", "headline", "reports/2026-10-07.md", "baseline")
    assert len(urls) == len(posted) >= 2
    assert all(p["labels"] == ["sanctions-review"] for p in posted)
    assert posted[0]["title"].startswith("Baseline status review 2026-10-07 (1/")
    assert {r["issue"] for r in h.rows("status='pending'")} <= {1, 2, 3}


def test_exports(tmp_path):
    recs, rep = _report(2)
    h = History(tmp_path / "h.db")
    ids = h.add_report(rep, "2026-10-07")
    h.review(ids[-1], "verified")
    h.record_check("CDi.008", "2026-10-07", "2026-07-09", 2, "baseline")
    h.commit()
    h.export(tmp_path, recs, {"CDi.008": ["us_ofac_sdn"]}, {"last_run": "2026-10-07"})
    profiles = list((tmp_path / "data/profiles").glob("*.md"))
    assert len(profiles) == len(recs) + 1                              # one per party + index
    p = (tmp_path / "data/profiles/CDi.008.md").read_text()
    assert "Last checked: 2026-10-07" in p and "us_ofac_sdn" in p
    d = json.loads((tmp_path / "data/dashboard.json").read_text())
    assert len(d["parties"]) == len(recs) and {f["status"] for f in d["findings"]} >= {"verified", "pending"}
    assert (tmp_path / "data/history.csv").read_text().count("\n") == 2   # header + 1 verified


def test_un_status_flags():
    assert un_status_flags({"comments": "Reported to have died in prison in Germany"}) == ["deceased", "detained"]
    assert un_status_flags({"comments": "Released in 2015"}) == ["released"]
