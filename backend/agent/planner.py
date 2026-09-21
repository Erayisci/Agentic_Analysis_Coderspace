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

from pydantic import BaseModel, ConfigDict, Field, model_validator

Intent = Literal["series_analysis", "followup", "url_analysis", "search", "metadata", "unsupported"]
Op = Literal["discover", "fetch_series", "transform", "analyze", "find_periods",
             "read_url", "search", "chart", "ingest_external", "clear_table", "footnotes"]
Operation = Literal["index_to_base", "deflate", "change", "ratio", "in_usd"]
Method = Literal["anomaly", "changepoint", "causality", "decompose"]
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
    "footnotes": (),
}


class Step(BaseModel):
    """One executable action. Only the fields its `op` needs are read.

    `extra="forbid"`: a field this model does not declare is a typo or a
    parameter nobody reads. Under guided decoding the schema already forbids
    it (`additionalProperties: false`); under the `json_object` fallback the
    step is dropped by `Plan._drop_invalid_steps` -- visible, unlike the
    silent discard that let a test pass `window=6` to a tool that never saw it.
    """

    model_config = ConfigDict(extra="forbid")

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
    against: Optional[str] = Field(None, description="second column: coincidence, predictor or price index")
    against_direction: Optional[Literal["up", "down"]] = None
    window: Optional[int] = Field(None, description="anomaly only: baseline length in months (default 12)")

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

    @model_validator(mode="before")
    @classmethod
    def _drop_invalid_steps(cls, data):
        """Keep the steps that validate; drop the ones that do not, and say so.

        Guided decoding guarantees the shape, not the sense: the model still
        writes a `transform` with `method` and no `operation`. Rejecting the
        whole plan for that cost a 45-second repair round that usually failed
        too, and then the deterministic fallback lost every step the model got
        right. Dropping the one bad step keeps the rest; the plan's `reasoning`
        records what was dropped so the audit trail shows it.
        """
        if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
            return data
        kept, dropped = [], []
        for raw in data["steps"]:
            # The model reliably writes `columns: [a, b]` on an analyze step
            # (the chart field) where the op reads `column`/`against`. Same
            # intent, wrong slot: map it rather than drop the step.
            if isinstance(raw, dict) and raw.get("op") == "analyze" and not raw.get("column") \
                    and isinstance(raw.get("columns"), list):
                named = [str(c) for c in raw["columns"] if c]
                raw = {k: v for k, v in raw.items() if k != "columns"}
                if named:
                    raw["column"] = named[0]
                if len(named) > 1 and not raw.get("against"):
                    raw["against"] = named[1]
            try:
                kept.append(Step.model_validate(raw) if isinstance(raw, dict) else raw)
            except ValueError as exc:
                op = raw.get("op") if isinstance(raw, dict) else "?"
                reason = next((line.strip() for line in str(exc).splitlines()
                               if "missing required field" in line), str(exc)[:120])
                dropped.append(f"{op} ({reason})")
        data = {**data, "steps": kept}
        if dropped:
            note = "dropped invalid step(s): " + "; ".join(dropped)
            data["reasoning"] = f"{data.get('reasoning') or ''} [{note}]".strip()
        return data

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
  change (periods=1 aylik, 12 yillik), ratio (other_column=payda), in_usd (TL tutari dolar bazina
  cevir, other_column=USD/TRY kuru).
- find_periods: bir sutunun dustugu/yukseldigi donemleri bul; against ile ikinci sutunla karsilastir.
- analyze: method + column (+against). Hangi method:
  anomaly     "anomali/aykiri/olagandisi hareket"       -> tek seri; column=ana seri.
  changepoint "kirilma/rejim degisikligi/yapisal degisim" -> tek seri; column=ana seri.
  causality   "onculuyor mu/nedensellik/etkiledi mi/Granger" -> column=hedef, against=aday oncu. IKI seri sart.
  decompose   "artmamasinin sebebi fiyat/enflasyon olabilir mi" -> column=nominal TUTAR, against=fiyat endeksi (KFE/TUFE).
- chart: grafik ciz.
- footnotes: bir bulletin tablosunun BDDK metodoloji notlari (dipnot/tanim/"neyi kapsar" sorulari).
  dataset verilmezse tablodaki serilerin tablolari kullanilir.
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
4. chart adimini SADECE kullanici acikca grafik/gorsel istediyse ("grafik", "ciz", "gorsellestir")
   ekle ve son adim olsun. Istenmediyse chart EKLEME -- cevap duz metin olacak.
5. BIRIME DIKKAT ET. Kredi/mevduat TUTARI istendiginde birimi "milyon TL" veya "bin TL"
   olan seriyi sec. Birimi "adet" olan seri bir SAYIDIR (ornegin konut SATIS adedi),
   kredi tutari degildir. Birimi "%" olan seri bir orandir.
   "YP mevduat" / "TL krediler" gibi para birimi dilimleri AYRI BIR KEY DEGILDIR: ayni key'i
   currency="FX" veya "TL" ile fetch et (aday satirindaki currency= degerini aynen kopyala).
6. Sutun adini as_name ile ver (kisa, tek kelime). Bir adimin kullanmadigi alanlari YAZMA:
   fetch_series icin sadece key, source, dataset, as_name; transform icin operation, column
   (+other_column/periods/base_period), as_name; find_periods icin column, direction, against,
   against_direction; analyze icin method, column (+against). unit/columns/title/window yazma
   (title sadece chart icin). KISA yaz: fazla alan = yavas cevap.
7. Kullanici disaridan bir dosya/URL'deki veriyi mevcut tabloyla KARSILASTIRMAK veya
   BIRLESTIRMEK istiyorsa ingest_external kullan; sadece OZETLEMESINI istiyorsa read_url yeter.
8. "X dustugu donemlerde Y nasil degisti / X dustugu halde Y yukselmedigi donem var mi" gibi
   sorular find_periods ile cevaplanir: column=X, direction=down, against=Y,
   against_direction=down (Y'nin YUKSELMEDIGI aylar). analyze/transform ile DEGIL.
9. "... sebebi fiyat/enflasyon artisi olabilir mi": fiyat endeksini fetch et (KFE: TP.KFE.TR,
   TUFE: TP.GENENDEKS.T1) + analyze decompose + find_periods (kural 8). causality SADECE
   "onculuyor mu / nedensellik / etkiledi mi" denirse.

Ornek 1 -- "konut kredisi tutari ve faizi; faiz dustugu halde kredi artmayan aylar var mi?":
{"intent":"series_analysis","start":"2021-01-01","end":"2025-12-01","steps":[
 {"op":"fetch_series","key":"tuketici_kredileri_konut","source":"bulletin","dataset":"tuketici_kredileri","as_name":"konut"},
 {"op":"fetch_series","key":"TP.KTF12","source":"macro","as_name":"faiz"},
 {"op":"find_periods","column":"faiz","direction":"down","against":"konut","against_direction":"down"}],
 "reasoning":"iki seri, sonra es zamanli hareket kontrolu"}
Ornek 2 -- "faiz dustugu halde kredilerin artmamasinin sebebi fiyat artisi olabilir mi?" (tablo: konut, faiz):
{"intent":"followup","steps":[
 {"op":"fetch_series","key":"TP.KFE.TR","source":"macro","as_name":"kfe"},
 {"op":"analyze","method":"decompose","column":"konut","against":"kfe"},
 {"op":"find_periods","column":"faiz","direction":"down","against":"konut","against_direction":"down"}],
 "reasoning":"fiyat endeksi eklenir, nominal/reel ayristirilir"}
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
    # The general shape: find what the question names. A chart is appended by
    # `pipeline.apply_presentation` only when the question asked for one.
    return Plan(intent="series_analysis", start=start, end=end,
                reasoning="deterministic fallback: discover",
                steps=[Step(op="discover", query=question)])
