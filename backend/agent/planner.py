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
Source = Literal["bulletin", "weekly", "macro"]
MonthlyRule = Literal["last", "avg", "sum"]

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
    direction: Optional[Literal["up", "down"]] = None
    against: Optional[str] = Field(None, description="second column for a coincidence question")
    against_direction: Optional[Literal["up", "down"]] = None

    # read_url / search / ingest_external
    url: Optional[str] = None
    query: Optional[str] = None

    # ingest_external adds a column and persists its observations in research.duckdb.
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


class URLPlan(Plan):
    """URL routing is already decided; choose actions without reclassifying it."""

    intent: Literal["url_analysis"] = "url_analysis"
    steps: List[Step] = Field(min_length=1, max_length=14)


PLANNER_SYSTEM = """Sen bir finansal veri analiz ajanisin. Turkiye bankacilik (BDDK) ve makro (TCMB EVDS) \
verilerini iceren bir lakehouse uzerinde calisiyorsun.

Gorevin: kullanicinin sorusunu ADIMLARA cevirmek. Hesaplama YAPMA, SQL YAZMA, sayi URETME. \
Sadece hangi adimlarin hangi sirayla calisacagini belirle.

Uygulama davranisi: fetch_series ve ingest_external ile eklenen sutunlar AYNI
calisma tablosunda tarih uzerinden birlesir ve web sitesinin Tablo sekmesinde
OTOMATIK gosterilir. ingest_external ayrica kaynak ve gozlemleri DuckDB'ye
OTOMATIK kaydeder. Tablo sekmesine koyma, birlestirme veya DuckDB'ye kaydetme
istekleri DESTEKLENIR; bunlar icin ayri GUI, SQL veya save adimi GEREKMEZ.
Farkli birimli seriler (ornegin milyon TL ve kg) AYRI sutunlarda yan yana
gosterilebilir; bunlari bolmek, ayni birime cevirmek veya tek metrik yapmak
gerekmez. fetch_series + ingest_external bu birlestirmeyi zaten yapar.

Adimlar:
- discover: bir kavramin lakehouse anahtarini bul (key). Anahtari bilmiyorsan ONCE bunu kullan.
- fetch_series: bir seriyi tabloya sutun olarak ekle. key ve source zorunlu.
- transform: operation ve column ZORUNLU. operation su degerlerden biridir:
  index_to_base (2021-01=100 gibi), deflate (enflasyondan arindirma, other_column=TUFE serisi),
  change (periods=1 aylik, 12 yillik), ratio (other_column=payda).
  Ornek: {"op":"transform","operation":"index_to_base","column":"kredi",
          "base_period":"2026-01-01","as_name":"kredi_endeksi"}.
  Her seri icin AYRI transform adimi kullan. column, onceki fetch_series veya
  ingest_external adiminin as_name degeriyle ayni olmalidir. Ham sutunlari korumak
  icin endeks sutununa FARKLI bir as_name ver. title, operation yerine GECMEZ.
- find_periods: bir sutunun dustugu/yukseldigi donemleri bul; against ile ikinci sutunla karsilastir.
- analyze: anomaly, changepoint veya causality.
- chart: grafik ciz.
- read_url / search: prompt'ta URL varsa veya disaridan bilgi gerekiyorsa. read_url sadece OKUR
  (metin/onizleme dondurur), tabloya sutun EKLEMEZ.
- ingest_external: bir URL'deki Excel/CSV veya desteklenen PDF dosyasindan bir sutunu SAYISAL SERI olarak tabloya
  ekler -- boylece uzerinde transform/analyze/chart calisabilir. value_column ZORUNLU (hangi
  sutunun sayi oldugunu once read_url ile onizleyip ogren). period_column verilmezse otomatik
  bulunur. Kaynak ve sayisal gozlemler research.duckdb'ye kalici kaydedilir.
  OKUNMUS DIS DOSYALAR varsa oradaki URL, columns ve units alanlarini aynen kullan.
  PDF'den sayilari kendin cikarma; desteklenen tablonun sutunlarini ingest_external ile al.
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
6. Sutun adini as_name ile ver. Bir adimin kullanmadigi alanlari JSON'a EKLEME;
   tum istege bagli alanlari null olarak tekrar etme. reasoning en fazla bir cumle olsun.
7. Kullanici disaridan bir dosya/URL'deki veriyi mevcut tabloyla KARSILASTIRMAK veya
   BIRLESTIRMEK istiyorsa ingest_external kullan; sadece OZETLEMESINI istiyorsa read_url yeter.
8. BDDK ve URL ayni sorudaysa hem fetch_series hem ingest_external gerekir. Web metni
   okumak BDDK sutununun yerine gecmez. Okunmamis baglanti/sutun uydurma: once read_url
   ve BDDK fetch_series planla; uygulama dosyayi kesfedip semayi verince tekrar planlayacak.
9. Veri tablosu, PDF okuma, veri kaydi ve endeksleme bu uygulamanin destekledigi islerdir.
   Bunlari unsupported diye reddetme. Anahtar eksikse discover; sema eksikse read_url sec.
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
