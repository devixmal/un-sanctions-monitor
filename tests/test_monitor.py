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
    assert not st.is_seen(k)          # nothing persisted until commit (after alerts are sent)
    st.commit()
    assert st.is_seen(k)

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
    assert len(names) == 5                                   # primary + all 4 aliases, over 2 queries
    nick = [q for q in qs if q.kind == "nicknames"]
    assert nick and "ABU WAQAS" in nick[0].terms and "ADF" in nick[0].context and "Congo" in nick[0].context
    assert "Tanzania" in nick[0].context
    assert identifiers(rec) == ["AB850901", "AB187304"]
    assert "JUNDI" in nickname_terms(rec)


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
