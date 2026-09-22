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
# Turkish is agglutinative, so the table is "tabloyu", "tablonun", "tabloya",
# "tablodaki" -- a `\b` right after "tablo" matched none of them, and "aynı
# tabloya enflasyonu ekle" / "bu tablonun grafiğini çiz" were read as fresh
# questions. The suffix is allowed (`\w*`); the phrase is what identifies a
# follow-up, not its case ending.
FOLLOWUP_PATTERN = re.compile(
    r"(bozmadan|bozmaks[ıi]z[ıi]n|ayn[ıi]\s+(tablo|grafi)\w*|bu\s+(tablo|grafi)\w*|"
    r"mevcut\s+(tablo|grafi)\w*|tablo(ya|nun|daki|dan)\b|grafi[ğg]e\s+ekle|"
    r"yeni\s+bir?\s+s[üu]tun|s[üu]tun\s+olarak\s+ekle|[üu]st[üu]ne\s+ekle|"
    r"without\s+(disturbing|changing|breaking)|add\s+(a\s+)?(new\s+)?column|same\s+(table|chart))", re.I)

# The words a bare presentation request is made of: "bunun grafiğini çizer
# misin", "tablo yap", "bunu grafik olarak göster". None of them names a
# series. When nothing else is left of the question and a table already
# exists, the user means THAT table -- the question is a follow-up that only
# changes how the table is shown, and must not be planned on its own words
# (which discovery cannot match, or worse, matches to the wrong series: the
# stem of "tabloyu" is a substring of the EVDS code TP.HPBITABLO1).
PRESENTATION_WORDS = re.compile(
    r"(grafi[kğg]\w*|çiz\w*|ciz\w*|görselle\w*|gorselle\w*|plot\w*|chart\w*|graph\w*|visuali[sz]e\w*|"
    r"tablo\w*|s[üu]tun\w*|listele\w*|d[öo]k\w*|table|column|list|show|"
    r"g[öo]ster\w*|yap\w*|ver\w*|getir\w*|olu[şs]tur\w*|haz[ıi]rla\w*|d[üu]zenle\w*|"
    r"bunun|bunu|bunlar\w*|[şs]unu|onu|onlar\w*|hepsi\w*|t[üu]m[üu]n[üu]|t[üu]m|bu|[şs]u|o|ayn[ıi]|mevcut|halinde|olarak|[şs]eklinde|"
    r"olsun|[şs]imdi|yeni|yine|hemen|sadece|bir\s+de|l[üu]tfen|misin\w*|m[ıi]s[ıi]n\w*|musun\w*|m[üu]s[üu]n\w*|"
    r"bar|çubuk|cubuk|çizgi|cizgi|pasta\w*|pie|dilim\w*|line|make|draw|as|a|the|it|them|these|those|this|that|please|"
    r"[üu]zerine|ekle\w*|tekrar|yeniden|için|icin|ve|da|de|ile)", re.I)


TR_MONTHS = ["ocak", "subat", "mart", "nisan", "mayis", "haziran", "temmuz", "agustos",
             "eylul", "ekim", "kasim", "aralik"]
MONTH_NAME = re.compile(
    r"\b(ocak|[şs]ubat|mart|nisan|may[ıi]s|haziran|temmuz|a[ğg]ustos|eyl[üu]l|ekim|kas[ıi]m|aral[ıi]k)\w*", re.I)
SINGLE_MONTH = re.compile(
    r"\b(20\d{2})[-/](0?[1-9]|1[0-2])\b"                                                   # 2024-06
    r"|\b(20\d{2})\s+(ocak|[şs]ubat|mart|nisan|may[ıi]s|haziran|temmuz|a[ğg]ustos|eyl[üu]l|ekim|kas[ıi]m|aral[ıi]k)\w*"  # 2024 Haziran
    r"|\b(ocak|[şs]ubat|mart|nisan|may[ıi]s|haziran|temmuz|a[ğg]ustos|eyl[üu]l|ekim|kas[ıi]m|aral[ıi]k)\w*\s+(20\d{2})\b",  # Haziran 2024
    re.I)


def extract_single_month(question: str):
    """'2024-06' / '2024 Haziran' / 'Haziran 2024' -> '2024-06-01', or None
    when the question names no month or more than one. Used for a snapshot
    (a pie), where the month is a point, not a window: `extract_window`
    deliberately reads a lone stamp as its whole year."""
    from ..core.labels import fold
    found = SINGLE_MONTH.findall(question or "")
    if len(found) != 1:
        return None
    y1, m1, y2, name2, name3, y3 = found[0]
    if y1:
        return f"{y1}-{int(m1):02d}-01"
    year = y2 or y3
    name = fold(name2 or name3)
    month = next((i + 1 for i, tr in enumerate(TR_MONTHS) if name.startswith(tr)), None)
    return f"{year}-{month:02d}-01" if month else None


def is_presentation_only(question: str) -> bool:
    """Does the question ask only HOW to show something, naming no series?

    "grafiğini çiz", "tablo yap", "bunu grafik olarak göster" -> True.
    "konut kredilerini grafik olarak çiz" -> False ("konut kredilerini" is
    left over, so it is a real question that also wants a chart).
    """
    if not question or not (CHART_PATTERN.search(question) or TABLE_PATTERN.search(question)
                            or SHOW_VERB.search(question)):
        return False
    # A date is not a subject: "2024 Haziran icin pasta grafigi" is still
    # only a presentation request (the month is a parameter of it).
    stripped = MONTH_NAME.sub(" ", question)
    # Whole words only: "bu" must not eat the "bu" inside "bulten".
    for word in re.findall(r"[\w'’]+", question, re.UNICODE):
        if PRESENTATION_WORDS.fullmatch(word.strip("'’")):
            stripped = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)", " ", stripped, count=1)
    leftover = [w for w in re.findall(r"\w+", stripped) if len(w) > 2 and not w.isdigit()]
    return not leftover

SEARCH_PATTERN = re.compile(
    r"\b(haberler|son\s+geli[şs]me|internetten|web'?den|ara[şs]t[ıi]r|güncel\s+haber|"
    r"search\s+the\s+web|latest\s+news)\b", re.I)

# "listele" is NOT here: "konut kredilerini listele" asks to see the figures
# (TABLE_PATTERN), and routing it to `metadata` produced a discover-only plan
# and no table. "Hangi verileri listeleyebilirsin" still lands here through
# `hangi veri`.
METADATA_PATTERN = re.compile(
    r"\b(hangi\s+(veri|tablo|seri|alan)|neler\s+var|kapsam|hangi\s+dönemler|"
    r"what\s+(data|tables|series)|list\s+the)\b", re.I)

# A chart or a table is produced only when the question asks for one. Without
# this every series question ended in a chart step and a 67-row table the user
# never asked to see; the answer is prose unless one of these words appears.
CHART_PATTERN = re.compile(
    r"(grafi[kğg]|çiz|ciz|görselle[şs]tir|gorselle[şs]tir|plot|chart|graph|visuali[sz]e|"
    # "pasta" only as a chart word -- "kredi pastasindan pay" is an idiom, not a request.
    r"\bpasta(s[ıi]|y[ıi]|n[ıi])?\b|\bpie\b)", re.I)
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

# A bare keyword match cannot tell "nedensellik var mı" (a question) from
# "nedensellik iddia etme" (an explicit prohibition) -- both contain
# "nedensellik". Measured live: a question that named its own guardrail
# ("aralarında nedensellik iddia etme") still triggered the causality step.
# Deliberately narrow -- only the handful of phrasings that explicitly forbid
# or disclaim a claim, never a general negation word -- because Turkish
# negation is not reliably keyword-local ("değil mi" asks for confirmation,
# it does not negate) and a broad check would suppress real causality
# questions phrased with an ordinary negative word nearby.
CAUSALITY_DISCLAIMED = re.compile(
    r"(iddia\s+etme|iddia\s+edilemez|ileri\s+s[üu]rme|sanma|varsayma|"
    r"nedensellik\s+(yok|de[ğg]ildir)|do not\s+claim|don'?t\s+claim)", re.I)

# An explicit request to empty the working table. Deliberately narrow: a
# bare "sil" is "npl sutununu sil", which must NOT wipe the table; only the
# table as a whole ("tabloyu temizle", "bastan basla", "her seyi sil") counts.
CLEAR_PATTERN = re.compile(
    r"(tablo\w*\s+(temizle|sil|bo[şs]alt|s[ıi]f[ıi]rla)|\btemizle\b|ba[şs]tan\s+ba[şs]la\w*|"
    r"(her\s*[şs]eyi|hepsini|t[üu]m[üu]n[üu])\s+(sil|temizle)|\bs[ıi]f[ıi]rla\w*|"
    r"clear\s+(the\s+)?table|start\s+over|\breset\b)", re.I)

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
    presentation_only: bool = False   # "grafiğini çiz" / "tablo yap": re-present the table, fetch nothing
    wants_clear: bool = False         # "tabloyu temizle" / "bastan basla": empty the working table first
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
    if CAUSALITY_DISCLAIMED.search(question):
        pass  # the question forbids a causal claim; do not plan the test at all
    elif ANALYSIS_PATTERNS["causality_strong"].search(question):
        found.append("causality")
    elif ANALYSIS_PATTERNS["causality_weak"].search(question):
        found.append("decompose" if ANALYSIS_PATTERNS["price"].search(question) else "causality")
    return found


def route(question: str, has_artifact: bool = False, client=None) -> Route:
    """Classify a question. `client` is consulted only if the rules are silent."""
    question = question or ""
    urls = URL_PATTERN.findall(question)
    start, end = extract_window(question)
    # "bunun grafiğini çiz" / "tablo yap" name no series: with a table in the
    # session they can only mean that table, so they are follow-ups that only
    # change the presentation. Without a table they fall through to the
    # ordinary path and get the "nothing to show" answer.
    presentation_only = has_artifact and is_presentation_only(question)
    wants_clear = bool(CLEAR_PATTERN.search(question))
    # A clear is about the table on screen, so alone it is a follow-up; with
    # a new question attached ("tabloyu temizle ve mevduati goster") the rest
    # is planned as a fresh question and the clear runs first.
    clear_only = wants_clear and not [w for w in re.findall(r"\w+", CLEAR_PATTERN.sub(" ", question))
                                      if len(w) > 2 and not PRESENTATION_WORDS.fullmatch(w)]
    followup = has_artifact and (bool(FOLLOWUP_PATTERN.search(question)) or presentation_only or clear_only)
    wants_chart = bool(CHART_PATTERN.search(question))
    # A follow-up extends a table the user already asked for, so it keeps it.
    wants_table = wants_a_table(question) or followup
    analyses = wanted_analyses(question)
    common = dict(start=start, end=end, is_followup=followup, presentation_only=presentation_only,
                  wants_clear=wants_clear,
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