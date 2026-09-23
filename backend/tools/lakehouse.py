"""The Lakehouse tool: discovery, series fetch, and a guarded SQL escape hatch.

Discovery is the tool that matters most. A 27B model asked "konut kredileri"
cannot be expected to know that housing loans live in `tuketici_kredileri` and
not in the sectoral table, that the row is keyed `tuketici_kredileri_konut`,
that it publishes three currencies, or that the interest rate is an EVDS series
in a different table entirely. `discover` answers all of that from the three
index tables the build maintains -- `bulletin_entities`, `weekly_items` and
`macro_series` -- so the planner names a concept and gets back keys.

Search runs on the ASCII-slugified key as well as the published name, because
Turkish case folding breaks the obvious approach: 'I'.lower() is not 'i', so an
ILIKE on a name containing 'İ' misses. A small alias table bridges the other
gap -- a question in English, or using a word the regulator does not ("mortgage",
"NPL", "enflasyon") -- because the corpus is published in Turkish only.
"""
import os
import re
import time
from typing import Any, Dict, List, Optional

import duckdb

from ..core.config import DUCKDB_PATH
from ..core.labels import fold, slugify
from .series import SeriesResult, load_series
from .vector_store import default_store, reciprocal_rank_fusion

# Read-only surface the agent is allowed to query. `observations` and the TBB
# tables are omitted on purpose: they are a second vocabulary for the same BDDK
# table 05, in a different unit, and letting a model choose between them is how
# a 1000x error reaches an answer. Cross-source questions go through
# reconciliation_monitor, which is here for exactly that.
ALLOWED_TABLES = {
    "bulletin_entities", "bulletin_observations", "bulletin_metrics", "bulletin_footnotes",
    "weekly_items", "weekly_observations", "macro_series", "macro_observations",
    "macro_observations_native", "data_quality_report", "reconciliation_monitor",
    "bulletin_lifecycle_report", "weekly_lifecycle_report",
    "finturk_observations", "finturk_metrics",
    # The external zone: views over data/external/, sources landed at runtime.
    "external_sources", "external_series", "external_observations",
    "external_observations_native", "external_quality_report",
}

FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|copy|export|install|load|pragma|set)\b", re.I)

# Words a question is likely to use that the corpus never publishes. Deliberately
# short and hand-checked: every entry was verified to resolve to a real key.
ALIASES: Dict[str, List[str]] = {
    "mortgage": ["konut"], "housing": ["konut"], "housing loan": ["konut"],
    "home loan": ["konut"], "konut kredisi": ["konut"],
    "vehicle": ["tasit"], "car loan": ["tasit"], "auto": ["tasit"],
    "personal loan": ["ihtiyac"], "consumer": ["tuketici"], "consumer loan": ["tuketici"],
    "credit card": ["kredi_kartlari"], "kredi karti": ["kredi_kartlari"],
    "npl": ["takipteki"], "non-performing": ["takipteki"], "bad loan": ["takipteki"],
    "takipteki kredi": ["takipteki"], "batik": ["takipteki"],
    "deposit": ["mevduat"], "savings": ["mevduat"],
    "capital adequacy": ["sermaye_yeterliligi"], "car ratio": ["sermaye_yeterliligi"],
    "profit": ["kar_zarar"], "earnings": ["kar_zarar"], "net income": ["donem_net_kari"],
    "inflation": ["TUFE", "GENENDEKS"], "enflasyon": ["TUFE", "GENENDEKS"], "cpi": ["GENENDEKS"],
    "tufe": ["GENENDEKS"], "tuketici fiyat": ["GENENDEKS"],
    "house price": ["KFE", "Konut Fiyat"], "konut fiyat": ["KFE"], "hpi": ["KFE"],
    "interest rate": ["faiz", "KTF"], "faiz orani": ["faiz", "KTF"],
    "faiz oranlari": ["faiz", "KTF"], "faizi": ["faiz", "KTF"], "faiz": ["KTF", "faiz"],
    # APIFON4 specifically: EVDS3 publishes no policy-rate series, and the
    # weighted average funding cost is the registered proxy. The group also
    # holds funding AMOUNTS, so the bare group prefix is not specific enough.
    "policy rate": ["APIFON4"], "politika faizi": ["APIFON4"],
    "funding cost": ["APIFON4"], "fonlama maliyeti": ["APIFON4"],
    "exchange rate": ["DK.USD", "kur"], "doviz kuru": ["DK.USD"], "usd": ["DK.USD"],
    # TL deposit rates are EVDS flow-weighted series; "mevduat faizi" otherwise
    # ranks the interest-rate swap line, which merely contains both words.
    "mevduat faizi": ["TRY.MT06"], "mevduat faizleri": ["TRY.MT06"], "mevduat faiz": ["TRY.MT06"],
    "deposit rate": ["TRY.MT06"],
    # KKM is published in BOTH corpora: the weekly bulletin carries it as an
    # informational line and EVDS publishes the TCMB aggregate (bie_kkm).
    "kkm": ["kur korumal"], "kur korumali": ["kur korumal"], "kur korumalı": ["kur korumal"],
    "house sales": ["KONUTSAT"], "konut satis": ["KONUTSAT"],
    "mortgaged sales": ["AKONUTSAT2", "IPOTEKLI"], "ipotekli": ["IPOTEKLI", "AKONUTSAT2"],
    "unemployment": ["issizlik"], "gdp": ["GSYIH"], "growth": ["GSYIH"],
    # Added after the discovery benchmark: each of these reached NO key at all,
    # and every target below was checked to resolve to exactly one row.
    # Abbreviations especially -- they are shorter than the four-letter floor
    # `_terms` applies to plain words, so an abbreviation only ever enters the
    # search as an alias.
    "syr": ["sermaye_yeterliligi_standart"], "capital adequacy ratio": ["sermaye_yeterliligi_standart"],
    "roe": ["ortalama_ozkaynaklar"], "return on equity": ["ortalama_ozkaynaklar"],
    "ozkaynak karliligi": ["ortalama_ozkaynaklar"], "ozkaynak getirisi": ["ortalama_ozkaynaklar"],
    "ozkaynak karlilik": ["ortalama_ozkaynaklar"],
    "tga": ["takipteki"], "donmus": ["takipteki"],
    "sorunlu": ["takipteki"], "sorunlu alacak": ["takipteki"],
    "net profit": ["donem_net_kari"], "net kar": ["donem_net_kari"],
    "net kar tutari": ["donem_net_kari"], "sektorun kari": ["donem_net_kari"],
    "total deposits": ["mevduat"], "toplam mevduat": ["mevduat"],
    "dolar kuru": ["DK.USD"], "amerikan dolari": ["DK.USD"],
    "loan to deposit": ["nakdi_krediler_toplam_mevduat"],
    "kredi mevduat orani": ["nakdi_krediler_toplam_mevduat"],
    "ev kredisi": ["konut"],
    # "mevduat faiz" alone loses the longest-match race to "faiz oranlari",
    # which overlaps it and is one character longer, so the full phrase is
    # registered rather than relying on the tie.
    "mevduat faiz oranlari": ["TRY.MT06"], "mevduat faiz orani": ["TRY.MT06"],
}


# A currency is a DIMENSION of the fact tables, not a word to search for.
# Measured on "Yabancı Para (YP) Mevduat ve TL Mevduat": searching the words
# matched the FX net-position table (its *name* contains "yabancı para") and
# fetched the deposit line as `total`, so the answer declared that the data
# holds no TL/FX split -- which it does, in every balance-sheet line. So the
# currency words are peeled off the concept before search, the search runs on
# what is left ("mevduat"), and the slice comes back as `currency` on every
# candidate that publishes it, for the plan to pass straight into the fetch's
# WHERE clause. Nothing here is left to the model or to a ranking bonus.
# "milyon TL" names a unit, not a slice, and is left alone; "TP." is a series
# code prefix, not Türk Parası.
CURRENCY_TERMS = {
    "FX": re.compile(r"(?<!\w)(yp|fx|yabanc[ıi]\s+para|d[öo]viz\s+cinsi(?:nden)?|d[öo]vizli|"
                     r"foreign[- ]currency)(?!\w)", re.I),
    "TL": re.compile(r"(?<!\w)(?<!milyon )(?<!milyar )(?<!bin )(?<!trilyon )"
                     r"(tl|tp(?![\w.])|t[üu]rk\s+liras[ıi]|t[üu]rk\s+paras[ıi]|turkish[- ]lira)(?![\w])", re.I),
}

# "YP mevduatin TL karsiligi" names ONE slice (FX) and states how its value
# is expressed, not a second slice -- but "TL" alone matches CURRENCY_TERMS
# same as a real TL-slice request, so the pair looked like "both named" and
# extract_currency bailed to None. Measured: that sent "Yabanci Para (YP)
# mevduatlarin TL karsiligi ..." to an unrelated 14-month EVDS series
# instead of the bulletin's own 67-month FX-currency deposit line. Stripping
# "<currency word> karsiligi" before the ambiguity check removes only the
# valuation phrase, not the slice it is naming.
_KARSILIK = re.compile(
    r"(yp|fx|yabanc[ıi]\s+para|d[öo]viz\w*|tl|tp|t[üu]rk\s+liras[ıi]|t[üu]rk\s+paras[ıi])"
    r"\s+kar[şs][ıi]l[ıi][ğg]\w*", re.I)


def extract_currency(text: str):
    """(currency, text without the currency words) -- (None, text) when the
    concept names no slice or names both, which the clause split normally
    prevents ("YP mevduat ve TL mevduat" arrives as two concepts)."""
    text = _KARSILIK.sub(" ", text or "")
    found = [currency for currency, pattern in CURRENCY_TERMS.items() if pattern.search(text)]
    if len(found) != 1:
        return None, text
    stripped = CURRENCY_TERMS[found[0]].sub(" ", text)
    stripped = re.sub(r"\(\s*\)", " ", stripped)               # "(YP)" leaves "()"
    return found[0], re.sub(r"\s+", " ", stripped).strip()


# A named source is a filter, not a search term -- the same lesson as the
# currency slice, in the other dimension. "BDDK haftalik bultenine gore toplam
# krediler" scored the weekly row it asks for at pool rank 83, because
# "haftalik" and "bulten" match nothing in any row and only dilute the two
# words that do. Measured before trusting these as filters: `haftalik`, `bddk`
# and `evds` appear in ZERO series names across all three corpora, so peeling
# them off can never remove a word that names a line.
#
# `tcmb` and `merkez bankasi` are deliberately absent: they occur inside 17 and
# 12 published names ("TCMB Agirlikli Ortalama Fonlama Maliyeti"), so treating
# them as a source would strip the subject out of the question.
#
# BDDK publishes both the monthly and the weekly bulletin, so "bddk" alone
# narrows to those two rather than to either one; the sets are intersected so
# "BDDK haftalik bulten" resolves to weekly and "BDDK aylik bulten" to monthly.
_SUFFIX = r"(?:['\u2019]\w*)?"
_BULTEN = r"\s+b[\u00fcu]lten\w*" + _SUFFIX
SOURCE_TERMS = (
    # FinTurk is the only release with a province grain, so "il bazinda" /
    # "illere gore" / "sehir bazli" name it as surely as "haftalik" names the
    # weekly bulletin. Listed first because it is the most specific BDDK
    # release a question can name.
    ("bddk", re.compile(r"(?<!\w)(?:fint[\u00fcu]rk|il(?:ler)?(?:e)?\s+(?:baz[\u0131i]nda|bazl[\u0131i]|"
                        r"g[\u00f6o]re|d[\u00fcu]zeyinde|baz[\u0131i]nda)|[\u015fs]ehir(?:ler)?(?:e)?\s+"
                        r"(?:baz[\u0131i]nda|bazl[\u0131i]|g[\u00f6o]re)|il\s+il|co[\u011fg]rafi\s+da[\u011fg])"
                        r"\w*", re.I), {"finturk"}),
    ("bddk", re.compile(r"(?<!\w)haftal[\u0131i]k(?:" + _BULTEN + r")?(?!\w)", re.I), {"weekly"}),
    ("bddk", re.compile(r"(?<!\w)ayl[\u0131i]k" + _BULTEN, re.I), {"bulletin"}),
    # "BDDK bulteni" with neither "aylik" nor "haftalik" said is the monthly
    # one: the weekly release is never called anything but "haftalik bulten".
    ("bddk", re.compile(r"(?<!\w)bddk" + _SUFFIX + _BULTEN, re.I), {"bulletin"}),
    ("evds", re.compile(r"(?<!\w)evds" + _SUFFIX, re.I), {"macro"}),
    # Least specific in its publisher, so it is listed last: it only decides
    # when nothing above it said which BDDK release.
    ("bddk", re.compile(r"(?<!\w)bddk" + _SUFFIX, re.I), {"bulletin", "weekly"}),
)

ALL_SOURCES = {"bulletin", "macro", "weekly", "finturk", "external"}

# Words that say a question is about the province grain -- the only reason
# to prefer a quarterly, province-summed FinTurk row over the monthly
# bulletin's line for the same concept. The 81 province names are read from
# the corpus itself (once per process) rather than typed here.
PROVINCE_HINT = re.compile(
    r"(?<!\w)(il|ili|iller|illere|ilinde|ilde|ilin|ilindeki|illerde|sehir|sehri|sehirler|sehirde|"
    r"bolge|bolgesi|bolgesel|bolgede|finturk|province|city|regional)(?!\w)", re.I)
_PROVINCES: Optional[Dict[str, str]] = None


def _province_names() -> Dict[str, str]:
    """Folded province name -> the spelling `finturk_observations` uses, loaded
    on first use; empty when the lakehouse holds no FinTurk table."""
    global _PROVINCES
    if _PROVINCES is None:
        try:
            con = _connect()
            try:
                rows = con.execute("SELECT DISTINCT province FROM finturk_observations").fetchall()
            finally:
                con.close()
            _PROVINCES = {fold(str(r[0])): str(r[0]) for r in rows if r[0]}
        except Exception:                                    # noqa: BLE001 -- no table, no names
            _PROVINCES = {}
    return _PROVINCES


# A province name with the case suffix Turkish glues to it: "İstanbul'daki",
# "Ankara'da", "İzmirdeki". Longest names first so "Afyonkarahisar" is not
# matched as "Afyon" plus a suffix.
def _province_pattern(folded_name: str) -> str:
    return rf"(?<![a-z]){re.escape(folded_name)}(?:['’]?[a-z]{{1,6}})?(?![a-z])"


def extract_province(text: str):
    """(province as the table spells it, text without the province word) --
    (None, text) when no province is named or several are.

    The same rule as `extract_currency`: a province is a DIMENSION of the
    FinTurk fact table, not a word any row's name contains, so it is peeled
    off the concept before search and comes back as `province` on every
    FinTurk candidate for the plan to pass straight into the fetch.
    """
    text = text or ""
    folded = fold(text)
    found = []
    for name in sorted(_province_names(), key=len, reverse=True):
        match = re.search(_province_pattern(name), folded)
        if match and not any(match.start() < e and s < match.end() for s, e in (m.span() for _, m in found)):
            found.append((name, match))
    if len(found) != 1:
        return None, text
    name, match = found[0]
    # `fold` is length-preserving (one ASCII letter per Turkish one), so the
    # span found on the folded text cuts the original at the same offsets.
    if len(folded) == len(text):
        text = text[:match.start()] + " " + text[match.end():]
    else:
        text = re.sub(_province_pattern(name), " ", folded)
    return _province_names()[name], re.sub(r"\s+", " ", text).strip()


def names_a_province(text: str) -> bool:
    """Does the question say "il"/"şehir"/"bölge" or name one of the 81?"""
    folded = fold(text or "")
    if PROVINCE_HINT.search(folded):
        return True
    return any(re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", folded) for p in _province_names())


def extract_sources(text: str):
    """(allowed sources or None, text without the source words).

    None means the question named no source and every corpus is eligible --
    which is the common case, and the reason this narrows rather than routes.
    """
    text = text or ""
    allowed, spans, decided = None, [], set()
    for publisher, pattern, sources in SOURCE_TERMS:
        match = pattern.search(text)
        if not match:
            continue
        # SOURCE_TERMS is ordered specific-first, and a later pattern inside a
        # region an earlier one already claimed says nothing new. Letting both
        # cut left "BDDK bultenindeki Takipteki" as "kipteki": the second cut
        # was measured against the original text and applied to the shortened
        # one, so it ate the start of the next word.
        if any(match.start() < end and start < match.end() for start, end in spans):
            continue
        spans.append(match.span())
        # Every match gets its words stripped; only the first per publisher
        # narrows the sources, so a second mention of the same release cannot
        # widen what a more specific phrase already settled.
        if publisher not in decided:
            decided.add(publisher)
            allowed = set(sources) if allowed is None else allowed | sources
    if allowed is None:
        return None, text
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " + text[end:]
    return allowed, re.sub(r"\s+", " ", text).strip()


def _names_the_currency(candidate, query: str) -> bool:
    """Does the candidate's own key or name contain the currency phrase the
    question used? Then the phrase names the line, not a slice of it."""
    for pattern in CURRENCY_TERMS.values():
        match = pattern.search(query or "")
        if not match:
            continue
        phrase = slugify(match.group(0))
        haystack = f"{candidate.get('key') or ''} {slugify(candidate.get('name') or '')}"
        if re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", haystack):
            return True
    return False


def _connect():
    if not DUCKDB_PATH.exists():
        raise FileNotFoundError(f"{DUCKDB_PATH} not found -- run `python -m backend.lakehouse.build` first")
    return duckdb.connect(str(DUCKDB_PATH), read_only=True)


# Generic words that match hundreds of rows and mean nothing on their own.
# "faiz" is here because almost every bulletin table has an interest accrual
# line; it stays useful as an alias expansion, just not as a term of its own.
# A qualifier narrows a line to a sub-case. "Konut kredileri" means the loan
# book; "takipteki konut kredileri" is a different, much smaller series that
# happens to contain the first as a substring, so matching alone ranks it
# first. Penalise a qualifier the question did not ask for.
# Does the question ask for a quotient rather than a quantity? Used to gate the
# derived-series bonus; the unit words in `core.search_text` cover the rest.
RATIO_WORDS = re.compile(
    r"(?<!\w)(oran|orani|oranlari|oranini|rasyo|rasyosu|yuzde|payi|share|ratio|%)(?!\w)", re.I)

# The mirror problem to RATIO_WORDS: a query naming an amount alongside a rate
# ("faiz orani konut kredisi HACMINI ongormeye yardimci oluyor mu") matches
# RATIO_WORDS on "orani" and its blanket +3%/-3TL bonus demotes the very TL
# series the "hacmi" half of the question asked for -- discover() has no
# per-word sense of which half of the query a candidate answers, only whether
# ratio words appear anywhere in it. AMOUNT_WORDS does not change that scoring
# (a blanket "amount seen -> demote every % candidate" would just as wrongly
# demote the correct rate series sitting in the same query); it only marks a
# query as "mixed" so discover() can additionally guarantee an amount-side
# candidate reaches the pool the ratio-side ranking would otherwise keep out.
AMOUNT_WORDS = re.compile(
    # "hacim" drops its second vowel before a vowel-initial suffix (Turkish
    # vowel elision: hacim + i -> hacmi, + ini -> hacmini), so "hacim\w*"
    # alone misses every possessive/case form except the bare word.
    # "tutar\w*" must not reach "tutarli"/"tutarsiz" ("consistent" /
    # "inconsistent" -- COMPOSER_SYSTEM tells the model to write exactly this
    # word pair for a decompose finding): those are a lexicalised adjective,
    # unrelated to the noun "tutar" (amount) despite sharing its stem, and
    # "faiz orani ... tutarli mi" is a plausible real question with no
    # amount in it at all.
    r"(?<!\w)(hac(?:im|m)\w*|tutar(?!li|siz)\w*|bakiye\w*|stok\w*|miktar\w*)(?!\w)", re.I)

# The property type a sales question would have to name to mean commercial
# premises. Absent these, TCMB's `K`-prefixed housing series is what was meant.
WORKPLACE_WORDS = re.compile(r"(?<!\w)(is\s*yeri|isyeri|ticari\s+gayrimenkul|dukkan|ofis|commercial|workplace)", re.I)

QUALIFIERS = {
    "takipteki": ("takipteki", "npl", "non-performing", "batik", "sorunlu", "tga", "donmus"),
    # "Konut satislari" is every sale; "ipotekli konut satislari" is the
    # mortgaged subset, and the derived share series is named with both phrases
    # inside it -- so the query phrase "konut satislari" is a literal substring
    # of "ipotekli konut satislarinin toplam konut satislarina orani" and the
    # ratio outranked the count it is computed from.
    "ipotekli": ("ipotekli", "mortgaged", "ipotek"),
    "dovize_endeksli": ("dovize", "endeksli", "fx-indexed"),
    # "Diger Mevduat" (Other Deposits, a specific leftover bucket) is a sibling
    # of "Toplam Mevduat" (Total Deposits) inside the same FinTurk table, close
    # enough in every other word that a plain "toplam mevduat" question (with
    # "toplam" stopped, see STOPWORDS above) scored "diger_mevduat" 0.02 points
    # above the row actually asked for. A question does not mean the "Diger"
    # bucket unless it says so, the same as every other qualifier here.
    "diger": ("diger", "diğer", "other"),
    "reeskont": ("reeskont", "accrual"),
    "bilgi": ("bilgi",),
    "verilen_faizler": ("verilen", "odenen", "gider"),
    "alinan_faizler": ("alinan", "gelir"),
}

# EVDS publishes each loan-rate concept twice -- a flow ("Akim", new lending
# that month) and a stock ("Stok", the whole book's average) -- and only the
# flow series carries the "KTF" code the "faiz" alias rewards (+6, see
# ALIASES above), so "Taşıt Kredisi (TL, **Stok**, %)" named verbatim, four
# times, still lost to the Akim series on score. This is NOT a QUALIFIERS
# entry on purpose: QUALIFIERS penalises a candidate whenever ITS OWN word
# is not in the query, regardless of the query's own wording -- correct for
# a genuinely rare sub-bucket ("diger", "ipotekli") where the generic bucket
# should win by default, wrong here, where Akim IS the default the "ktf"
# alias was built for. Measured: adding "akim"/"stok" as QUALIFIERS demoted
# every legitimate Akim match on any query that simply never said the word
# "akim" ("mortgage interest rate" among them) -- recall@3 dropped from
# 92.6% to 87.9%. The block below only fires when the query explicitly
# names ONE of the two, and boosts the one asked for rather than only
# penalising the other, mirroring RATIO_WORDS' own +3/-3 shape.
# is present in some candidate the query did not ask for.
AKIM_STOK = {
    "akim": re.compile(r"(?<!\w)(akim|akım|flow)(?!\w)", re.I),
    "stok": re.compile(r"(?<!\w)(stok|stock)(?!\w)", re.I),
}

STOPWORDS = {
    "kullan", "kullanin", "kullanınız", "sistemdeki",
    # English boilerplate
    "the", "and", "for", "with", "show", "give", "what", "which", "how", "over",
    "between", "please", "monthly", "data", "chart", "table", "also", "using",
    # Turkish question boilerplate. A demo question is a sentence, not a keyword:
    # without these, "gosteriniz" and "dagilimini" score as loudly as "konut" and
    # the series the question is actually about falls out of the candidate list.
    # "total"/"toplam" stays a stopword on purpose, even though it looks like the
    # same trap "agirlikli" is not: letting it score as a normal word fixed
    # "Ankara'da toplam mevduat hacmi" (see the "diger" qualifier below for the
    # fix that was actually used) but broke a pinned, harder case the other way
    # -- "toplam konut kredilerinin dagilimini" then outranked the housing-loan
    # line with "Toplam Krediler" (the whole bank's loan book, matching only
    # "toplam"), because a bare `key.startswith("toplam")` bonus outweighs one
    # matching "konut" concept. A qualifier can be undone with one word telling
    # it what NOT to be; "toplam" scored as a term cannot be told what it must
    # additionally match, so the general-purpose fix is the wrong shape for it.
    "total", "toplam", "olarak", "icin", "için", "nedir", "nasil", "nasıl",
    "grafik", "tablo", "aylik", "aylık", "veri", "veriler", "veriden", "turkiye",
    "türkiye", "turkey", "goster", "göster", "gosteriniz", "gösteriniz",
    "arasinda", "arasında", "arasindaki", "arasındaki", "yillari", "yılları",
    "ayrica", "ayrıca", "lutfen", "lütfen", "kullanilan", "kullanılan",
    "dagilimini", "dağılımını", "dagilim", "dağılım", "buna", "bunu", "bunlar",
    "misin", "misiniz", "musun", "eder", "edebilir", "getir", "getirebilir",
    "donem", "dönem", "donemlerde", "dönemlerde", "miktari", "miktarı",
    "degisim", "değişim", "gostermis", "göstermiş", "olmus", "olmuş",
    "yukselmedigi", "yükselmediği", "dustugu", "düştüğü", "halde", "sadece",
    "yeni", "sutun", "sütun", "hangi", "yapabilir", "verilerini", "kullanarak",
    "gore", "göre", "gostermektedir", "bulten", "bülten", "bulteni", "bülteni",
    # "karşılaştır" is a question verb, and its five-letter stem "karsi" is a
    # substring of "karsiligi": the provision-coverage ratio outranked the
    # NPL ratio for a question that merely asked to compare two series.
    "karsilastir", "karsilastirin", "karsilastirma", "karsilastirmasi", "kiyasla", "kiyaslayin",
    # "Agirlikli ortalama" names a METHOD, not a subject: `validation.macro`
    # already records that 92 EVDS rate series publish it as their BIRIMI, which
    # is why the unit is resolved per series instead of read from the group. It
    # discriminates nothing as a search term either, and it actively misleads --
    # "Agirlikli Ortalama Ticari Kredi Faizleri" ranked the TCMB funding cost
    # first, whose name carries both words, above the commercial-loan rate the
    # question names. "ortalama" is left alone: it is the subject in
    # "Ortalama Toplam Aktifler".
    "agirlikli", "ağırlıklı",
    # Presentation verbs and the case-inflected table/chart nouns. "tablo" and
    # "grafik" were already here, but the five-character stemmer below is
    # applied to the *inflected* word ("tabloyu" -> "tablo") and used to skip
    # this list, and `%tablo%` is a substring of the EVDS money-supply code
    # TP.HPBITABLO1: "bu tabloyu bozmadan grafigini ciz" fetched M1. Folded
    # (ASCII) forms, because `_terms` folds before it looks here.
    "tabloyu", "tablonun", "tabloya", "tablodaki", "tablodan", "tablosu", "tablosunu",
    "grafigi", "grafigini", "grafige", "grafigin", "grafikle", "grafikte", "grafikler",
    "cizin", "cizer", "cizdir", "cizebilir", "cizsene", "cizelim", "cizilsin",
    "bozmadan", "bozmaksizin", "gosterin", "gosterir", "gosterebilir", "gostersene",
    "listeleyin", "listeler", "olustur", "olusturun", "hazirla", "duzenle",
    "halinde", "seklinde", "bunun", "bunlarin", "sunu", "onun",
    # The demo's own verbs and amount words. "arindirir" stems to "arind",
    # a substring of "kredi_kartlarindan": the deflation clause of the
    # reference scenario ranked credit-card debt first. "tutar" is the same
    # kind of word as "miktari" above -- it says "amount", names nothing.
    "arindir", "arindirir", "arindirin", "arindirilmis", "arind",
    "tutar", "tutari", "tutarini", "tutarlari", "tutarlarini", "tutarlarinin",
    "verisi", "verisini", "veriyi", "seti", "setine", "setini", "ayni", "aylara",
    "ekle", "ekleyin", "hizala", "sekilde",
}


def _terms(query: str):
    """Weighted search terms: (term, weight).

    Three rules, each earned by a ranking failure:

    - The full phrase is a strong signal, *unless* the query is a single word --
      then the "phrase" is just the word, and scoring it twice let a balance-sheet
      line called "Odenmis Sermaye Enflasyon Duzeltme Farki" outrank the CPI index
      for the query "enflasyon".
    - Aliases match on word boundaries, not substrings, so "faiz" does not fire
      inside "politika faizi".
    - When two alias phrases overlap, the longer one wins and the shorter is
      dropped. "politika faizi" maps to the funding-cost proxy; without this,
      the "faizi" alias fired alongside it and pulled up interest-rate swaps.
    """
    # Folded, not just lowercased: 'İ'.lower() is 'i̇' (i plus a combining dot),
    # which matches nothing, and a question writing "orani" must reach a row
    # published as "oranı". Keys are ASCII already, so this only changes what
    # the names and the search text can be compared against.
    lowered = fold(query).strip()
    lowered = re.sub(r"\bayni (?:aylara|doneme) denk gelecek sekilde\b", " ", lowered).strip()
    single_word = len(lowered.split()) == 1
    weighted = {lowered: 1.5 if single_word else 4.0}

    consumed = []
    for phrase in sorted(ALIASES, key=len, reverse=True):
        # A Turkish noun carries its case: the demo's own turn 2 says
        # "enflasyonDAN arindir", and an alias that fires only on the bare
        # word "enflasyon" never reached TUFE for it. A short suffix is
        # allowed after any alias of four letters or more -- four letters, a case
        # ending, not a whole derivation: "satislarinin" must not fire "konut
        # satis" or the count outranks the mortgage share. The two- and
        # three-letter ones ("npl", "gdp", "usd", "kkm") stay exact, because
        # "usd" + a suffix is how an abbreviation gets inside another word.
        suffix = r"\w{0,4}" if len(phrase) >= 4 else ""
        match = re.search(rf"(?<!\w){re.escape(phrase)}{suffix}(?!\w)", lowered)
        if not match:
            continue
        if any(match.start() < end and start < match.end() for start, end in consumed):
            continue                                    # a longer alias already claimed these words
        consumed.append(match.span())
        for expansion in ALIASES[phrase]:
            key = expansion.lower()
            # ...and so does an expansion that is one of the phrase's own
            # words: "faiz orani" -> "faiz" tripled the weight of "faiz" and
            # ranked a bulletin ratio whose name says it twice above the
            # loan rate the question named. Only the vocabulary the phrase
            # does NOT already contain ("KTF") is worth adding.
            if key == phrase.lower() or key in phrase.lower().split():
                # A self-map ("faiz" -> "faiz") adds no vocabulary; it only
                # triples the weight of a word the question already used, which
                # is how one incidental "faiz" buried the loan series the
                # question was actually about. Let it score as a plain word.
                continue
            weighted[key] = max(weighted.get(key, 0), 3.0)

    for word in re.split(r"[^\w\u00c0-\u024f]+", lowered):
        if len(word) > 3 and word not in STOPWORDS and not word.isdigit():
            weighted.setdefault(word, 1.0)
            # Turkish is agglutinative: "kredilerinin" and "kredileri" are the
            # same concept as "kredi", and an exact-substring search finds
            # neither in a key spelled "tuketici_kredileri_konut". A five-character
            # prefix is a crude stemmer, but it costs nothing and recovers the
            # match that matters.
            # The stem must clear the stopword list too: "tabloyu" is not a
            # stopword, its stem "tablo" is, and a stem that skips the check
            # is a stopword back in the query with the same substring reach.
            if len(word) > 6 and word[:5] not in STOPWORDS:
                weighted.setdefault(word[:5], 0.8)
    return sorted(weighted.items(), key=lambda pair: -pair[1])[:18]


def _score(candidate, terms, province_named: bool = False) -> float:
    """Rank a candidate against the weighted terms.

    `province_named`: the question names a province or the il grain (or the
    caller asked for the FinTurk corpus), so a FinTurk row is what it wants.

    Beyond term matching, two structural preferences encode what a question
    usually means: a top-level row beats a sub-item of it (asking about housing
    loans means the line, not its FX-indexed sub-row), and a shorter key is the
    more canonical one. EVDS tier 0 is boosted because those series were
    registered precisely as the ones a demo question needs, and province-level
    codes are pushed down -- "konut satislari" means Turkiye unless a city is named.
    """
    key = fold(candidate.get("key") or "")
    name = fold(candidate.get("name") or "")
    # The context that names the row -- parent line, table or data group title,
    # category, words for its unit and kind. Scored below the name on purpose: a
    # data group called "Kredi Faiz Oranları" must help the series inside it be
    # found and must never outrank a series whose own name says "kredi faizi".
    context = str(candidate.get("search_fold") or "")
    score = 0.0
    for term, weight in terms:
        if term in key:
            score += weight * 2.0
            if key == term or key.startswith(term):
                score += weight
        if term in name:
            score += weight * 1.5
        elif term in context:
            score += weight * 0.6
    if not score:
        return 0.0

    # Coverage beats any single boost: "konut kredileri" names two concepts,
    # and a series matching both ("Tüketici Kredileri - Konut") is what was
    # asked for, where one matching only "konut" (the mortgaged-sales share:
    # tier 0 and derived, +5 of structural bonus) is a neighbour. Measured:
    # without this the share outranked the loan book for the anomaly question.
    # A concept is a question word (weight 1.0) or its 5-character stem -- the
    # phrase and the alias expansions are not separate concepts, and counting
    # the stem beside its word double-counted "enflasyon".
    stems = {term for term, weight in terms if weight == 0.8}
    concepts = 0
    for term, weight in terms:
        if weight != 1.0:
            continue
        stem = term[:5] if len(term) > 6 and term[:5] in stems else None
        haystacks = (key, name, context)
        if any(term in field for field in haystacks) or (
                stem and any(stem in field for field in haystacks)):
            concepts += 1
    score += 2.0 * max(0, concepts - 1)

    query_text = terms[0][0] if terms else ""
    for qualifier, asked_words in QUALIFIERS.items():
        present = qualifier in key or qualifier.replace("_", " ") in name
        asked = any(word in query_text for word in asked_words)
        if present and not asked:
            score -= 6.0

    # sektorel_kredi_dagilimi is the one table published in bin TL rather than
    # milyon TL, and it breaks credit down by the borrower's activity sector. A
    # product question ("konut kredileri") is nearly always answered by the
    # consumer-loan table instead, and mixing the two is a 1000x error.
    if candidate.get("dataset") == "sektorel_kredi_dagilimi" and "sekt" not in query_text:
        score -= 3.0

    # "Takipteki Alacaklar" names three different rows -- a balance-sheet stock
    # in milyon TL, a provision in milyon TL, and the published ratio in %. The
    # only word separating them is the one the question actually used, and a
    # unit has no spelling a text search reaches. Measured: without this the
    # stock won "NPL orani" outright, so a question about a ratio the corpus
    # publishes was answered with a quantity a thousand times its size.
    if RATIO_WORDS.search(query_text):
        if candidate.get("unit") == "%":
            score += 3.0
        elif str(candidate.get("unit") or "").endswith("TL"):
            score -= 3.0

    # EVDS's flow ("Akim")/stock ("Stok") split of the same loan-rate concept:
    # the "ktf" alias above (+6, "faiz" -> "KTF") only reaches the flow series,
    # since the stock ones are coded "BKR.TRY.*" -- so a query naming "Stok"
    # four times still lost to the flow series on score alone, and the QUALIFIERS
    # entries above (penalising whichever was NOT asked) still were not enough
    # on their own to close a "ktf"-sized gap. This is the other half: the one
    # actually named is also boosted, not merely left unpenalised.
    asked_akim, asked_stok = bool(AKIM_STOK["akim"].search(query_text)), bool(AKIM_STOK["stok"].search(query_text))
    if asked_akim != asked_stok:  # exactly one named; both or neither leaves this alone
        is_akim, is_stok = bool(AKIM_STOK["akim"].search(name)), bool(AKIM_STOK["stok"].search(name))
        if asked_stok and is_akim:
            score -= 8.0
        elif asked_stok and is_stok:
            score += 4.0
        elif asked_akim and is_stok:
            score -= 8.0
        elif asked_akim and is_akim:
            score += 4.0

    # "Konut kredileri" is an amount; "konut satislari" is a count. Both match
    # the word "konut", and EVDS publishes the sales count under a name that
    # contains it. Asking for credit and being handed a unit of "adet" is the
    # error this prevents.
    if candidate.get("unit") == "adet" and re.search(r"\bkredi|\bloan|\btutar|\bbakiye|\bstok", query_text):
        score -= 4.0

    # An external series_key is '<source_id>/<location>/<name>': the slashes
    # and the hash prefix are its address, not a sign of being a child row, so
    # only the name segment is judged for length.
    key_body = key.rsplit("/", 1)[-1] if candidate.get("source") == "external" else key
    score -= 1.5 * key_body.count("/")                  # a child row, not the line itself
    score -= min(len(key_body), 90) / 45.0              # prefer the canonical short key

    # FinTurk publishes many of the bulletin's concepts again, quarterly and
    # by province. Its rows answer a question that names a province or the
    # grain ("il bazında", "İstanbul'da") and nothing else does; for any other
    # question the monthly national line is the better answer. Measured on
    # "konut kredisi": the FinTurk twin, whose name IS the phrase, scored 25.5
    # against the bulletin line's 12.8, so a flat penalty left it first; the
    # multiplier puts it at 10.2, in the pool but below the line it duplicates.
    # The other way round, "Ankara konut kredileri" scored the FinTurk row 9.0
    # against the bulletin line's 11.3 (the city is not a term any row holds),
    # so the bonus has to be worth more than the phrase's own weight.
    if candidate.get("source") == "finturk":
        score = score + 4.0 if province_named else score * 0.4

    if candidate.get("source") == "macro":
        tier = candidate.get("tier")
        score += 3.0 if tier == 0 else (1.0 if tier == 1 else 0.0)
        raw_key = str(candidate.get("key") or "")
        # EVDS repeats a code per province. "Konut satislari" means Turkiye
        # unless a city is named, and TOPLAM is how TCMB spells the national row.
        # EVDS publishes several national variants under one name -- KTRTOPLAM
        # beside KMA and MA (moving-average forms). Demote the variants rather
        # than promoting the canonical row: a bonus on "TOPLAM" is not confined
        # to the tie it was meant to break, and twice let house SALES outrank
        # the housing-loan rate for "mortgage interest rate".
        # A province code is TR/KTR followed by a digit or A-C (TR100..TRC11);
        # ".TRY." is the lira, and the wider pattern demoted every TL deposit
        # rate (TP.TRY.MT06) out of the ranking.
        if re.search(r"\.K?MA$", raw_key):
            score -= 2.0
        elif "TOPLAM" not in raw_key.upper() and re.search(r"\.(KTR|TR)[0-9A-C]", raw_key):
            score -= 6.0
        # Built at build time so the agent never divides -- but only when the
        # question wants the quotient. There are 166 derived shares, and an
        # unconditional bonus let them outrank the very series they divide.
        if raw_key.startswith("DERIVED.") and RATIO_WORDS.search(query_text):
            score += 2.0
        # TCMB suffixes house sales with the property type: a leading `K` is
        # Konut, its absence Is Yeri. The two are published under names that
        # differ in that word alone, so "mortgaged sales share" separated them
        # by 0.03 points -- a key-length tie-break deciding between housing and
        # commercial property. A sales question means housing unless it says
        # otherwise, the same way it means Turkiye unless a city is named.
        if re.search(r"(?:AKONUTSAT\d|IPOTEKLI_PAY)\.(?!K)", raw_key) and not WORKPLACE_WORDS.search(query_text):
            score -= 5.0
    return round(score, 3)


# The negative lookbehind on "da"/"de" is load-bearing, not decorative: Turkish
# attaches the locative case suffix to a place name with an apostrophe and no
# space -- "Ankara'da", "İstanbul'de" -- and `\bda\b` alone cannot tell that
# apart from the standalone conjunction "da" ("too/also"), because an
# apostrophe is already a non-word character and satisfies `\b` on its own.
# Measured live: "Ankara'da toplam mevduat hacmi ne kadar?" split into
# "Ankara'" and "toplam mevduat hacmi ne kadar" -- severing the province name
# from the question that named it, so no finturk candidate downstream ever saw
# it named a province and the national bulletin total answered in its place.
# "bozmadan" / "sadece" open a new clause too: the demo's turn 2 -- "...faiz
# oranlari tablosunu bozmadan sadece konut kredisi tutarlarini enflasyondan
# arindirir misin" -- was one clause whose top three were all loan-rate
# series, and TUFE never reached the planner's candidate list.
CLAUSE_SPLIT = re.compile(
    r"[.,;?!]|\bbuna ek olarak\b|\bayrica\b|\bayrıca\b|\bve\b|\bile\b|"
    r"(?<!['’])\bda\b|(?<!['’])\bde\b|"
    r"\bbozmadan\b|\bbozmaks[ıi]z[ıi]n\b|\bsadece\b|\byaln[ıi]zca\b|"
    # Explicit dimension-to-row mappings need no punctuation between them.
    r"(?=\b(?:TP|TL|YP|FX)\s+i[çc]in\b)", re.I)


def split_clauses(question: str) -> List[str]:
    """Keep published labels (including nested parentheses) intact."""
    chunks, depth, start = [], 0, 0
    boundaries = re.compile(r"[()]|" + CLAUSE_SPLIT.pattern, CLAUSE_SPLIT.flags)
    for match in boundaries.finditer(question):
        token = match.group()
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            chunks.append(question[start:match.start()].strip())
            start = match.end()
    chunks.append(question[start:].strip())
    return [chunk for chunk in chunks if chunk]


def concept_identity(candidate: Dict[str, Any]) -> tuple:
    """Both the dataset and the currency slice are part of a series address."""
    return (candidate["source"], candidate["key"], candidate.get("currency"), candidate.get("dataset"),
            candidate.get("metric"), candidate.get("province"))


def entity_currency(name: str) -> Optional[str]:
    """A currency encoded by a published row, rather than a fact-table slice."""
    currency, _ = extract_currency(name)
    if currency:
        return currency
    if re.search(r"\bdoviz\s+tevdiat\b", fold(name or "")):
        return "FX"
    return None


def discover_concepts(question: str, per_concept: int = 4, limit: int = 8, *,
                      requested_basis: Optional[str] = None, frequency: Optional[str] = None):
    """Discovery over a whole question, by splitting it into concepts first.

    A demo question is a paragraph -- "...konut kredilerinin dagilimini aylik
    olarak gosteriniz. Buna ek olarak konut kredisi faiz oranlarini da
    gosteriniz..." -- and scoring it as one string dilutes every content word
    among thirty filler ones. No weighting fixes that: the sentence genuinely
    contains two questions. Splitting on clause boundaries and searching each
    piece recovers both, which is what the planner needs to see.
    """
    # A source word ("BDDK haftalık bültenine göre") stated once, before the
    # question's own "ve"/"," splits it into clauses, only survives in the
    # clause it textually sits in -- the other clause searches every corpus
    # unrestricted and can rank an obscure cross-source match above the
    # right answer in the corpus the question actually named. Measured:
    # "...haftalık bültenine göre toplam mevduat ve toplam kredilerin..."
    # split "toplam kredi" into its own clause with no source word left in
    # it, and it matched a bulletin line instead of the weekly bulletin's
    # own "Toplam Krediler (2+10)". Extracting once, over the whole
    # question, and handing it to every clause is the fix.
    named_sources, _ = extract_sources(question or "")
    semantic_basis = {"stock": "stok", "flow": "akim"}.get(requested_basis)
    question_basis = semantic_basis or rate_basis(question or "")
    context_words = set(re.findall(r"\w+", fold(question or "")))
    chunks = split_clauses(question or "")
    # Folded, as `_terms` folds: "grafiğini".lower() is not the ASCII
    # "grafigini" the stopword list holds, so an unfolded check let a
    # clause made only of presentation words through to the search.
    chunks = [chunk for chunk in chunks
              if any(len(word) > 3 and fold(word) not in STOPWORDS and fold(word)[:5] not in STOPWORDS
                     for word in re.split(r"[^\w\u00c0-\u024f]+", chunk))]
    if not chunks:
        chunks = [question]

    # Currency slices AND datasets survive: one deposit row occurs in both
    # the size and maturity tables, with different available metrics.
    merged, seen, by_concept = [], set(), []
    for chunk in chunks:
        # The whole-question source only backfills a clause that has neither
        # a source word nor a province of its own: a province names FinTurk
        # (or leaves the source open) by itself, and a source stated for a
        # DIFFERENT clause of an explicit comparison ("Istanbul'daki ... ile
        # BDDK aylik bultenindeki ...yi karsilastir") must not be forced onto
        # it, or the province-tagged FinTurk candidate never gets scored and
        # a national bulletin row answers "for Istanbul" in its place.
        chunk_sources, chunk_body = extract_sources(chunk)
        chunk_province, _ = extract_province(chunk_body)
        fallback_source = None if chunk_province else named_sources
        basis = semantic_basis or rate_basis(chunk) or question_basis
        found = discover(chunk, source=chunk_sources or fallback_source, limit=per_concept,
                         rate_basis_constraint=basis, frequency=frequency)["candidates"]
        # Identical row labels in different tables are disambiguated using
        # the whole question's dataset words (e.g. maturity), not row names.
        for candidate in found:
            candidate["dataset_match"] = sum(
                len(word) >= 4 and word in context_words
                for word in (candidate.get("dataset") or "").split("_"))
            candidate["dataset_requested"] = candidate.get("dataset_requested", False) or any(
                len(w) >= 4 and w not in fold(candidate.get("name") or "")
                and any(word.startswith(w) for word in context_words)
                for w in (candidate.get("dataset") or "").split("_")[-1:])
        found.sort(key=lambda c: (*_rank_key(c), -c["dataset_match"]))
        by_concept.append([concept_identity(c) for c in found])
        for candidate in found:
            identity = concept_identity(candidate)
            if identity not in seen:
                seen.add(identity)
                merged.append(candidate)
            elif candidate.get("name_match"):
                # An earlier vague clause must not erase later exact evidence.
                previous = next(c for c in merged if concept_identity(c) == identity)
                if _rank_key(candidate) < _rank_key(previous):
                    previous.update(candidate)
    merged.sort(key=lambda c: -c["score"])

    # Coordinated phrases share their noun: the first half of 'TP ve YP
    # mevduat' is not a separate concept without a measure. Resolve each role
    # against the whole context and retain the actual published identity.
    roles = [role for role, pattern in CURRENCY_TERMS.items() if pattern.search(question or "")]
    role_candidates = []
    for role in roles if any(c.get("dataset_requested") for c in merged) else []:
        hits = discover(question, source=named_sources, limit=1, currency_role=role,
                        frequency=frequency, rate_basis_constraint=question_basis)["candidates"]
        if hits and hits[0].get("entity_currency") == role and hits[0].get("dataset_requested"):
            candidate = hits[0]
            candidate["requested_role"] = role
            role_candidates.append(candidate)
            if concept_identity(candidate) not in seen:
                merged.append(candidate)
                seen.add(concept_identity(candidate))

    # Every clause's own first choice gets a seat, before anything competes on
    # score. This is the guarantee that matters, and a per-corpus quota was
    # standing in for it: scores are NOT comparable across clauses, because
    # each clause is scored against its own terms. Measured on the NPL/loan-rate
    # question -- clause 2's best answer, the commercial-loan rate, scored below
    # clause 4's best, so a merge ordered by score filled all eight seats
    # without it and the planner never saw the series the question named.
    # An explicitly quoted published name must survive even when many vague
    # setup/instruction clauses have already filled the context budget.
    kept = sorted([c for c in merged if c.get("name_match") or c in role_candidates],
                  key=lambda c: (-c["name_match"], -c["dataset_match"]))
    for ranked in by_concept:
        if not ranked:
            continue
        candidate = next((c for c in merged if concept_identity(c) == ranked[0]), None)
        if candidate is not None and candidate not in kept:
            kept.append(candidate)

    # Then the per-corpus quota, on the seats the clauses did not claim: a
    # rate-heavy question otherwise fills the remainder with EVDS series and the
    # planner sees no BDDK row to pair them with.
    per_source = {}
    for candidate in kept:
        per_source[candidate["source"]] = per_source.get(candidate["source"], 0) + 1
    for candidate in merged:
        if len(kept) >= limit:
            break
        if candidate not in kept and per_source.get(candidate["source"], 0) < max(2, limit // 3):
            kept.append(candidate)
            per_source[candidate["source"]] = per_source.get(candidate["source"], 0) + 1
    for candidate in merged:
        if len(kept) >= limit:
            break
        if candidate not in kept:
            kept.append(candidate)
    first_choices = {ranked[0] for ranked in by_concept if ranked}
    kept = sorted(kept[:limit], key=lambda c: (
        -c.get("name_match", 0), not c.get("requested_role"), concept_identity(c) not in first_choices,
        -c["score"], -c["dataset_match"]))

    # `by_concept` keeps each clause's own ranking: the merged list orders by
    # score, and a loud clause's second choice can outscore a quiet clause's
    # first. A deterministic plan wants the first choice of each clause.
    return {"query": question, "n_concepts": len(chunks), "concepts": chunks,
            "by_concept": by_concept, "n_candidates": len(kept), "candidates": kept,
            "currency_roles": {c["requested_role"]: c for c in role_candidates}}


def discover(query: str, source=None, limit: int = 8, rate_basis_constraint: Optional[str] = None,
             frequency: Optional[str] = None, currency_role: Optional[str] = None):
    """Find the lakehouse keys that answer a natural-language concept.

    The candidate pool is every row that matches any term -- not a truncated
    slice of them. An earlier version took an unordered `LIMIT 60` from the
    database and scored that, which meant the correct series was often never
    scored at all and the ranking looked mysteriously unstable. The three index
    tables hold 2,235 rows between them, so scoring all matches in Python costs
    nothing and is the only way the ranking means anything.

    Returns candidates ranked by relevance, each carrying everything the next
    step needs: the key, the unit, the temporal semantics, and which currencies
    and metrics the series actually publishes. A planner that reads this cannot
    invent a filter the data does not support.
    """
    # Two dimensions are peeled off the question before it is searched, for the
    # same reason: a word that names WHERE the series lives, or WHICH slice of
    # it is wanted, is a filter, and searching it ranks whatever row happens to
    # repeat it. The source goes first, because "BDDK haftalik bulten" is three
    # words of pure dilution around the one that matters.
    named_sources, query_body = extract_sources(query)
    currency, concept = extract_currency(query_body)
    currency = currency_role or currency
    concept = concept if currency and concept else query_body
    # A province is the third dimension peeled off before search: "İstanbul"
    # names no row, and left in the concept it only dilutes the words that do.
    province, concept = extract_province(concept)
    plain_terms = _terms(query_body)
    terms = _terms(concept) if concept and concept != query_body else plain_terms
    if not terms:
        return {"query": query, "terms_used": [], "currency": currency, "province": province,
                "sources": sorted(named_sources) if named_sources else None,
                "n_candidates": 0, "candidates": []}

    con = _connect()
    try:
        pooled = []
        # An explicit `source` argument is the caller's, and outranks the
        # question's own wording. `discover_concepts` passes a set here (a
        # source word extracted from the whole question, not just this one
        # clause); a single string from any other caller is still a plain
        # restriction to that one corpus.
        if source:
            wanted = {source} if isinstance(source, str) else set(source)
        else:
            wanted = named_sources or set(ALL_SOURCES)
        # The pool is queried with both readings of the concept so that the
        # slice can be rejected below without a second round trip.
        words = list(dict.fromkeys([term for term, _ in terms] + [term for term, _ in plain_terms]))

        # `search_text` is the row's own name plus the context that names it,
        # composed at build time by `core.search_text`. Searching it rather than
        # the name alone is what lets a question use a word the series does not:
        # no `TP.KTF*` name contains "faiz", its data group's title does.
        if "bulletin" in wanted:
            where = " OR ".join(["entity_key ILIKE ? OR search_fold ILIKE ?"] * len(words))
            params = [p for term in words for p in (f"%{term}%", f"%{term}%")]
            pooled += con.execute(
                "SELECT 'bulletin' AS source, dataset, entity_key AS key, entity_name AS name, unit, "
                "temporal_semantics, currencies, metrics, NULL AS tier, n_periods, search_fold, "
                "first_period::VARCHAR AS first_period, last_period::VARCHAR AS last_period "
                f"FROM bulletin_entities WHERE {where}", params).df().to_dict("records")

        if "macro" in wanted:
            where = " OR ".join(["series_code ILIKE ? OR search_fold ILIKE ?"] * len(words))
            params = [p for term in words for p in (f"%{term}%", f"%{term}%")]
            pooled += con.execute(
                "SELECT 'macro' AS source, datagroup AS dataset, series_code AS key, name_tr AS name, unit, "
                "temporal_semantics, NULL AS currencies, monthly_rule AS metrics, tier, NULL AS n_periods, "
                "search_fold, native_frequency, published_start AS first_period, published_end AS last_period "
                f"FROM macro_series WHERE {where}", params).df().to_dict("records")

        if "external" in wanted:
            # The external zone has no build-time `search_fold`: a source lands
            # at runtime, so the fold is computed here from the same fields the
            # other indexes fold at build time (name, location, url).
            where = " OR ".join(["series_key ILIKE ? OR name ILIKE ? OR coalesce(name_clean, '') ILIKE ?"]
                                * len(words))
            params = [p for term in words for p in (f"%{term}%",) * 3]
            try:
                external = con.execute(
                    "SELECT 'external' AS source, source_id AS dataset, series_key AS key, "
                    "coalesce(name_clean, name) AS name, name AS raw_name, location, url, unit, unit_verified, "
                    "temporal_semantics, NULL AS currencies, monthly_rule AS metrics, NULL AS tier, n_periods, "
                    "native_frequency, published_start::VARCHAR AS first_period, "
                    "published_end::VARCHAR AS last_period "
                    f"FROM external_series WHERE {where}", params).df().to_dict("records")
            except duckdb.CatalogException:
                external = []                       # a database built before the external zone existed
            for row in external:
                row["search_fold"] = fold(f"{row.get('raw_name') or ''} {row.get('location') or ''} "
                                          f"{row.get('url') or ''} dis kaynak external")
            pooled += external

        if "weekly" in wanted:
            where = " OR ".join(["search_fold ILIKE ?"] * len(words))
            pooled += con.execute(
                "SELECT 'weekly' AS source, dataset, entity_key AS key, entity_name AS name, "
                "'milyon TL' AS unit, 'stock' AS temporal_semantics, 'TL,FX,total' AS currencies, "
                "NULL AS metrics, NULL AS tier, NULL AS n_periods, search_fold, "
                "NULL AS first_period, NULL AS last_period "
                f"FROM weekly_items WHERE retired_on IS NULL AND ({where})",
                [f"%{term}%" for term in words]).df().to_dict("records")

        if "finturk" in wanted:
            where = " OR ".join(["metric ILIKE ? OR search_fold ILIKE ?"] * len(words))
            params = [p for term in words for p in (f"%{term}%", f"%{term}%")]
            pooled += con.execute(
                "SELECT 'finturk' AS source, dataset, metric AS key, metric_name AS name, unit, "
                "temporal_semantics, NULL AS currencies, NULL AS metrics, NULL AS tier, n_periods, "
                "search_fold, first_period::VARCHAR AS first_period, last_period::VARCHAR AS last_period "
                f"FROM finturk_metrics WHERE {where}", params).df().to_dict("records")
    finally:
        con.close()

    for candidate in pooled:
        for field_name, value in list(candidate.items()):
            if hasattr(value, "item"):
                candidate[field_name] = value.item()
            elif value is not None and str(value) == "nan":
                candidate[field_name] = None
        if isinstance(candidate.get("currencies"), str):
            candidate["currencies"] = candidate["currencies"].split(",")
    # Read off the question as asked, before the source words were stripped:
    # "il bazında" is both a source filter and the reason a FinTurk row ranks.
    province_named = wanted == {"finturk"} or names_a_province(query)
    for candidate in pooled:
        candidate["grain"] = candidate_grain(candidate)
        candidate["score"] = _score(candidate, terms, province_named)
        candidate["name_match"] = _published_name_match(candidate, query)
        candidate["entity_currency"] = entity_currency(candidate.get("name") or "")
        words = set(re.findall(r"\w+", fold(query)))
        candidate["dataset_match"] = sum(len(w) >= 4 and w in words
                                         for w in (candidate.get("dataset") or "").split("_"))
        candidate["dataset_requested"] = any(
            len(w) >= 4 and w not in fold(candidate.get("name") or "")
            and any(word.startswith(w) for word in words)
            for w in (candidate.get("dataset") or "").split("_")[-1:])
        if (currency and candidate["entity_currency"] == currency and candidate["dataset_requested"]
                and candidate.get("temporal_semantics") == "stock" and not candidate.get("currencies")):
            candidate["score"] += 15

    # An explicit grain in the question ("aylık", "haftalık") demotes every
    # other grain before anything is ranked; silence keeps every corpus in play.
    wanted_grain = frequency or query_grain(query)
    _apply_grain_policy(pooled, wanted_grain)

    lexical = sorted([c for c in pooled if c["score"] > 0], key=_rank_key)
    scored, fusion = _fuse_with_vectors(query, lexical, pooled, limit)

    # The slice is a filter, not a score: when the concept named a currency,
    # only series that publish that slice can answer it, and each carries the
    # slice for the fetch. If nothing publishes it (an EVDS rate has no
    # currency column; "TL mevduat faizi" is one series, not a slice), the
    # word was a qualifier of the concept and the plain ranking stands.
    if currency:
        scored = [c for c in scored if not c.get("entity_currency") or c["entity_currency"] == currency]
        # Excluded: a line that publishes a currency split without this
        # slice (the FX net position is `total` only). Kept beside the tagged
        # lines: series with no currency dimension at all -- an EVDS rate in
        # the same clause ("TL mevduat stokunu USD/TRY kuru ile") is still
        # what the clause asked for.
        sliced = [c for c in scored if currency in (c.get("currencies") or [])]
        if sliced and _names_the_currency(sliced[0], query):
            # "Yabancı Para Net Genel Pozisyonu" is a line whose own name
            # contains the currency words: the phrase is the entity, not a
            # slice of it. Rank the question as written.
            currency, sliced = None, []
            for candidate in pooled:
                candidate["score"] = _score(candidate, plain_terms, province_named)
            scored = sorted([c for c in pooled if c["score"] > 0], key=_rank_key)
        if sliced:
            for candidate in sliced:
                candidate["currency"] = currency
            scored = [c for c in scored if c.get("currency") == currency or not c.get("currencies")]

    # The province, like the currency, rides on the candidate that can use
    # it: every FinTurk row publishes every province, so the slice is a
    # filter for the fetch, never a reason to drop a candidate.
    if province:
        for candidate in scored:
            if candidate["source"] == "finturk":
                candidate["province"] = province

    # One concept, ranked by score alone.
    #
    # There used to be a per-corpus seat guarantee here as well as in
    # `discover_concepts`, and applying it twice broke it. The floor was
    # `max(2, limit // 3)` per source, which at the `limit=3` that
    # `discover_concepts` calls with reserves six seats for three -- so the
    # truncation kept the top-scoring of the *reserved* set rather than of the
    # ranking. Measured on "Takipteki Alacaklar (TGA / NPL) Oranı": the
    # published NPL ratio scored 22.38 and was dropped for a weekly row scoring
    # 8.31, which is how a question about a ratio the corpus publishes was
    # answered with "the data does not hold it".
    #
    # The guarantee itself is sound and it stays -- one clause loud enough to
    # fill every slot really does hide the row another clause asked for. But it
    # belongs to the merge across clauses, where the list the planner sees is
    # actually built, and not to the ranking of a single concept.
    # Basis is a constraint on rates, not on loan balances. Apply after dense
    # fusion too, so an embedding cannot reintroduce the opposite basis.
    basis = rate_basis_constraint or rate_basis(query)
    if basis:
        scored = [c for c in scored if _matches_rate_basis(c, basis)]
    scored.sort(key=_rank_key)
    ranked = scored[:limit]

    # A query naming both a rate and an amount ("faiz orani konut kredisi
    # hacmini") has RATIO_WORDS' blanket bonus pushing every % candidate up
    # and every TL/stock candidate down in `scored` -- correct for the rate
    # half, wrong for the amount half, and nothing in a single flat ranking
    # can be right for both at once. Rather than reweight the shared ranking
    # (which would just as easily demote the correct rate candidate), a
    # second pass scores the same pool with that one bonus cancelled and
    # reversed, and its top hit is guaranteed a seat -- the existing
    # `discover_concepts` "every clause's own first choice gets a seat"
    # guarantee, applied to a semantic role split within one clause instead
    # of a punctuation split across clauses.
    query_text_for_words = terms[0][0] if terms else ""
    # "Stok" in a published rate's parenthetical basis is not a request for
    # a monetary balance. Keep actual amount words outside that qualifier.
    amount_text = re.sub(r"\([^()]*\)", lambda m: " " if rate_basis(m.group()) else m.group(),
                         query_text_for_words)
    if RATIO_WORDS.search(query_text_for_words) and AMOUNT_WORDS.search(amount_text):
        # Not a handicap on the shared score -- the "ktf" alias alone puts a
        # 15-20 point gap between a rate series and the correct amount one,
        # so any fixed swing that still ranks them on the same scale leaves
        # the rate series on top regardless. The amount half of a mixed query
        # is answered by whichever monetary/stock candidate scored best on
        # its own terms, full stop, not by whichever candidate wins after a
        # bounded nudge.
        amount_candidates = [
            c for c in pooled if c.get("score", 0) > 0 and c.get("unit") != "%"
            and (str(c.get("unit") or "").endswith("TL") or c.get("temporal_semantics") == "stock")]
        if amount_candidates:
            amount_top = max(amount_candidates, key=lambda c: c["score"])
            already_kept = any(
                c["source"] == amount_top["source"] and c["key"] == amount_top["key"]
                and c.get("currency") == amount_top.get("currency") for c in ranked)
            if not already_kept:
                ranked = ranked[:max(0, limit - 1)] + [amount_top]

    return {"query": query, "terms_used": [t for t, _ in terms], "currency": currency,
            "province": province,
            "sources": sorted(named_sources) if named_sources else None,
            "requested_grain": wanted_grain, "requested_basis": basis, "retrieval": fusion,
            "n_candidates": len(ranked), "candidates": ranked}


# --------------------------------------------------------------------------
# Temporal grain
#
# Discovery used to be grain-blind, and that produced a worse failure than a
# bad ranking. Asked for monthly consumer credit, it returned weekly item 5688
# (`Tüketici Kredileri ve Bireysel Kredi Kartları`, whose latest observation is
# 2026-09-04) alongside monthly bulletin rows that end 2026-07 -- and the
# planner divided one by the other. Two different grains, two different
# vintages, two different scopes: a ratio computed from them is not a number
# with a large error bar, it is not a number at all.
#
# Grain is therefore a first-class field on every candidate, derived from the
# catalogue rather than guessed, and it gates the ranking before the planner
# ever sees the list.

GRAIN_BY_SOURCE = {"bulletin": "monthly", "weekly": "weekly", "finturk": "quarterly"}

# EVDS spells its frequencies out; these are the values `native_frequency`
# actually takes across the 44 registered groups, plus the external zone's.
FREQUENCY_TO_GRAIN = {
    "daily": "daily", "business_daily": "daily", "isgunu": "daily", "gunluk": "daily",
    "weekly": "weekly", "haftalik": "weekly",
    "monthly": "monthly", "aylik": "monthly",
    "quarterly": "quarterly", "ceyreklik": "quarterly",
    "semiannual": "semiannual", "annual": "annual", "yillik": "annual",
}

QUERY_GRAIN_WORDS = {
    "monthly": ("aylik", "aylık", "monthly", "ay bazinda", "ay bazında", "her ay"),
    "weekly": ("haftalik", "haftalık", "weekly", "hafta bazinda", "hafta bazında"),
    "daily": ("gunluk", "günlük", "daily", "gun bazinda", "gün bazında"),
    "quarterly": ("ceyrek", "çeyrek", "quarterly", "ceyreklik", "çeyreklik"),
    "annual": ("yillik", "yıllık", "annual", "yearly", "yil bazinda"),
}

# What a monthly question may still be answered with. A weekly series is a
# legitimate *answer* to a monthly question only after resampling, which the
# executor does on the way in -- but it must never outrank the monthly series
# that was actually asked for, and it must never be silently paired with one.
GRAIN_PENALTY = -10.0


def candidate_grain(candidate: Dict[str, Any]) -> str:
    """The observation grain of one candidate, from the catalogue."""
    source = str(candidate.get("source") or "")
    fixed = GRAIN_BY_SOURCE.get(source)
    if fixed:
        return fixed
    frequency = str(candidate.get("native_frequency") or "").strip().lower()
    return FREQUENCY_TO_GRAIN.get(frequency, "monthly" if frequency in ("", "none", "nan") else frequency)


def query_grain(query: str) -> Optional[str]:
    """The grain a question explicitly asks for, or None when it does not say.

    Explicit only: silence is not a request for monthly. A question that never
    mentions a period should keep every corpus in play, which is what lets
    "en guncel toplam kredi buyuklugu" reach the weekly bulletin at all.
    """
    text = fold(query or "")
    for grain, words in QUERY_GRAIN_WORDS.items():
        for word in words:
            if re.search(rf"(?<!\w){re.escape(fold(word))}(?!\w)", text):
                return grain
    return None


def _apply_grain_policy(candidates: List[Dict[str, Any]], wanted: Optional[str]) -> None:
    """Demote candidates whose grain contradicts an explicit request.

    A penalty rather than a filter: the weekly bulletin leads the monthly one
    by weeks, so a demoted series is still worth showing the planner when
    nothing better exists. It just cannot win a seat it was not asked for.
    """
    if not wanted:
        return
    for candidate in candidates:
        grain = candidate.get("grain") or candidate_grain(candidate)
        candidate["grain"] = grain
        # EVDS ("macro") is pulled at native frequency but published to the
        # agent through `macro_observations`, already aligned to months
        # (`transform.macro.align_monthly`) -- every series is deliverable at
        # monthly grain regardless of how often it is actually reported.
        # `candidate_grain` reads `native_frequency` for macro candidates
        # (there is no fixed entry for "macro" in GRAIN_BY_SOURCE, unlike
        # bulletin/weekly/finturk, each pinned to one real table), so a
        # weekly-native series like TP.KTF12 (the reference demo's own
        # housing-loan rate) was demoted -10 by an explicit "aylık" the same
        # way a genuinely wrong-vintage BDDK weekly item would be -- enough
        # to flip a close pair to its Stok counterpart, which happens to be
        # monthly-native. A non-monthly explicit request (haftalık/günlük)
        # still penalizes a macro candidate whose native frequency cannot
        # serve it natively.
        if wanted == "monthly" and candidate.get("source") == "macro":
            continue
        if grain != wanted:
            candidate["score"] = round(candidate.get("score", 0.0) + GRAIN_PENALTY, 3)
            candidate["grain_mismatch"] = f"{grain} != requested {wanted}"


# --------------------------------------------------------------------------
# Hybrid retrieval: dense recall over the lexical ranking, fused by rank.
#
# `DENSE_OVERSAMPLE` and `MAX_DENSE_PER_DATASET` are measured, not chosen:
# `bie_akonutsat2` held 93 of the dense top-120 for the reference demo question
# (EVDS repeats one series per province), so without a per-dataset cap the
# housing-loan rate never reached the fused list; a cap below 8 lost the
# consumer-NPL row instead. The dense weight is 0.0 by measurement too: every
# positive value cost pinned rankings and bought none back, because this
# lexical ranker encodes tested domain knowledge (aliases, tiers, qualifier
# penalties, the unit traps) that a generic embedding cannot see. So dense
# retrieval contributes RECALL -- rows the lexical SQL pool never saw -- and
# never ORDER. Raising the weight is a deliberate act: re-run the discovery
# eval first.
DENSE_OVERSAMPLE = 4
LEXICAL_WEIGHT = 1.0
DENSE_WEIGHT = 0.0
MAX_DENSE_PER_DATASET = 8
# Opt-in, because the dense half embeds every query through the model
# endpoint: measured on this machine, `discover` went from milliseconds to
# ~4.5 s with the index on disk and a key configured, and `discover_concepts`
# runs it once per clause. With DENSE_WEIGHT at 0.0 the half buys recall only,
# so it is off unless a deployment asks for it.
HYBRID_ENV = "KKB_HYBRID_DISCOVERY"


def hybrid_enabled() -> bool:
    return os.environ.get(HYBRID_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _diversify(hits, depth: int):
    """Cap how many dense hits one dataset contributes, keeping rank order."""
    kept, per_dataset = [], {}
    for hit in hits:
        dataset = str(hit.dataset)
        count = per_dataset.get(dataset, 0)
        if count >= MAX_DENSE_PER_DATASET:
            continue
        per_dataset[dataset] = count + 1
        kept.append(hit)
        if len(kept) >= depth:
            break
    return kept


def _identity(candidate: Dict[str, Any]):
    """What makes a candidate one row: `key` alone is not unique across datasets."""
    return (str(candidate.get("source")), str(candidate.get("dataset")), str(candidate.get("key")))


def rate_basis(text: str) -> Optional[str]:
    """Explicit stock/flow basis in a query or the publisher's rate label."""
    found = set(re.findall(r"\b(?:stok|akim)\b", fold(text)))
    return next(iter(found)) if len(found) == 1 else None


def _matches_rate_basis(candidate: Dict[str, Any], basis: Optional[str]) -> bool:
    return not (basis and candidate["source"] == "macro" and candidate.get("temporal_semantics") == "rate"
                and (rate_basis(candidate.get("name") or "") or rate_basis(candidate.get("search_fold") or ""))
                not in (None, basis))


def _published_name_match(candidate: Dict[str, Any], query: str) -> int:
    # Full names outrank aliases; short generic labels are not exact series
    # references. Normalise punctuation/spacing without losing any words.
    name = " ".join(re.findall(r"\w+", fold(candidate.get("name") or "")))
    body = " ".join(re.findall(r"\w+", fold(query)))
    if len(name.split()) >= 3 and f" {name} " in f" {body} ":
        return len(name)
    # A rate question may name just its product, leaving the basis to another
    # clause. The complete product heading (before publisher qualifiers) is
    # more specific than a combined consumer-loan rate mentioning that product.
    if candidate.get("source") == "macro" and candidate.get("temporal_semantics") == "rate" \
            and re.search(r"\bfaiz\w*\b|\brate\b", body):
        subject = " ".join(re.findall(r"\w+", fold(candidate.get("name") or "").split("(")[0]))
        if len(subject.split()) >= 2 and f" {subject} " in f" {body} ":
            return len(subject)
    return 0


def _rank_key(candidate: Dict[str, Any]):
    """Order by fused score when the dense half ran, by lexical score otherwise."""
    return (-candidate.get("name_match", 0), -candidate.get("rrf_score", 0.0),
            -candidate.get("score", 0.0))


def _fuse_with_vectors(query: str, lexical: List[Dict[str, Any]], pooled: List[Dict[str, Any]],
                       limit: int, depth: int = 30):
    """Dense recall over a lexically-ordered list.

    Degrades to pure lexical ranking whenever the dense side cannot run -- no
    index built, no embedder configured, a model outage, a stale index from a
    different embedding model. Discovery is load-bearing for every question the
    agent answers, so none of those may turn into an error. The lexical layer
    holds a veto: a candidate its gates disqualified (a grain the question did
    not ask for, a qualifier it never mentioned) is not resurrected on cosine.
    """
    report: Dict[str, Any] = {"lexical": len(lexical), "vector": 0, "mode": "lexical"}
    if not hybrid_enabled():
        report["dense"] = f"disabled (set {HYBRID_ENV}=1)"
        return lexical, report
    try:
        store = default_store()
        if not store.available():
            return lexical, report
        hits = _diversify(store.search(query, limit=depth * DENSE_OVERSAMPLE), depth)
    except Exception:                                        # noqa: BLE001 -- never break discovery
        return lexical, report
    if not hits:
        return lexical, report

    by_identity = {}
    for candidate in pooled:
        by_identity.setdefault(_identity(candidate), candidate)

    dense: List[Dict[str, Any]] = []
    dense_only: List[Any] = []
    for hit in hits:
        candidate = by_identity.get(hit.identity())
        if candidate is None:
            dense_only.append(hit)              # a row the lexical SQL pool never saw
            continue
        if candidate.get("score", 0.0) <= 0:
            continue                            # the lexical veto
        candidate["similarity"] = round(hit.similarity, 6)
        dense.append(candidate)

    fused = reciprocal_rank_fusion([lexical[:depth], dense], key=_identity,
                                   weights=[LEXICAL_WEIGHT, DENSE_WEIGHT])
    pool = {_identity(c): c for c in lexical[:depth]}
    pool.update({_identity(c): c for c in dense})
    for identity, score in fused.items():
        if identity in pool:
            pool[identity]["rrf_score"] = round(score, 8)
    merged = sorted(pool.values(), key=_rank_key)

    # The recall tail: only when the lexical pass came up short.
    if len(merged) < limit and dense_only:
        merged += _dense_only_candidates(dense_only, limit - len(merged))

    report.update({"vector": len(dense), "dense_only": len(dense_only),
                   "mode": "hybrid_rrf", "backend": store.backend(), "fused": len(merged)})
    return merged[:max(depth, limit)], report


_CATALOGUE_BY_IDENTITY: Optional[Dict[Any, Dict[str, Any]]] = None


def _dense_only_candidates(hits, room: int) -> List[Dict[str, Any]]:
    """Full catalogue rows for dense hits the lexical pool missed; `score`
    stays 0.0 so these always sort behind everything the lexical layer judged."""
    global _CATALOGUE_BY_IDENTITY
    if _CATALOGUE_BY_IDENTITY is None:
        try:
            _CATALOGUE_BY_IDENTITY = {_identity(row): row for row in catalogue_candidates()}
        except Exception:                                    # noqa: BLE001 -- recall is optional
            _CATALOGUE_BY_IDENTITY = {}
    admitted = []
    for hit in hits[:room]:
        row = _CATALOGUE_BY_IDENTITY.get(hit.identity())
        if row is None:
            continue
        admitted.append(dict(row, score=0.0, similarity=round(hit.similarity, 6), found_by="vector_only"))
    return admitted


def catalogue_candidates() -> List[Dict[str, Any]]:
    """Every indexable series, in the shape `discover` scores. The vector
    index is built from this, so the text an index holds is built from exactly
    the fields discovery would have scored."""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT 'bulletin' AS source, dataset, entity_key AS key, entity_name AS name, unit, "
            "temporal_semantics, 'monthly' AS native_frequency FROM bulletin_entities").df().to_dict("records")
        rows += con.execute(
            "SELECT 'macro' AS source, datagroup AS dataset, series_code AS key, name_tr AS name, unit, "
            "temporal_semantics, native_frequency FROM macro_series").df().to_dict("records")
        rows += con.execute(
            "SELECT 'weekly' AS source, dataset, entity_key AS key, entity_name AS name, "
            "'milyon TL' AS unit, 'stock' AS temporal_semantics, 'weekly' AS native_frequency "
            "FROM weekly_items WHERE retired_on IS NULL").df().to_dict("records")
        rows += con.execute(
            "SELECT 'finturk' AS source, dataset, metric AS key, metric_name AS name, unit, "
            "temporal_semantics, 'quarterly' AS native_frequency FROM finturk_metrics").df().to_dict("records")
        try:
            rows += con.execute(
                "SELECT 'external' AS source, source_id AS dataset, series_key AS key, "
                "coalesce(name_clean, name) AS name, unit, temporal_semantics, native_frequency "
                "FROM external_series").df().to_dict("records")
        except duckdb.CatalogException:
            pass
    finally:
        con.close()

    for row in rows:
        for field_name, value in list(row.items()):
            if hasattr(value, "item"):
                row[field_name] = value.item()
            elif value is not None and str(value) == "nan":
                row[field_name] = None
        row["key"] = str(row.get("key"))
        row["grain"] = candidate_grain(row)
    return rows


def fetch_series(
    key: str,
    source: str = "bulletin",
    dataset: Optional[str] = None,
    currency: Optional[str] = "total",
    metric: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    **kwargs,
) -> SeriesResult:
    """One series with its unit and semantics attached. Thin by design: the
    domain rules live in `series.load_series` so every tool inherits them."""
    return load_series(key, source=source, dataset=dataset, currency=currency,
                       metric=metric, start=start, end=end, **kwargs)


def footnotes(dataset: str) -> Dict[str, Any]:
    """BDDK's own methodology notes for one bulletin table, with the months
    each one covers.

    Three of these change what a sum means (table 05's bank loans and table
    08's repo securities sit outside their own totals; table 06 counts a
    multi-product customer once) and no arithmetic check can see that -- the
    published totals reconcile without those rows. This is the only lakehouse
    fact the agent needs that is text rather than a number, so it gets a
    narrow op rather than a SQL escape hatch.
    """
    con = _connect()
    try:
        frame = con.execute(
            "SELECT footnote, first_period, last_period, n_periods FROM bulletin_footnotes "
            "WHERE dataset = ? ORDER BY first_period", [dataset]).df()
    finally:
        con.close()
    notes = [{"footnote": str(row.footnote),
              "first_period": str(row.first_period)[:7], "last_period": str(row.last_period)[:7],
              "n_periods": int(row.n_periods)} for row in frame.itertuples()]
    sql = f"SELECT footnote, first_period, last_period FROM bulletin_footnotes WHERE dataset = '{dataset}'"
    return {"dataset": dataset, "n_notes": len(notes), "notes": notes,
            "citation": {"table": "bulletin_footnotes", "filters": {"dataset": dataset}, "sql": sql}}


def run_sql(sql: str, limit: int = 200) -> Dict[str, Any]:
    """The escape hatch, not the main road.

    Read-only connection, single statement, allowlisted tables, enforced LIMIT.
    A model that writes its own SQL against this corpus will eventually compare
    `bin TL` to `milyon TL`; the typed steps exist so it rarely has to.
    """
    statement = sql.strip().rstrip(";")
    if not statement.lower().lstrip("(").startswith(("select", "with")):
        raise ValueError("only SELECT / WITH statements are allowed")
    if ";" in statement:
        raise ValueError("only a single statement is allowed")
    if FORBIDDEN_SQL.search(statement):
        raise ValueError("statement contains a write or session-modifying keyword")

    referenced = set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][\w]*)", statement, re.I))
    unknown = {name for name in referenced if name.lower() not in ALLOWED_TABLES}
    if unknown:
        raise ValueError(f"table(s) not available to the agent: {sorted(unknown)}; "
                         f"allowed: {sorted(ALLOWED_TABLES)}")

    started = time.perf_counter()
    con = _connect()
    try:
        frame = con.execute(f"SELECT * FROM ({statement}) LIMIT {int(limit)}").df()
    finally:
        con.close()
    return {
        "sql": statement,
        "n_rows": int(len(frame)),
        "columns": [str(c) for c in frame.columns],
        "rows": frame.astype(object).where(frame.notna(), None).to_dict("records"),
        "seconds": round(time.perf_counter() - started, 3),
        "citation": {"table": sorted(referenced), "sql": statement, "n_rows": int(len(frame))},
    }
