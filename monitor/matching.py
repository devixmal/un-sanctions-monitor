"""Name normalisation, search-term selection and in-text matching."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Prefixes that appear in UN aliases but aren't part of how media write names.
_TITLES = {
    "colonel", "col", "general", "gen", "major", "brigadier", "lieutenant", "lt", "commander",
    "captain", "capt", "dr", "doctor", "sheikh", "shaykh", "mullah", "maulvi", "mawlawi", "haji",
    "hajji", "mr", "mrs", "professor", "prof", "emir", "amir", "qari", "commandant",
}

# Generic words that make a short entity alias useless as a search term.
_GENERIC = {"group", "company", "limited", "ltd", "corporation", "trading", "general", "bank",
            "foundation", "organization", "organisation", "front", "army", "brigade", "movement"}


# Given names and name particles so common that, on their own, they identify no one.
_COMMON_GIVEN = set("""
muhammad mohammad mohammed mohamed mohamad muhammed mohd ahmad ahmed ahmet ali hassan hasan hussein
husain hussain husayn abdullah abdallah abdulla abdelrahman abdurrahman abdulrahman rahman rehman
abdel abdul abd abu ibn bin bint al el ul ur ud din uddin karim kareem hadi samad aziz ibrahim
ibraheem ismail ismael omar umar othman osman usman uthman khalid khaled said saeed sayed sayyid
salem salim yusuf yousef yousif youssef musa moussa isa issa mustafa mostafa mahmoud mahmud hamid
hameed hamed rashid rasheed jamal nasser nasir naser malik amir khan aslam akbar ashraf asad
hafiz hafez zaid zayd yasin yassin yahya idris tariq tarek faisal fahad fahd saleh salih sultan
nur noor shah mir sheikh syed ibrahim jalal kamal farouk farooq faruk anwar bakr bakar haji
muslim imam mullah maulana qari george arthur john james michael michel david peter paul joseph
jean pierre marie andre daniel thomas charles william robert richard emmanuel innocent victor
mighty kim lee park choi jong sung il yong chol nam ri pak han
zafar iqbal javed javaid shahid imran irfan nawaz shafiq rafiq akhtar arshad asif abbas raza rizwan
saleem waheed zahid bilal hamza usama osama salman sami nadeem naeem qasim kashif hafeez majid
sajid waseem wasim yasir yaser younis younus yunus zubair haq ullah gul jan noor zaman rahim
abdulrahman abdulaziz abdulkarim abdurahman abdirahman abdi mohamud
mohamoud ahmedi hasani husseini sadiq siddiq siddique hakim hakeem latif nabi rasul habib
""".split())
# Short English words that look like acronyms in upper-case text but match everything.
_ENGLISH_SHORT = set("""
mid man war new all can use act aid may red sun air arm art bad big box car day end far few fit
fun god gun hot law low map oil one own pay per put run say see set sir six ten top two way win
yes yet age ago bit cut die dog eat eye fly job key kid lot off old out sea sit son tax tea tie
try via sin are was has had his her him its our who why how not but any its led
""".split())
_GENERIC_ORG = set("""
the of and for in on at to de la le les des du et y a an
ministry ministere department departement office bureau directorate agency authority administration
committee council commission service services national state public people peoples popular general
central first second third defence defense security military army armed forces force guard guards
navy naval air airforce munitions munition industry industries industrial factory factories works
academy natural science sciences institute institution research development technology technical
technologies engineering center centre university college school bank banking finance financial
investment investments trading trade commercial commerce company companies corporation corp co
ltd limited inc group holding holdings enterprise enterprises international global import export
shipping maritime marine ocean sea transport logistics airlines airline aviation construction
energy oil petroleum gas mining minerals mineral gold metal metals steel chemical chemicals
equipment machinery materials material products product supply supplies network association union
federation foundation organization organisation society charity relief humanitarian welfare
islamic democratic republic revolutionary liberation movement front party brigade brigades
battalion unit units regiment division command operations operation special branch machine
machines alliance armee forces mouvement parti populaire nationale paix pour tous developpement
democratiques democratique liberation ansar
""".split())


def fold(s: str) -> str:
    """Lower-case, strip accents and punctuation, collapse whitespace."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s.lower(), flags=re.UNICODE)
    return " ".join(s.split())


def display_name(n: str) -> str:
    """'MAKENGA, Colonel SULTANI' -> 'Colonel SULTANI MAKENGA'; strips quote marks."""
    n = n.strip().strip("\"'“”‘’ ")
    if n.count(",") == 1:
        last, first = (p.strip() for p in n.split(","))
        if first and last:
            n = f"{first} {last}"
    return " ".join(n.split())


def _strip_titles(n: str) -> str:
    words = n.split()
    while words and fold(words[0]) in _TITLES:
        words = words[1:]
    return " ".join(words)


def is_searchable(n: str, kind: str, min_len: int) -> bool:
    f = fold(n)
    toks = f.split()
    if not toks or len(toks) > 12 or re.search(r"\d{5,}", f):   # UN alias fields sometimes hold notes
        return False
    if kind == "entity":
        meaningful = [t for t in toks if t not in _GENERIC]
        return len(f) >= min_len and bool(meaningful)
    # Individuals: two+ tokens, or a single unusually long token (e.g. a unique surname-alias).
    return (len(toks) >= 2 and len(f) >= min_len) or (len(toks) == 1 and len(f) >= 9)


def is_common_name(n: str) -> bool:
    """'Abdul Rahman', 'Hassan', 'George' — identifies no one on its own."""
    toks = fold(n).split()
    return bool(toks) and all(t in _COMMON_GIVEN or t in _TITLES for t in toks)


def is_thin_name(n: str) -> bool:
    """Names shared by many real people, searched only with context: one short distinctive word
    ('Abu Anas', 'Abd al-Muhsin', 'Matiur Rahman', 'Kim Kwang Il') or a single word ('Hidayatullah')."""
    toks = fold(n).split()
    rest = [t for t in toks if t not in _COMMON_GIVEN and t not in _TITLES]
    # 4+ word full names ("Hamza Usama Muhammad bin Laden") are specific enough on their own.
    return len(toks) == 1 or (len(rest) == 1 and len(rest[0]) < 7 and 1 < len(toks) <= 3)


def is_generic_name(n: str) -> bool:
    """'Ministry of National Defence', 'Second Academy of Natural Sciences' — needs context."""
    toks = fold(n).split()
    return bool(toks) and all(t in _GENERIC_ORG for t in toks)


def search_names(rec: dict, min_len: int = 8, limit: int = 4, include_low: bool = False) -> list[str]:
    """Distinctive names: primary name + good-quality aliases, de-duplicated, best first.

    Names made only of common given names or generic institutional words are excluded here;
    generic ones are searched with context via nickname_terms(), common ones are not searchable.
    """
    raw = [rec["name"], *rec.get("aliases", [])]
    if include_low:
        raw += rec.get("low_aliases", [])
    out, seen = [], set()
    for n in raw:
        n = _strip_titles(display_name(n))
        key = fold(n)
        if (key and key not in seen and is_searchable(n, rec["kind"], min_len)
                and not is_common_name(n) and not is_generic_name(n)
                and not (rec["kind"] == "individual" and is_thin_name(n))):
            seen.add(key)
            out.append(n)
    return out[:limit]


def all_match_names(rec: dict, min_len: int = 8) -> list[str]:
    """Every name variant usable for matching text (wider than the query list)."""
    names = search_names(rec, min_len=min_len, limit=50)
    if rec.get("original_script"):
        names.append(rec["original_script"])
    return names


def _pattern(names: list[str]) -> re.Pattern | None:
    folded = sorted({fold(n) for n in names if fold(n)}, key=len, reverse=True)
    if not folded:
        return None
    alts = "|".join(r"\s+".join(map(re.escape, f.split())) for f in folded)
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)")


def find_mentions(text: str, names: list[str]) -> list[str]:
    """Return the folded name variants that appear as whole words in text."""
    pat = _pattern(names)
    if not pat or not text:
        return []
    return sorted({" ".join(m.group(0).split()) for m in pat.finditer(fold(text))})


def context_snippet(text: str, names: list[str], width: int = 450) -> str:
    """Folded text window around the first mention (good enough for review/LLM)."""
    pat = _pattern(names)
    ft = fold(text)
    m = pat.search(ft) if pat else None
    if not m:
        return ""
    a, b = max(0, m.start() - width), min(len(ft), m.end() + width)
    return ("…" if a else "") + ft[a:b] + ("…" if b < len(ft) else "")


class NameIndex:
    """Fast lookup of which listed parties are mentioned in a long document."""

    def __init__(self, records: dict[str, dict], min_len: int = 8):
        self.by_name: dict[str, set[str]] = {}
        for ref, rec in records.items():
            for n in all_match_names(rec, min_len):
                f = fold(n)
                if f:
                    self.by_name.setdefault(f, set()).add(ref)
        names = sorted(self.by_name, key=len, reverse=True)
        self._chunks = [names[i:i + 400] for i in range(0, len(names), 400)]
        self._pats = [
            re.compile(r"(?<!\w)(?:" + "|".join(r"\s+".join(map(re.escape, n.split())) for n in chunk) + r")(?!\w)")
            for chunk in self._chunks
        ]

    def scan(self, text: str, width: int = 400) -> dict[str, list[tuple[str, str]]]:
        """{ref: [(matched_name, snippet), ...]} for every listed party found in text."""
        ft = fold(text)
        hits: dict[str, list[tuple[str, str]]] = {}
        for pat in self._pats:
            for m in pat.finditer(ft):
                name = " ".join(m.group(0).split())
                a, b = max(0, m.start() - width), min(len(ft), m.end() + width)
                for ref in self.by_name.get(name, ()):
                    if len(hits.setdefault(ref, [])) < 3:
                        hits[ref].append((name, ft[a:b]))
        return hits


# ============================================================ query planning
# Long official country names -> how the press writes them (used as context terms).
_SHORT_COUNTRY = {
    "democratic republic of the congo": "Congo",
    "democratic people s republic of korea": "North Korea",
    "korea democratic people s republic of": "North Korea",
    "syrian arab republic": "Syria",
    "iran islamic republic of": "Iran",
    "united republic of tanzania": "Tanzania",
    "russian federation": "Russia",
    "republic of korea": "South Korea",
    "lao people s democratic republic": "Laos",
    "viet nam": "Vietnam",
    "bolivarian republic of venezuela": "Venezuela",
    "central african republic": "Central African Republic",
    "united kingdom of great britain and northern ireland": "United Kingdom",
    "united states of america": "United States",
}
_NOT_CONTEXT = {"UN", "UNSC", "UNSCR", "INTERPOL", "USD", "HQ", "ID", "AKA", "NO", "DOB", "QDE", "QDI",
                "CDE", "CDI", "THE", "AND", "FOR", "LTD", "CO", "II", "III", "IV", "PO", "BOX"}
# Low-quality aliases that are just ranks/titles/common words: useless even with context.
_WEAK_ALIAS = _TITLES | {"chairman", "boss", "chief", "the", "sheikh", "abu", "ibn", "bin", "al", "mr",
                         "romeo", "lydia", "terminator", "defender", "punisher", "israel", "omari",
                         "musa", "major", "tango", "alpha", "doctor"}


def short_country(c: str) -> str:
    f = fold(c)
    return _SHORT_COUNTRY.get(f, re.sub(r"\s*\(.*?\)", "", c).strip())


def context_terms(rec: dict, limit: int = 6) -> list[str]:
    """Terms that tie an ambiguous name to this party.

    Organisation acronyms (M23, FDLR, AQAP…), distinctive words from the party's other names
    (e.g. a rare surname), and — for organisations only — their countries. Countries are too broad
    to corroborate a person's nickname (half of all news mentions "Pakistan" or "Iraq").
    """
    text = " ".join([*rec.get("designation", []), rec.get("comments", "")])
    acronyms = []
    for a in re.findall(r"\b[A-Z][A-Z0-9]{2,7}\b", text):
        if (a not in _NOT_CONTEXT and fold(a) not in _ENGLISH_SHORT and any(c.isalpha() for c in a)
                and a not in acronyms):
            acronyms.append(a)
    distinctive = []
    for n in [rec["name"], *rec.get("aliases", [])]:
        for t in fold(_strip_titles(display_name(n))).split():
            if (len(t) >= 5 and t not in _COMMON_GIVEN and t not in _GENERIC_ORG and t not in _TITLES
                    and t not in distinctive):
                distinctive.append(t)
    countries = []
    for c in [*rec.get("nationality", []), *rec.get("countries", [])]:
        s = short_country(c)
        if s and s not in countries:
            countries.append(s)
    return (acronyms[:3] + distinctive[:3] + countries[:2])[:limit]


def context_for(name: str, ctx: list[str]) -> list[str]:
    """Context terms usable for one ambiguous name: excludes words of the name itself, so the
    name cannot corroborate itself."""
    own = set(fold(name).split())
    return [c for c in ctx if not set(fold(c).split()) <= own]


def nickname_terms(rec: dict, limit: int = 8) -> list[str]:
    """Ambiguous names searched *only* together with context terms.

    Low-quality aliases, aliases too short to search alone, and generic institutional names
    ("Ministry of National Defence"). Kept as written, titles included ("Commandant Jérôme").
    Names made only of common given names ("Hassan", "Abdul Rahman") are dropped: no context
    can make them identify one person.
    """
    out, seen = [], set()
    pool = rec.get("low_aliases", []) + [
        a for a in [rec.get("name", ""), *rec.get("aliases", [])] if a
        if not is_searchable(_strip_titles(display_name(a)), rec["kind"], 8)
        or is_generic_name(_strip_titles(display_name(a)))
        or (rec["kind"] == "individual" and is_thin_name(_strip_titles(display_name(a))))]
    for n in pool:
        n = display_name(n)
        f = fold(n)
        toks = f.split()
        acronym = rec["kind"] == "entity" and n.isupper() and n.isalnum() and len(n) >= 3
        if not f or f in seen or f in _WEAK_ALIAS or all(t in _WEAK_ALIAS for t in toks):
            continue
        if is_common_name(n) and not acronym:
            continue
        if len(toks) == 1 and len(f) < 6 and not acronym:
            continue
        seen.add(f)
        out.append(n)
    return out[:limit]


_DOC_NUM = re.compile(r"\b(?:[A-Z]{1,3}[\s-]?)?\d[\d\s-]{5,}\d\b")


def identifiers(rec: dict) -> list[str]:
    """Passport / national ID numbers and IMO vessel numbers as searchable strings."""
    out = []
    nums = rec.get("doc_numbers") or [m for d in rec.get("documents", []) for m in _DOC_NUM.findall(d)]
    for n in nums:
        compact = re.sub(r"[^A-Za-z0-9]", "", n)
        if len(compact) >= 7 and sum(c.isdigit() for c in compact) >= 5:
            out.append(compact)
    for m in re.findall(r"IMO(?:\s*(?:number|no\.?|#))?[:\s]*([0-9]{7})", rec.get("comments", ""), re.I):
        out.append(f"IMO {m}")
    return list(dict.fromkeys(out))


def find_identifiers(text: str, ids: list[str]) -> list[str]:
    compact = re.sub(r"[^a-z0-9]", "", fold(text))
    return [i for i in ids if re.sub(r"[^a-z0-9]", "", i.lower()) in compact]


@dataclass
class Query:
    kind: str                 # "names" | "nicknames" | "identifiers"
    terms: list[str]
    context: list[str] = field(default_factory=list)


def plan_queries(rec: dict, min_len: int = 8, per_query: int = 4) -> list[Query]:
    """Every name variant of the party, spread over as many queries as needed."""
    qs: list[Query] = []
    names = search_names(rec, min_len=min_len, limit=100)
    for i in range(0, len(names), per_query):
        qs.append(Query("names", names[i:i + per_query]))
    ctx = context_terms(rec)
    nicks = nickname_terms(rec)
    for n in nicks:
        c = context_for(n, ctx)
        if c:
            qs.append(Query("nicknames", [n], c))
    ids = identifiers(rec)
    for i in range(0, len(ids), per_query):
        qs.append(Query("identifiers", ids[i:i + per_query]))
    return qs


def news_match_names(rec: dict, min_len: int = 8) -> list[str]:
    """Names used to confirm a news hit: all names + nicknames (the filter judges context)."""
    return all_match_names(rec, min_len) + nickname_terms(rec, limit=50)
