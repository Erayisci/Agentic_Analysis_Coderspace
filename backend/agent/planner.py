"""The plan DSL: the only language the model is allowed to speak.

A plan is a list of typed steps over a closed vocabulary. The model chooses
steps and arguments; it never writes SQL, never does arithmetic, and never
decides what a number means. The server's guided decoding constrains generation
to this schema token by token, so a syntactically invalid plan is not
reachable -- which is what makes a 27B model usable here at all.

The step model is deliberately **flat** rather than a discriminated union of
per-op models. Guided-decoding backends vary in how well they handle `$ref` and
`anyOf`, and a schema that a deployment silently mishandles is a failure mode
with no error message. One object with optional fields and a semantic validator
is uglier to read and far harder to get wrong.

`template_plan` is the deterministic fallback. When the model fails twice, or
when the router already knows what the question is, a hand-written plan runs
instead. An agent that cannot answer without the LLM having a good day is not
a system you can demo.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

Intent = Literal["series_analysis", "followup", "url_analysis", "search", "metadata", "unsupported"]
Op = Literal["discover", "fetch_series", "transform", "analyze", "find_periods",
             "read_url", "search", "chart"]
Operation = Literal["index_to_base", "deflate", "change", "ratio"]
Method = Literal["anomaly", "changepoint", "causality"]
Source = Literal["bulletin", "weekly", "macro"]

# Which fields each op actually needs. Checked after parsing, because guided
# decoding guarantees the shape and not the sense.
REQUIRED: dict = {
    "discover": ("query",),
    "fetch_series": ("key",),
    "transform": ("operation", "column"),
    "analyze": ("method", "column"),
    "find_periods": ("column",),
    "read_url": ("url",),
    "search": ("query",),
    "chart": (),
}


class Step(BaseModel):
    """One executable action. Only the fields its `op` needs are read."""

    op: Op = Field(description="which action to run")

    # fetch_series / discover
    key: Optional[str] = Field(None, description="entity_key (bulletin, weekly) or series_code (macro)")
    source: Optional[Source] = Field(None, description="which corpus the key belongs to")
    dataset: Optional[str] = Field(None, description="bulletin table slug, when the key is ambiguous")
    currency: Optional[str] = Field(None, description="'total' (TL+FX), 'TL' or 'FX'")
    metric: Optional[str] = Field(None, description="only for tables whose metric is a bucket")
    as_name: Optional[str] = Field(None, description="column name to store the result under")

    # transform
    operation: Optional[Operation] = Field(None, description="which transform to apply")
    column: Optional[str] = Field(None, description="column in the current table to operate on")
    other_column: Optional[str] = Field(None, description="deflator (deflate) or denominator (ratio)")
    base_period: Optional[str] = Field(None, description="YYYY-MM-DD base for indexing or deflating")
    periods: Optional[int] = Field(None, description="lag for change: 1 = MoM, 12 = YoY")

    # analyze / find_periods
    method: Optional[Method] = None
    direction: Optional[Literal["up", "down"]] = None
    against: Optional[str] = Field(None, description="second column for a coincidence question")
    against_direction: Optional[Literal["up", "down"]] = None

    # read_url / search
    url: Optional[str] = None
    query: Optional[str] = None

    # chart
    columns: Optional[List[str]] = Field(None, description="columns to plot; omit for all")
    title: Optional[str] = None

    @model_validator(mode="after")
    def _has_required_fields(self) -> "Step":
        missing = [
            field
            for field in REQUIRED[self.op]
            if getattr(self, field, None) in (None, "")
        ]

        if missing:
            raise ValueError(
                f"step op={self.op!r} is missing required field(s): {missing}"
            )

        if (
            self.op == "analyze"
            and self.method == "causality"
            and not (self.against or self.other_column)
        ):
            raise ValueError(
                "causality analysis requires against or other_column"
            )

        return self

    def arguments(self) -> dict:
        """Non-null fields other than `op`, for the audit trail."""
        return {k: v for k, v in self.model_dump().items() if k != "op" and v not in (None, [], "")}


class Plan(BaseModel):
    """A complete answer, expressed as steps."""

    intent: Intent
    start: Optional[str] = Field(None, description="window start, YYYY-MM-DD")
    end: Optional[str] = Field(None, description="window end, YYYY-MM-DD")
    steps: List[Step] = Field(default_factory=list, max_length=14)
    reasoning: Optional[str] = Field(None, description="one sentence, for the audit trail")

    @model_validator(mode="after")
    def _not_empty(self) -> "Plan":
        if self.intent != "unsupported" and not self.steps:
            raise ValueError("a plan must have at least one step unless intent is 'unsupported'")
        return self


PLANNER_SYSTEM = """Sen bir finansal veri analiz ajanisin. Turkiye bankacilik (BDDK) ve makro (TCMB EVDS) \
verilerini iceren bir lakehouse uzerinde calisiyorsun.

Gorevin: kullanicinin sorusunu ADIMLARA cevirmek. Hesaplama YAPMA, SQL YAZMA, sayi URETME. \
Sadece hangi adimlarin hangi sirayla calisacagini belirle.

Adimlar:
- discover: bir kavramin lakehouse anahtarini bul (key). Anahtari bilmiyorsan ONCE bunu kullan.
- fetch_series: bir seriyi tabloya sutun olarak ekle. key ve source zorunlu.
- transform: index_to_base (2021-01=100 gibi), deflate (enflasyondan arindirma, other_column=TUFE serisi),
  change (periods=1 aylik, 12 yillik), ratio (other_column=payda).
- find_periods: bir sutunun dustugu/yukseldigi donemleri bul; against ile ikinci sutunla karsilastir.
- analyze: anomaly, changepoint veya causality.
  causality icin column=hedef seri ve against=olasi neden/oncul seri olmalidir.
- chart: grafik ciz.
- read_url / search: prompt'ta URL varsa veya disaridan bilgi gerekiyorsa.

Kurallar:
1. Anahtari (key) kesin bilmiyorsan once discover kullan. Anahtar UYDURMA.
2. Mevcut tabloya ekleme yapiliyorsa ("bozmadan", "yeni sutun olarak") var olan sutunlari SILME,
   sadece yeni fetch_series/transform adimlari ekle.
3. Tarih araligini start/end alanlarina yaz (YYYY-MM-DD).
4. Grafik istenmisse son adim chart olsun.
5. BIRIME DIKKAT ET. Kredi/mevduat TUTARI istendiginde birimi "milyon TL" veya "bin TL"
   olan seriyi sec. Birimi "adet" olan seri bir SAYIDIR (ornegin konut SATIS adedi),
   kredi tutari degildir. Birimi "%" olan seri bir orandir.
6. Sutun adini as_name ile ver. Bir adimin kullanmadigi alanlari BOS BIRAK.
"""


def planner_messages(question: str, context: str) -> List[dict]:
    return [{"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user", "content": f"{context}\n\nSoru: {question}"}]


# --------------------------------------------------------------------- #
# Deterministic fallbacks
# --------------------------------------------------------------------- #

def template_plan(intent: Intent, question: str, urls: Optional[List[str]] = None,
                  start: Optional[str] = None, end: Optional[str] = None) -> Plan:
    """A plan that needs no model.

    Used when the planner fails validation twice, and as the router's direct
    answer for question shapes that have exactly one sensible plan. These are
    the paths that keep a demo alive when the endpoint is slow or the model is
    having a bad turn.
    """
    if intent == "url_analysis" and urls:
        return Plan(intent=intent, reasoning="deterministic: read each URL in the prompt",
                    steps=[Step(op="read_url", url=url) for url in urls])
    if intent == "search":
        return Plan(intent=intent, reasoning="deterministic: web search fallback",
                    steps=[Step(op="search", query=question)])
    if intent == "metadata":
        return Plan(intent=intent, reasoning="deterministic: discovery only",
                    steps=[Step(op="discover", query=question)])
    # The general shape: find what the question names, chart what came back.
    return Plan(intent="series_analysis", start=start, end=end,
                reasoning="deterministic fallback: discover then chart",
                steps=[Step(op="discover", query=question), Step(op="chart")])
