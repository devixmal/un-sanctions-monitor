"""Offline tests: parsing, diffing, matching, filtering and report rendering."""
from pathlib import Path

from monitor import unlist
from monitor.alerts import Report, render_markdown
from monitor.matching import NameIndex, display_name, find_mentions, search_names
from monitor.scoring import heuristic
from monitor.sources import Hit
from monitor.state import State, canonical_url, key_of

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<CONSOLIDATED_LIST dateGenerated="2026-10-03T23:00:03.428Z">
 <INDIVIDUALS>
  <INDIVIDUAL>
   <DATAID>1</DATAID><FIRST_NAME>SULTANI</FIRST_NAME><SECOND_NAME>MAKENGA</SECOND_NAME>
   <UN_LIST_TYPE>DRC</UN_LIST_TYPE><REFERENCE_NUMBER>CDi.008</REFERENCE_NUMBER>
   <LISTED_ON>2012-11-12</LISTED_ON>
   <COMMENTS1>A military leader of the Mouvement du 23 Mars (M23).</COMMENTS1>
   <NATIONALITY><VALUE>Democratic Republic of the Congo</VALUE></NATIONALITY>
   <LAST_DAY_UPDATED><VALUE/></LAST_DAY_UPDATED>
   <INDIVIDUAL_ALIAS><QUALITY>Good</QUALITY><ALIAS_NAME>MAKENGA, Colonel SULTANI</ALIAS_NAME></INDIVIDUAL_ALIAS>
   <INDIVIDUAL_ALIAS><QUALITY>Good</QUALITY><ALIAS_NAME>MAKENGA, EMMANUEL SULTANI</ALIAS_NAME></INDIVIDUAL_ALIAS>
   <INDIVIDUAL_ALIAS><QUALITY>Low</QUALITY><ALIAS_NAME>Musa</ALIAS_NAME></INDIVIDUAL_ALIAS>
   <INDIVIDUAL_ADDRESS><COUNTRY>Uganda</COUNTRY></INDIVIDUAL_ADDRESS>
   <INDIVIDUAL_DATE_OF_BIRTH><TYPE_OF_DATE>EXACT</TYPE_OF_DATE><DATE>1973-12-25</DATE></INDIVIDUAL_DATE_OF_BIRTH>
   <INDIVIDUAL_DOCUMENT/>
  </INDIVIDUAL>
  <INDIVIDUAL>
   <DATAID>2</DATAID><FIRST_NAME>JÉRÔME</FIRST_NAME><SECOND_NAME>KAKWAVU BUKANDE</SECOND_NAME>
   <UN_LIST_TYPE>DRC</UN_LIST_TYPE><REFERENCE_NUMBER>CDi.005</REFERENCE_NUMBER>
   <LISTED_ON>2005-11-01</LISTED_ON>
   <INDIVIDUAL_ALIAS><QUALITY>Good</QUALITY><ALIAS_NAME>Jérôme Kakwavu</ALIAS_NAME></INDIVIDUAL_ALIAS>
   <INDIVIDUAL_DATE_OF_BIRTH><TYPE_OF_DATE>BETWEEN</TYPE_OF_DATE><FROM_YEAR>1973</FROM_YEAR><TO_YEAR>1974</TO_YEAR></INDIVIDUAL_DATE_OF_BIRTH>
   <INDIVIDUAL_DOCUMENT><TYPE_OF_DOCUMENT>Passport</TYPE_OF_DOCUMENT><NUMBER>OB 0243318</NUMBER></INDIVIDUAL_DOCUMENT>
  </INDIVIDUAL>
 </INDIVIDUALS>
 <ENTITIES>
  <ENTITY>
   <DATAID>3</DATAID><FIRST_NAME>ALLIED DEMOCRATIC FORCES</FIRST_NAME>
   <UN_LIST_TYPE>DRC</UN_LIST_TYPE><REFERENCE_NUMBER>CDe.001</REFERENCE_NUMBER>
   <ENTITY_ALIAS><QUALITY>Good</QUALITY><ALIAS_NAME>ADF</ALIAS_NAME></ENTITY_ALIAS>
   <ENTITY_ADDRESS><COUNTRY>Democratic Republic of the Congo</COUNTRY></ENTITY_ADDRESS>
  </ENTITY>
 </ENTITIES>
</CONSOLIDATED_LIST>""".encode("utf-8")


def recs():
    return unlist.parse(SAMPLE)


def test_parse():
    r = recs()
    assert set(r) == {"CDi.008", "CDi.005", "CDe.001"}
    m = r["CDi.008"]
    assert m["name"] == "SULTANI MAKENGA" and m["low_aliases"] == ["Musa"]
    assert m["countries"] == ["Democratic Republic of the Congo", "Uganda"]
    assert r["CDi.005"]["dobs"] == ["1973-1974"]
    assert r["CDe.001"]["kind"] == "entity"


def test_diff():
    old = recs()
    new = recs()
    new.pop("CDi.005")
    new["CDi.008"] = {**new["CDi.008"], "comments": "Now in Goma.", "hash": "changed"}
    new["XXi.001"] = {**old["CDi.005"], "ref": "XXi.001"}
    d = unlist.diff(old, new)
    assert [r["ref"] for r in d["added"]] == ["XXi.001"]
    assert [r["ref"] for r in d["removed"]] == ["CDi.005"]
    assert d["amended"][0]["changed_fields"] == ["comments"]


def test_names_and_matching():
    r = recs()
    assert display_name("MAKENGA, Colonel SULTANI") == "Colonel SULTANI MAKENGA"
    names = search_names(r["CDi.008"])
    assert names[0] == "SULTANI MAKENGA" and "EMMANUEL SULTANI MAKENGA" in names
    assert "Musa" not in names                       # low-quality single word excluded
    assert search_names(r["CDe.001"]) == ["ALLIED DEMOCRATIC FORCES"]  # 'ADF' too short
    assert find_mentions("Rebel chief Sultani Makenga said...", names) == ["sultani makenga"]
    assert find_mentions("Jerome Kakwavu was released", search_names(r["CDi.005"]))  # accent folding
    assert not find_mentions("Sultani Makengaville", names)


def test_name_index_scan():
    idx = NameIndex(recs())
    hits = idx.scan("The Panel notes that Emmanuel Sultani Makenga met officials. The Allied Democratic Forces attacked.")
    assert set(hits) == {"CDi.008", "CDe.001"}


def test_heuristic_filter():
    rec = recs()["CDi.008"]
    good = Hit("GDELT", "M23 commander Sultani Makenga seen in Goma", "https://a.example/1",
               snippet="sultani makenga the m23 rebel commander", matched=["sultani makenga"])
    noise = Hit("GDELT", "Weather today", "https://a.example/2", snippet="", matched=[])
    weak = Hit("GDELT", "Sultani Makenga wins local chess cup", "https://a.example/3",
               snippet="sultani makenga won the cup", matched=["sultani makenga"])
    kept, near = heuristic(rec, [good, noise, weak])
    assert kept == [good] and good.score > 0.5
    assert near == [weak]                     # shown as borderline, not silently dropped


def test_state_and_render(tmp_path: Path):
    st = State(tmp_path / "s.db")
    k = key_of("x", canonical_url("https://www.example.com/a/?utm_source=x&id=2#frag"))
    assert canonical_url("https://www.example.com/a/?utm_source=x&id=2#frag") == "https://example.com/a?id=2"
    st.mark(k, "gdelt")
    assert st.is_seen(k)              # visible within the run (no duplicates across sources)
    assert not State(tmp_path / "s.db").is_seen(k)   # but persisted only at commit (after alerts)
    st.commit()
    assert State(tmp_path / "s.db").is_seen(k)

    r = recs()
    rep = Report(run_date="2026-10-12")
    assert rep.count == 0             # quiet week -> nothing sent
    rep.list_added.append(r["CDi.005"])
    h = Hit("GDELT", "Makenga in Goma", "https://a.example/1", "20261010", "a.example", score=0.9,
            summary="Reported in Goma.", category="location")
    rep.news["CDi.008"] = (r["CDi.008"], [h])
    md = render_markdown(rep)
    assert rep.count == 2
    assert "Added" in md and "Makenga in Goma" in md and "[location]" in md


def test_query_plan_covers_everything():
    from monitor.matching import identifiers, nickname_terms, plan_queries
    rec = {
        "ref": "CDi.040", "kind": "individual", "name": "AHMAD MAHMOOD HASSAN",
        "aliases": ["AHMED MAHAMUD HASSAN ALIYANI", "AHMAD MAHMOUD HASSAN", "AHMAD MAHAMOOD HASSAN",
                    "AHMED MAHMOUD HASSAN"],
        "low_aliases": ["ABU WAQAS", "SAINT JOYAGE", "JUNDI", "MARABOU", "LEBLANC"],
        "designation": ["Senior leader of the Allied Democratic Forces (ADF) (CDe.001)"],
        "comments": "", "nationality": ["United Republic of Tanzania"],
        "countries": ["Democratic Republic of the Congo"], "doc_numbers": ["AB850901", "AB187304 "],
    }
    qs = plan_queries(rec)
    names = [t for q in qs if q.kind == "names" for t in q.terms]
    # every distinctive variant is searched; "AHMAD MAHMOUD HASSAN" / "AHMED MAHMOUD HASSAN" are made
    # only of very common given names, so on their own they would match thousands of people
    assert names == ["AHMAD MAHMOOD HASSAN", "AHMED MAHAMUD HASSAN ALIYANI", "AHMAD MAHAMOOD HASSAN"]
    nick = {q.terms[0]: q.context for q in qs if q.kind == "nicknames"}
    assert "ADF" in nick["ABU WAQAS"] and "Congo" in nick["ABU WAQAS"] and "Tanzania" in nick["ABU WAQAS"]
    assert identifiers(rec) == ["AB850901", "AB187304"]
    assert "MARABOU" in nickname_terms(rec) and "JUNDI" not in nickname_terms(rec)  # 5-letter word: too common
    jer = {"kind": "individual", "aliases": [], "low_aliases": ["Commandant Jérôme", "Mr Omari", "Omari"]}
    assert nickname_terms(jer) == ["Commandant Jérôme"]          # title kept: bare "Jérôme" is too common


def test_local_editions():
    import yaml
    from monitor.main import editions_for
    gn = yaml.safe_load(open("config.yaml"))["sources"]["google_news"]
    drc = {"countries": ["Democratic Republic of the Congo"], "nationality": []}
    dprk = {"countries": [], "nationality": ["Democratic People's Republic of Korea"]}
    assert [e["hl"] for e in editions_for(drc, gn)] == ["en-US", "fr"]
    assert [e["hl"] for e in editions_for(dprk, gn)] == ["en-US", "ko"]
    assert [e["hl"] for e in editions_for({"countries": ["Nigeria"], "nationality": []}, gn)] == ["en-US"]


def test_identifier_matching():
    from monitor.matching import find_identifiers
    assert find_identifiers("passport no. OB-0243318 was used", ["OB0243318"]) == ["OB0243318"]
    assert find_identifiers("vessel (IMO: 9118135) loaded coal", ["IMO 9118135"]) == ["IMO 9118135"]


def test_gkg_file_processing(monkeypatch):
    import io
    import zipfile

    import requests

    from monitor import gkg
    from monitor.matching import context_terms, fold, nickname_terms, search_names

    recs = unlist.parse(SAMPLE)
    names, nicks = {}, {}
    for ref, rec in recs.items():
        for n in search_names(rec, limit=100):
            names.setdefault(fold(n), []).append(ref)
        for n in nickname_terms(rec, 50):
            nicks.setdefault(fold(n), []).append((ref, tuple(fold(c) for c in context_terms(rec))))
    gkg._init(names, nicks)

    def row(url, allnames, locs="", title="T"):
        c = [""] * 27
        c[1], c[3], c[4], c[9], c[23] = "20261005120000", "news.example", url, locs, allnames
        c[26] = f"<PAGE_TITLE>{title}</PAGE_TITLE>"
        return "\t".join(c)

    body = "\n".join([
        row("https://n.example/1", "General Sultani Makenga,10;Goma,40", title="M23 chief speaks"),
        row("https://n.example/2", "ADF,5", "1#Democratic Republic of the Congo#CG#"),
        row("https://n.example/3", "ADF,5", "1#Australia#AS#"),          # ADF = Australian forces
        row("https://n.example/4", "Weather,1"),
    ])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.gkg.csv", body)

    class R:
        status_code, content = 200, buf.getvalue()
    monkeypatch.setattr(requests, "get", lambda url, timeout=0: R())
    stream, ts, rows, out = gkg.process_file("english", "20261005120000")
    assert rows == 4
    got = {(o["ref"], o["url"]) for o in out}
    assert got == {("CDi.008", "https://n.example/1"), ("CDe.001", "https://n.example/2")}
    assert [o["title"] for o in out if o["ref"] == "CDi.008"] == ["M23 chief speaks"]


def test_ambiguous_names_need_independent_context():
    from monitor.matching import context_for, is_common_name, is_generic_name, is_thin_name, search_names
    assert is_common_name("Abdul Rahman") and is_common_name("George") and not is_common_name("Makenga")
    assert is_generic_name("MINISTRY OF NATIONAL DEFENCE") and not is_generic_name("THE HOUTHIS")
    assert is_thin_name("Abu Anas") and is_thin_name("Matiur Rahman") and not is_thin_name("Lova Madayev")
    rec = {"kind": "entity", "name": "MINISTRY OF NATIONAL DEFENCE", "aliases": ["MINISTRY OF THE PEOPLE'S ARMED FORCES (MPAF)"]}
    assert search_names(rec) == ["MINISTRY OF THE PEOPLE'S ARMED FORCES (MPAF)"]
    # a name cannot corroborate itself
    assert context_for("Matiur Rahman", ["matiur", "ustad", "Pakistan"]) == ["ustad", "Pakistan"]


def test_google_news_pauses_once_then_stops(monkeypatch):
    from datetime import datetime, timezone

    from monitor import sources

    calls = []

    class R:
        status_code, content, text = 503, b"", ""
    monkeypatch.setattr(sources, "get", lambda *a, **k: calls.append(1) or R())
    monkeypatch.setattr(sources.time, "sleep", lambda s: calls.append(("sleep", s)))
    monkeypatch.setattr(sources, "GNEWS_LIMIT", sources.RateLimiter(0))
    monkeypatch.setitem(sources.GNEWS_BLOCK, "paused", False)
    monkeypatch.setattr(sources, "STATS", sources.SourceStats())
    ed = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert sources.google_news(["X Y"], t, t, ed) is None
    assert ("sleep", 900) in calls and sources.STATS.is_tripped("google_news")
    assert sources.google_news(["X Y"], t, t, ed) is None and calls.count(1) == 2   # no more requests


def test_relevance_gate():
    import yaml

    from monitor.relevance import Relevance
    r = Relevance(yaml.safe_load(open("config.yaml"))["relevance"])
    assert r.check("Les FDLR ont commis plusieurs exactions", "")[0]
    assert r.check("x", "", "WB_2433_CONFLICT_AND_VIOLENCE;TAX_TERROR_GROUP")[0]   # GDELT topic codes
    assert r.check("العقوبات الأمريكية على الحوثي", "")[0]                          # Arabic, attached prefix
    assert not r.check("Ty Jerome, nouveau leader des Grizzlies", "")[0]              # sports namesake
    assert not r.check("Neil Armstrong anniversary", "")[0]                           # 'arms' is whole-word only
    # prominence: headline or early mention
    assert r.prominent("Houthis fire missile", "", ["The Houthis", "Houthis"], None)
    assert r.prominent("Regional roundup", "", ["Houthis"], 300)
    assert not r.prominent("Regional roundup", "x " * 3000 + "Houthis", ["Houthis"], None)
