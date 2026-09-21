"""Decides what kind of question this is, cheaply and mostly without a model.

Everything the router can settle with a regex, it settles with a regex. A URL
in the prompt means the URL tool runs -- no model needed to notice that. A
follow-up phrased "bozmadan" against an existing table is a follow-up whatever
a classifier thinks. This removes the model from the decisions it is worst at
and makes the common paths free and deterministic.

The LLM is consulted only when the deterministic signals are silent, and even
then its answer is one of six labels chosen by guided decoding, not free text.
"""
import re
from typing import List, Optional

from pydantic import BaseModel, Field

from ..llm import LLMError
from .planner import Intent

URL_PATTERN = re.compile(r"https?://[^\s<>\"'\)]+", re.I)

# Turkish and English ways of saying "keep the table and add to it". The
# reference scenario's turns 2 and 3 are both phrased this way, and reading
# them as new questions is what produces a silently different table.
FOLLOWUP_PATTERN = re.compile(
    r"\b(bozmadan|bozmaks[ıi]z[ıi]n|ayn[ıi]\s+tablo|bu\s+tabloy[ua]|tabloya\s+ekle|"
    r"yeni\s+bir?\s+s[üu]tun|s[üu]tun\s+olarak\s+ekle|ayn[ıi]\s+grafi|üstüne\s+ekle|"
    r"without\s+(disturbing|changing|breaking)|add\s+(a\s+)?(new\s+)?column|same\s+table)\b", re.I)

SEARCH_PATTERN = re.compile(
    r"\b(haberler|son\s+geli[şs]me|internetten|web'?den|ara[şs]t[ıi]r|güncel\s+haber|"
    r"search\s+the\s+web|latest\s+news)\b", re.I)

METADATA_PATTERN = re.compile(
    r"\b(hangi\s+(veri|tablo|seri|alan)|neler\s+var|listele|kapsam|hangi\s+dönemler|"
    r"what\s+(data|tables|series)|list\s+the)\b", re.I)

# A chart or a table is produced only when the question asks for one. Without
# this every series question ended in a chart step and a 67-row table the user
# never asked to see; the answer is prose unless one of these words appears.
CHART_PATTERN = re.compile(
    r"(grafi[kğg]|çiz|ciz|görselle[şs]tir|gorselle[şs]tir|plot|chart|graph|visuali[sz]e)", re.I)
# "aylık olarak gösteriniz" asks to see the monthly figures, which is a table.
TABLE_PATTERN = re.compile(
    r"(tablo|s[üu]tun|listele|d[öo]k(?:üm|um)|table|column|list\s+(the|all)|\bshow\b)", re.I)
# "göster" is a request to display something -- except in "değişim göstermiş",
# "tepki vermiş/göstermiş", "artış gösterdi", where it is the verb "exhibit"
# and the question wants prose. Both phrasings are common in the same
# question, so the collocations are removed before the verb is looked for
# rather than weighed against it.
SHOW_VERB = re.compile(r"g[öo]ster", re.I)
SHOW_AS_EXHIBIT = re.compile(
    r"(de[ğg]i[şs]im|de[ğg]i[şs]iklik|art[ıi][şs]|azal[ıi][şs]|d[üu][şs][üu][şs]|y[üu]kseli[şs]|"
    r"tepki|performans|e[ğg]ilim|trend|b[üu]y[üu]me|geli[şs]im|seyir|direnç|benzerlik|farkl[ıi]l[ıi]k)"
    r"\s+g[öo]ster", re.I)


def wants_a_table(question: str) -> bool:
    """Does the question ask to SEE the figures, or only to be told about them?"""
    if TABLE_PATTERN.search(question):
        return True
    return bool(SHOW_VERB.search(SHOW_AS_EXHIBIT.sub(" ", question)))


# Which analysis tool a question is asking for, decided by vocabulary. Measured
# before this existed: the production configuration never emitted an `analyze`
# step for the anomaly or changepoint eval questions -- one line in the
# planner prompt was not enough signal for a 27B model. The words are the
# signal, so the router reads them and `pipeline.apply_analysis` guarantees
# the step. Turkish with and without diacritics, plus English.
ANALYSIS_PATTERNS = {
    "anomaly": re.compile(
        r"(anomali|ayk[ıi]r[ıi]|ola[ğg]and[ıi][şs][ıi]|s[ıi]ra\s*d[ıi][şs][ıi]|u[çc]\s*de[ğg]er|"
        r"beklenmedik|outlier|anomal)", re.I),
    "changepoint": re.compile(
        r"(k[ıi]r[ıi]lma|rejim|yap[ıi]sal|trend\s+de[ğg]i[şs]|kopu[şs]|de[ğg]i[şs]im\s+noktas|"
        r"regime|structural|change\s*point|\bbreaks?\b)", re.I),
    # "etkiliyor mu" / "etkilediğini" / "neden-sonuç" / "öncü gösterge" were
    # measured live as phrasings the planner answered with a chart and no
    # test; they belong here, where `apply_analysis` guarantees the step.
    "causality_strong": re.compile(
        r"(nedensellik|neden[- ]sonu[çc]|granger|[öo]nc[üu]l?\b|[öo]nc[üu]l[üu]yor|[öo]nc[üu]\s*g[öo]sterge|"
        r"etkile(?:di|r)\s*mi|etkiliyor\s*mu|etkiledi[ğg]ini|etkisi\s+var\s*m[ıi]|"
        r"yol\s+a[çc]|lead[- ]lag|causal|\bcause)", re.I),
    "causality_weak": re.compile(r"(\bneden\b|sebe[bp]|olabilir\s*mi|\bwhy\b)", re.I),
    "price": re.compile(r"(fiyat|enflasyon|t[üu]fe|kfe|endeks|price|inflation)", re.I),
}

FOOTNOTE_PATTERN = re.compile(
    r"(dipnot|metodoloji|tan[ıi]m[ıi]|nas[ıi]l\s+hesaplan|kapsam\s+d[ıi][şs][ıi]|footnote|methodolog)", re.I)

# One token per date mention, in reading order: "2021-03", "2021 sonundan",
# "2024 yılının başında", or a bare "2024". The month is resolved here, in
# Python, and never by the model: "2021 sonu" is December, not "sometime in
# 2021", and a plan that read it as January returned eleven months nobody
# asked for. "2021-2025" is two bare years, not a month stamp -- the stamp
# branch needs a word boundary after a one- or two-digit month.
DATE_TOKEN = re.compile(
    r"\b(20\d{2})"
    r"(?:[-/](0?[1-9]|1[0-2])\b"
    r"|(?:\s*y[ıi]l[ıi]n?[ıi]?n?)?\s*(sonu\w*|ba[şs][ıi]\w*|ortas[ıi]\w*|ilk\s+yar[ıi]s[ıi]\w*|ikinci\s+yar[ıi]s[ıi]\w*)"
    # A bare year with its case suffix: "2021'den itibaren" opens a window,
    # "2024'e kadar" closes one. Without this the year read as a closed
    # calendar year and "2021'den itibaren" returned twelve months.
    r"|['’](d[ae]n|t[ae]n|y?[ae]|n[ae])(?![\w])"
    r")?", re.I)
FROM_SUFFIX = re.compile(r"(dan|den|tan|ten)$", re.I)
TO_SUFFIX = re.compile(r"(na|ne|ya|ye|^[ae])$", re.I)


class Route(BaseModel):
    """What the pipeline should do with this question."""

    intent: Intent
    urls: List[str] = Field(default_factory=list)
    start: Optional[str] = None
    end: Optional[str] = None
    is_followup: bool = False
    wants_chart: bool = False
    wants_table: bool = False
    wants_analysis: List[str] = Field(default_factory=list)   # anomaly | changepoint | causality | decompose
    wants_footnotes: bool = False
    decided_by: str = "rules"
    reason: str = ""


class _IntentOnly(BaseModel):
    """The narrow question put to the model when the rules are silent."""

    intent: Intent
    reason: str = Field("", description="kisa gerekce")


CLASSIFIER_SYSTEM = """Bir soruyu siniflandiriyorsun. SADECE etiketi sec:
- series_analysis: lakehouse'daki bir zaman serisi hakkinda (kredi, mevduat, faiz, enflasyon, konut...)
- followup: onceki tabloyu degistirmeden genisletme istegi
- url_analysis: prompt'ta verilen bir URL'nin icerigi isteniyor
- search: guncel/dis bilgi, internet aramasi gerekiyor
- metadata: hangi veriler/tablolar/seriler var sorusu
- unsupported: yukaridakilerin hicbiri
"""


def extract_window(question: str):
    """(start, end) as YYYY-MM-DD, from whatever date language the question uses.

    "2021-2025" in the reference scenario means 2021-01 through 2025-12, which
    is 60 months -- the count the brief itself states, and a useful check that
    this read is the intended one.
    """
    tokens = []                                   # (year, start_month, end_month, qualifier)
    for year, month, qualifier, suffix in DATE_TOKEN.findall(question or ""):
        year = int(year)
        if month:
            tokens.append((year, int(month), int(month), ""))
        elif suffix:
            tokens.append((year, 1, 12, suffix.lower()))
        elif qualifier:
            word = qualifier.lower()
            if word.startswith("son"):
                span = (12, 12)
            elif word.startswith("ba"):
                span = (1, 1)
            elif word.startswith("orta"):
                span = (6, 6)
            elif word.startswith("ilk"):
                span = (1, 6)
            else:
                span = (7, 12)
            tokens.append((year, span[0], span[1], word))
        else:
            tokens.append((year, 1, 12, ""))
    if not tokens:
        return None, None

    if len(tokens) == 1:
        year, first, last, word = tokens[0]
        # A lone qualified year says where the window opens or closes:
        # "2021 sonundan itibaren" opens in December, "2024 sonuna kadar"
        # closes there. A lone month stamp keeps the old reading (the year).
        if word and FROM_SUFFIX.search(word):
            return f"{year}-{first:02d}-01", None
        if word and TO_SUFFIX.search(word):
            return None, f"{year}-{last:02d}-01"
        if not word:
            return f"{year}-01-01", f"{year}-12-01"
        return f"{year}-{first:02d}-01", f"{year}-{last:02d}-01"

    earliest = min(tokens, key=lambda t: (t[0], t[1]))
    latest = max(tokens, key=lambda t: (t[0], t[2]))
    return f"{earliest[0]}-{earliest[1]:02d}-01", f"{latest[0]}-{latest[2]:02d}-01"


def wanted_analyses(question: str) -> List[str]:
    """Which analysis methods the question's own words ask for, in plan order.

    "Sebebi fiyat artışı olabilir mi" is a decomposition question (nominal =
    price x real), not a Granger question: the weak causal words with a price
    word go to `decompose`, and only the strong ones ("öncülüyor mu",
    "nedensellik", "Granger") ask for a lead-lag test.
    """
    found = [m for m in ("anomaly", "changepoint") if ANALYSIS_PATTERNS[m].search(question)]
    if ANALYSIS_PATTERNS["causality_strong"].search(question):
        found.append("causality")
    elif ANALYSIS_PATTERNS["causality_weak"].search(question):
        found.append("decompose" if ANALYSIS_PATTERNS["price"].search(question) else "causality")
    return found


def route(question: str, has_artifact: bool = False, client=None) -> Route:
    """Classify a question. `client` is consulted only if the rules are silent."""
    question = question or ""
    urls = URL_PATTERN.findall(question)
    start, end = extract_window(question)
    followup = bool(FOLLOWUP_PATTERN.search(question)) and has_artifact
    wants_chart = bool(CHART_PATTERN.search(question))
    # A follow-up extends a table the user already asked for, so it keeps it.
    wants_table = wants_a_table(question) or followup
    analyses = wanted_analyses(question)
    common = dict(start=start, end=end, is_followup=followup,
                  wants_chart=wants_chart, wants_table=wants_table,
                  wants_analysis=analyses, wants_footnotes=bool(FOOTNOTE_PATTERN.search(question)))

    if urls:
        return Route(intent="url_analysis", urls=urls, reason="prompt contains a URL", **common)
    if followup:
        return Route(intent="followup", reason="follow-up phrasing with a table already in session",
                     **common)
    if SEARCH_PATTERN.search(question):
        return Route(intent="search", reason="asks for external information", **common)
    # An analysis request is a series question whatever else it says: "anomali
    # analizi yap, listele" is not a metadata question, and the classifier's
    # 2.5s round trip cannot improve on the words already read.
    if analyses:
        return Route(intent="series_analysis", reason=f"asks for analysis: {', '.join(analyses)}",
                     **common)
    if METADATA_PATTERN.search(question):
        return Route(intent="metadata", reason="asks what data exists", **common)

    if client is None:
        return Route(intent="series_analysis",
                     reason="no classifier available; defaulting to series analysis", **common)
    try:
        decided = client.structured(
            [{"role": "system", "content": CLASSIFIER_SYSTEM}, {"role": "user", "content": question}],
            _IntentOnly, max_tokens=200)
        return Route(intent=decided.intent, decided_by="llm",
                     reason=decided.reason or "classified by model", **common)
    except LLMError as exc:
        # The classifier is an optimisation, not a dependency.
        return Route(intent="series_analysis",
                     reason=f"classifier unavailable ({exc}); defaulting to series analysis", **common)
