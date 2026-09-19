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
             "read_url", "search", "chart", "ingest_external", "clear_table"]
Operation = Literal["index_to_base", "deflate", "change", "ratio"]
Method = Literal["anomaly", "changepoint", "causality"]
Kind = Literal["auto", "level", "trend", "volatility"]          # changepoint: what kind of change
Sensitivity = Literal["low", "medium", "high"]                  # changepoint: how eager to cut
Source = Literal["bulletin", "weekly", "macro"]
MonthlyRule = Literal["last", "avg", "sum"]

# Which fields each op actually needs. Checked after parsing, because guided
# decoding guarantees the shape and not the sense.
REQUIRED: dict = {
    "discover": ("query",),
    "fetch_series": ("key",),
    "transform": ("operation", "column"),
    "analyze": ("method",),          # column required too; resolved at Plan level (Plan._fill_analyze_columns)
    "find_periods": ("column",),
    "read_url": ("url",),
    "search": ("query",),
    "chart": (),
    "ingest_external": ("url", "value_column"),
    "clear_table": (),
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
    kind: Optional[Kind] = Field(None, description="changepoint only: volatility when the question is about "
                                                   "stability (dalgalanma/oynaklik); otherwise leave unset (auto)")
    sensitivity: Optional[Sensitivity] = Field(None, description="changepoint only: high to surface smaller "
                                                                 "shifts, low for only the major ones")
    direction: Optional[Literal["up", "down"]] = None
    against: Optional[str] = Field(None, description="second column for a coincidence question")
    against_direction: Optional[Literal["up", "down"]] = None

    # read_url / search / ingest_external
    url: Optional[str] = None
    query: Optional[str] = None

    # ingest_external only -- adds a column from an external file to the
    # CURRENT SESSION'S table only; nothing is written to the lakehouse.
    value_column: Optional[str] = Field(None, description="column in the external file holding the numbers")
    period_column: Optional[str] = Field(None, description="column holding the dates; auto-detected if omitted")
    sheet: Optional[str] = Field(None, description="Excel sheet name; ignored for CSV")
    monthly_rule: Optional[MonthlyRule] = Field(
        None, description="how to collapse several rows in one month: last (stock), avg (rate), sum (flow)")
    unit: Optional[str] = Field(None, description="the external column's unit, if it is stated near it")

    # chart
    columns: Optional[List[str]] = Field(None, description="columns to plot; omit for all")
    title: Optional[str] = None

    @model_validator(mode="after")
    def _has_required_fields(self) -> "Step":
        missing = [field for field in REQUIRED[self.op] if getattr(self, field, None) in (None, "")]
        if missing:
            raise ValueError(f"step op={self.op!r} is missing required field(s): {missing}")
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

    @model_validator(mode="after")
    def _fill_analyze_columns(self) -> "Plan":
        """An analyze step must name a column. Small models reliably forget
        this right after fetching the very series they mean; when the plan
        makes it unambiguous -- the most recent step that put a column on the
        table -- fill it in rather than fail the whole plan. Fail, with the
        same message as any other missing field, only when nothing precedes.
        """
        produced: list = []
        for i, step in enumerate(self.steps):
            if step.op in ("fetch_series", "transform", "ingest_external") and step.as_name:
                produced.append(step.as_name)
            elif step.op == "fetch_series" and step.key:
                produced.append(step.key)       # executor names an as_name-less fetch by its key
            elif step.op == "analyze" and not step.column:
                if not produced:
                    raise ValueError(f"step {i} op='analyze' is missing required field(s): ['column'] "
                                     "and no earlier step put a column on the table")
                step.column = produced[-1]
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
- analyze: method (anomaly, changepoint veya causality) ve column ZORUNLU. column = analiz edilecek
  sutunun adi, yani onceki fetch_series/transform adiminda verdigin as_name (ornegin "konut").
  changepoint icin: soru dalgalanma / oynaklik / istikrar hakkindaysa kind=volatility; aksi halde kind
  BOS birak (otomatik secilir). Kucuk kirilmalar da istenirse sensitivity=high, sadece buyuk kirilmalar
  istenirse sensitivity=low; varsayilan medium. causality icin ikinci sutun: against (veya other_column).
- chart: grafik ciz.
- read_url / search: prompt'ta URL varsa veya disaridan bilgi gerekiyorsa. read_url sadece OKUR
  (metin/onizleme dondurur), tabloya sutun EKLEMEZ.
- ingest_external: bir URL'deki Excel/CSV dosyasindan bir sutunu SAYISAL SERI olarak tabloya
  ekler -- boylece uzerinde transform/analyze/chart calisabilir. value_column ZORUNLU (hangi
  sutunun sayi oldugunu once read_url ile onizleyip ogren). period_column verilmezse otomatik
  bulunur. Bu ekleme SADECE bu oturum icindir, kalici veritabanina hicbir sey yazilmaz.
- clear_table: mevcut tabloyu (tum sutunlari) tamamen bosaltir. Kullanici "tabloyu temizle",
  "sil", "bastan basla", "yeni tablo yap" gibi bir sey isterse kullan. Bu adim SADECE bu
  oturumun bellekteki calisma tablosunu bosaltir -- lakehouse.duckdb'ye HICBIR ETKISI YOKTUR,
  onu silme/degistirme imkani yoktur ve olmayacaktir.

Kurallar:
1. Anahtari (key) kesin bilmiyorsan once discover kullan. Anahtar UYDURMA.
2. Mevcut tabloya ekleme yapiliyorsa ("bozmadan", "yeni sutun olarak") var olan sutunlari SILME,
   sadece yeni fetch_series/transform/ingest_external adimlari ekle. Kullanici acikca "sil"/
   "temizle"/"bastan basla" DEMEDIKCE clear_table KULLANMA.
3. Tarih araligini start/end alanlarina yaz (YYYY-MM-DD).
4. Grafik istenmisse son adim chart olsun.
5. BIRIME DIKKAT ET. Kredi/mevduat TUTARI istendiginde birimi "milyon TL" veya "bin TL"
   olan seriyi sec. Birimi "adet" olan seri bir SAYIDIR (ornegin konut SATIS adedi),
   kredi tutari degildir. Birimi "%" olan seri bir orandir.
6. Sutun adini as_name ile ver. Bir adimin kullanmadigi alanlari BOS BIRAK.
7. Kullanici disaridan bir dosya/URL'deki veriyi mevcut tabloyla KARSILASTIRMAK veya
   BIRLESTIRMEK istiyorsa ingest_external kullan; sadece OZETLEMESINI istiyorsa read_url yeter.
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