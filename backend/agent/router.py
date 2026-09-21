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
    r"search\s+(the\s+)?(web|internet)|web\s+search|research\s+online|latest\s+news)\b", re.I)

METADATA_PATTERN = re.compile(
    r"\b(hangi\s+(veri|tablo|seri|alan)|neler\s+var|listele|kapsam|hangi\s+dönemler|"
    r"what\s+(data|tables|series)|list\s+the)\b", re.I)

YEAR_RANGE = re.compile(r"\b(20\d{2})\s*[-–—/]\s*(20\d{2})\b")
SINGLE_YEAR = re.compile(r"\b(20\d{2})\b")
MONTH_STAMP = re.compile(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])\b")


class Route(BaseModel):
    """What the pipeline should do with this question."""

    intent: Intent
    urls: List[str] = Field(default_factory=list)
    start: Optional[str] = None
    end: Optional[str] = None
    is_followup: bool = False
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
    stamps = MONTH_STAMP.findall(question)
    if len(stamps) >= 2:
        (y1, m1), (y2, m2) = stamps[0], stamps[-1]
        return f"{y1}-{int(m1):02d}-01", f"{y2}-{int(m2):02d}-01"
    span = YEAR_RANGE.search(question)
    if span:
        return f"{span.group(1)}-01-01", f"{span.group(2)}-12-01"
    years = SINGLE_YEAR.findall(question)
    if len(years) >= 2:
        return f"{min(years)}-01-01", f"{max(years)}-12-01"
    if len(years) == 1:
        return f"{years[0]}-01-01", f"{years[0]}-12-01"
    return None, None


def route(question: str, has_artifact: bool = False, client=None) -> Route:
    """Classify a question. `client` is consulted only if the rules are silent."""
    urls = URL_PATTERN.findall(question or "")
    start, end = extract_window(question or "")
    followup = bool(FOLLOWUP_PATTERN.search(question or "")) and has_artifact

    if urls:
        return Route(intent="url_analysis", urls=urls, start=start, end=end,
                     is_followup=followup, reason="prompt contains a URL")
    if followup:
        return Route(intent="followup", start=start, end=end, is_followup=True,
                     reason="follow-up phrasing with a table already in session")
    if SEARCH_PATTERN.search(question or ""):
        return Route(intent="search", start=start, end=end, reason="asks for external information")
    if METADATA_PATTERN.search(question or ""):
        return Route(intent="metadata", start=start, end=end, reason="asks what data exists")

    if client is None:
        return Route(intent="series_analysis", start=start, end=end,
                     reason="no classifier available; defaulting to series analysis")
    try:
        decided = client.structured(
            [{"role": "system", "content": CLASSIFIER_SYSTEM}, {"role": "user", "content": question}],
            _IntentOnly, max_tokens=200)
        return Route(intent=decided.intent, start=start, end=end, is_followup=followup,
                     decided_by="llm", reason=decided.reason or "classified by model")
    except LLMError as exc:
        # The classifier is an optimisation, not a dependency.
        return Route(intent="series_analysis", start=start, end=end,
                     reason=f"classifier unavailable ({exc}); defaulting to series analysis")
