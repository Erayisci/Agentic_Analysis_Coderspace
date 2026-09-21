"""Wires the five stages into one turn, and holds sessions across turns.

    route -> plan -> execute -> verify -> compose

Written as plain functions rather than a LangGraph graph because the control
flow genuinely is a line with one bounded repair edge. A graph framework buys
persistence and conditional routing that this shape does not need yet; when a
stage grows a real loop, `run_turn` is the function a graph node would wrap.

The planner is given a *context block* rather than a bare question: the
lakehouse keys that discovery already found, and the columns the current table
holds. A 27B model asked to plan without that invents keys; asked to choose
among candidates it was handed, it mostly picks correctly.
"""
import logging
import re
import time
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional

from ..llm import KloudeksClient, LLMError
from ..tools.lakehouse import discover_concepts
from .composer import compose
from .executor import Executor, _column_name, _normalise_key
from .planner import Plan, Step, planner_messages, template_plan
from .router import Route, route
from .state import Session
from .verifier import verify

# Six was too few: a question with several interest-rate clauses filled every
# slot with rate series and the planner never saw the loan series it was asked
# about. Twelve costs ~200 prompt tokens and gives each corpus four seats.
MAX_CANDIDATES_IN_CONTEXT = 12

logger = logging.getLogger("kkb.agent")


@contextmanager
def _timed(timings: Dict[str, float], stage: str) -> Generator[None, None, None]:
    """Record how long a stage took, in `timings` and in the log.

    The log line is the bottleneck finder: a turn that takes 40s says at INFO
    which of route / plan / execute / verify / compose it spent them in, and
    the executor's own audit trail then says which step.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        seconds = round(time.perf_counter() - started, 3)
        timings[stage] = seconds
        logger.info("stage %-8s %7.3fs", stage, seconds)


def build_context(question: str, session: Session, route_result: Route,
                  discovery: Optional[Dict[str, Any]] = None) -> str:
    """What the planner needs to know before it can name a key.

    Discovery runs first, deterministically, for exactly this reason: the plan
    then chooses among real keys instead of inventing plausible ones. This is
    the highest-leverage prompt decision in the system. `make_plan` passes the
    discovery it already ran so the dimension guard sees the same candidates.
    """
    blocks: List[str] = []

    if session.has_artifact():
        # The table the user is looking at, which is the last turn's columns --
        # not every column the conversation has ever built. The rest are named
        # so a question that does mean one ("NPL sutununu geri getir") can
        # reach it, but they are not presented as the current table.
        view = session.view()
        columns = "\n".join(
            f"  - {name} ({line.unit}, {line.temporal_semantics}) = {line.label}"
            for name, line in view.lineage.items())
        blocks.append(f"MEVCUT TABLO ({len(view.frame)} satir):\n{columns}")
        hidden = [name for name in session.artifact.column_names() if name not in view.frame.columns]
        if hidden:
            blocks.append("ONCEKI SORULARDAN KALAN SUTUNLAR (bu tabloda gosterilmiyor; sadece "
                          "soru acikca isterse kullan): " + ", ".join(hidden))
        if route_result.is_followup:
            blocks.append("Bu bir DEVAM sorusu: mevcut sutunlari KORU, sadece yeni sutun ekle.")
    else:
        blocks.append("MEVCUT TABLO: bos.")

    found = discovery if discovery is not None else discover_concepts(question, limit=MAX_CANDIDATES_IN_CONTEXT)
    if found["candidates"]:
        # Currencies and the period span come from discovery already; showing
        # them is what lets the plan choose a filter the data supports and
        # lets a coverage question be answered without a fetch. A slice the
        # question named ("YP mevduat") is shown as the currency to copy.
        lines = "\n".join(
            f"  - key={c['key']} | source={c['source']}"
            + (f" | dataset={c['dataset']}" if c["source"] == "bulletin" else "")
            + f" | {c['name']} | {c['unit']} | {c['temporal_semantics']}"
            + (f" | currency={c['currency']} (soruda istenen dilim)" if c.get("currency")
               else (f" | cur={','.join(c['currencies'])}" if c.get("currencies") else ""))
            + (f" | {str(c['first_period'])[:7]}..{str(c['last_period'])[:7]} ({c['n_periods']} donem)"
               if c.get("n_periods") else "")
            for c in found["candidates"])
        blocks.append("BULUNAN ANAHTARLAR (key alanina TAM olarak bunlardan birini yaz):\n" + lines)
    else:
        blocks.append("BULUNAN ANAHTARLAR: yok -- once discover adimi kullan.")

    if route_result.start or route_result.end:
        blocks.append(f"TARIH ARALIGI: start={route_result.start} end={route_result.end}")
    if route_result.wants_analysis:
        hints = {"anomaly": "analyze(method=anomaly, column=ana seri)",
                 "changepoint": "analyze(method=changepoint, column=ana seri)",
                 "causality": "analyze(method=causality, column=hedef, against=aday oncu)",
                 "decompose": "fiyat endeksini fetch et + analyze(method=decompose, column=nominal tutar, "
                              "against=fiyat endeksi)"}
        blocks.append("ANALIZ ISTEGI: " + "; ".join(hints[m] for m in route_result.wants_analysis))
    return "\n\n".join(blocks)


def deterministic_series_plan(question: str, route_result: Route, limit: int = 3,
                              discovery: Optional[Dict[str, Any]] = None) -> Plan:
    """A real plan with no model: fetch what discovery ranked highest.

    The bare template (`discover` alone) produces an empty table, which is a
    worse failure than no fallback at all -- it looks like an answer. This
    fetches the top candidate plus the best from each other corpus, which is
    usually the shape of the question anyway: one BDDK series and one macro
    series that contextualises it. A chart is added by `apply_presentation`
    only when the question asked for one.
    """
    # The same clause-splitting discovery the planner is shown: plain
    # `discover` on a thirty-word question diluted the one content word that
    # mattered, which is exactly what `discover_concepts` exists to prevent.
    found = discovery if discovery is not None else discover_concepts(question, limit=MAX_CANDIDATES_IN_CONTEXT)
    by_identity = {(c["source"], c["key"], c.get("currency")): c for c in found["candidates"]}
    chosen, taken = [], set()

    def take(candidate) -> None:
        identity = (candidate["source"], candidate["key"], candidate.get("currency"))
        if identity not in taken and len(chosen) < limit:
            taken.add(identity)
            chosen.append(candidate)

    # Each clause's first choice first ("konut kredileri" -> the loan book,
    # "faiz oranlari" -> the rate), then the best of any corpus not yet seen.
    for ranked in found.get("by_concept") or []:
        for identity in ranked[:1]:
            if identity in by_identity:
                take(by_identity[identity])
    for candidate in found["candidates"]:
        if candidate["source"] not in {c["source"] for c in chosen}:
            take(candidate)
    if not chosen:
        return template_plan("metadata", question)

    steps = [Step(op="fetch_series", key=c["key"], source=c["source"],
                  dataset=c["dataset"] if c["source"] == "bulletin" else None,
                  currency=c.get("currency"),
                  as_name=_slice_name(c["key"], c["currency"]) if c.get("currency") else None)
             for c in chosen]
    return Plan(intent="series_analysis", start=route_result.start, end=route_result.end,
                steps=steps, reasoning="deterministic: top-ranked discovery candidates")


def _slice_name(base: str, currency: str) -> str:
    slug = re.sub(r"[^\w]+", "_", base).strip("_")
    return f"{slug}_{currency.lower()}"


def apply_dimensions(plan: Plan, discovery: Dict[str, Any]) -> Plan:
    """The currency slice the question named is on the fetch, whatever the plan source.

    Discovery peeled "YP" / "TL" off the concept and tagged the candidates
    that publish that slice with `currency`. This puts it on the plan: a
    fetch of a tagged key with no currency gets the slice (or one step per
    slice, when the question asked for both), and a tagged first-choice key
    the plan never fetched is fetched -- the same guarantee `apply_analysis`
    gives an analysis step. Measured before this existed: the model fetched
    the deposit line as `total` twice from two tables, divided one by the
    other, and reported a 99.97% "share".
    """
    tagged: Dict[tuple, List[str]] = {}
    for candidate in discovery.get("candidates") or []:
        if candidate.get("currency"):
            slices = tagged.setdefault((candidate["source"], candidate["key"]), [])
            if candidate["currency"] not in slices:
                slices.append(candidate["currency"])
    if not tagged:
        return plan
    first_choices = {(ranked[0][0], ranked[0][1]) for ranked in discovery.get("by_concept") or [] if ranked}

    steps: List[Step] = []
    fetched: Dict[tuple, List[Step]] = {}
    for step in plan.steps:
        identity = (step.source, _normalise_key(step.key)) if step.op == "fetch_series" and step.key else None
        slices = tagged.get(identity) if identity else None
        if not slices:
            steps.append(step)
            continue
        covered = {s.currency for s in fetched.get(identity, [])}
        base = step.as_name or step.key
        if step.currency in slices:
            wanted = [step.currency]
        else:
            wanted = [s for s in slices if s not in covered] or [slices[0]]
        for i, currency in enumerate(wanted):
            clone = step if i == 0 else step.model_copy()
            clone.currency = currency
            if len(wanted) > 1 or clone.as_name is None:
                clone.as_name = _slice_name(base, currency)
            steps.append(clone)
            fetched.setdefault(identity, []).append(clone)

    # A slice the question named for a first-choice key that the plan never
    # fetched at all.
    for identity, slices in tagged.items():
        if identity not in first_choices or identity in fetched:
            continue
        candidate = next(c for c in discovery["candidates"]
                         if (c["source"], c["key"]) == identity)
        insert_at = next((i + 1 for i, s in reversed(list(enumerate(steps))) if s.op == "fetch_series"), 0)
        for currency in slices:
            steps.insert(insert_at, Step(
                op="fetch_series", key=identity[1], source=identity[0],
                dataset=candidate.get("dataset") if identity[0] == "bulletin" else None,
                currency=currency, as_name=_slice_name(identity[1], currency)))
            insert_at += 1
        plan.reasoning = f"{plan.reasoning or ''} [dimension: {identity[1]} fetched as {','.join(slices)}]".strip()
    plan.steps = steps
    return plan


EXCHANGE_RATE_KEY = re.compile(r"^TP\.DK\.(USD|EUR)\.", re.I)


def apply_valuation_guard(plan: Plan, session: Session) -> Plan:
    """An FX stock in TL moves with the exchange rate by construction; make the
    comparison the question meant, in Python.

    When a plan fetches a currency slice of a balance-sheet line beside a
    USD/TRY series, this adds -- unasked and model-free -- the other slices of
    that line, the TL share (`ratio` TL/total) and the FX slice in dollars
    (`in_usd`). "USD rose in 42 months and FX deposits rose in all 42" was the
    finding before this existed; it is an identity, not a correlation.
    """
    fetches = [s for s in plan.steps if s.op == "fetch_series" and s.key]
    rate_name = None
    for step in fetches:
        if step.source == "macro" and EXCHANGE_RATE_KEY.match(_normalise_key(step.key)):
            rate_name = _column_name(step, re.sub(r"[^\w]+", "_", step.key))
            break
    if rate_name is None and session.has_artifact():
        rate_name = next((name for name, line in session.artifact.lineage.items()
                          if line.key and EXCHANGE_RATE_KEY.match(line.key)), None)
    if rate_name is None:
        return plan

    sliced = [s for s in fetches if s.source in ("bulletin", "weekly") and s.currency in ("FX", "TL")]
    if not sliced:
        return plan
    existing = set(session.artifact.column_names()) if session.has_artifact() else set()
    planned = {_column_name(s, re.sub(r"[^\w]+", "_", s.key or s.op)) for s in plan.steps if s.op != "analyze"}

    entities: List[tuple] = []
    for step in sliced:
        identity = (step.source, _normalise_key(step.key), step.dataset)
        if identity not in entities:
            entities.append(identity)
    added: List[Step] = []
    for source, key, dataset in entities:
        own = [s for s in fetches if (s.source, _normalise_key(s.key), s.dataset) == (source, key, dataset)]
        base = re.sub(r"_(fx|tl|total)$", "", own[0].as_name or re.sub(r"[^\w]+", "_", key))
        names = {s.currency: _column_name(s, re.sub(r"[^\w]+", "_", key)) for s in own}
        for currency in ("TL", "FX", "total"):
            if currency in names:
                continue
            step = Step(op="fetch_series", key=key, source=source, dataset=dataset,
                        currency=currency, as_name=_slice_name(base, currency))
            names[currency] = step.as_name
            added.append(step)
        share, usd = f"{base}_tl_payi", f"{base}_fx_usd"
        if share not in existing | planned:
            added.append(Step(op="transform", operation="ratio", column=names["TL"],
                              other_column=names["total"], as_name=share))
        if usd not in existing | planned:
            added.append(Step(op="transform", operation="in_usd", column=names["FX"],
                              other_column=rate_name, as_name=usd))
    if not added:
        return plan
    insert_at = next((i + 1 for i, s in reversed(list(enumerate(plan.steps))) if s.op == "fetch_series"), 0)
    plan.steps[insert_at:insert_at] = added
    plan.reasoning = f"{plan.reasoning or ''} [valuation guard: TL payi ve USD bazli YP eklendi]".strip()
    return plan


PRICE_INDEX_HINT = re.compile(r"(KFE|GENENDEKS|TUFE|ENDEKS|fiyat)", re.I)
DEFAULT_DEFLATOR = ("TP.GENENDEKS.T1", "tufe")


def _planned_columns(plan: Plan, session: Session) -> List[Dict[str, Any]]:
    """The columns the table will hold after the plan runs, in order, with
    what is known about each at plan time: existing lineage for the artifact's
    columns, the step's own fields for the ones it is about to fetch."""
    columns: List[Dict[str, Any]] = []
    if session.has_artifact():
        for name, line in session.artifact.lineage.items():
            columns.append({"name": name, "source": line.source, "unit": line.unit,
                            "semantics": line.temporal_semantics, "key": line.key or ""})
    for step in plan.steps:
        if step.op in ("fetch_series", "ingest_external"):
            fallback = re.sub(r"[^\w]+", "_", step.key or step.value_column or step.op)
            columns.append({"name": _column_name(step, fallback), "source": step.source or "external",
                            "unit": step.unit or "", "semantics": "", "key": step.key or ""})
    return columns


def _looks_like_price_index(column: Dict[str, Any]) -> bool:
    return column["semantics"] == "index" or bool(
        PRICE_INDEX_HINT.search(f"{column['name']} {column['key']}"))


def _looks_monetary(column: Dict[str, Any]) -> bool:
    return "TL" in column["unit"] or (column["source"] == "bulletin" and not column["unit"])


def apply_analysis(plan: Plan, route_result: Route, session: Session) -> Plan:
    """Every analysis the question asked for has a step in the plan.

    The router reads the request from the question's own words; this appends
    the `analyze` step when the plan lacks it, mirroring `apply_presentation`
    for charts. Column names are the ones the executor will assign, computed
    with its own `_column_name`, so the step resolves at run time. A wrong
    guess costs one step (the executor's `against` fallback and the verifier's
    input check make it visible), never the turn.
    """
    have = {step.method for step in plan.steps if step.op == "analyze"}
    for method in route_result.wants_analysis:
        if method in have:
            continue
        columns = _planned_columns(plan, session)
        if not columns:
            logger.info("apply_analysis: no columns to run %s on; skipped", method)
            continue
        if method in ("anomaly", "changepoint"):
            plan.steps.append(Step(op="analyze", method=method, column=columns[0]["name"]))
        elif method == "causality":
            if len(columns) < 2:
                logger.info("apply_analysis: causality needs two columns, have %d; skipped", len(columns))
                continue
            plan.steps.append(Step(op="analyze", method="causality",
                                   column=columns[0]["name"], against=columns[1]["name"]))
        elif method == "decompose":
            nominal = next((c for c in columns if _looks_monetary(c) and not _looks_like_price_index(c)),
                           columns[0])
            price = next((c for c in columns if _looks_like_price_index(c) and c is not nominal), None)
            if price is None:
                # A decomposition needs a price index; CPI is the canonical one
                # and the question cannot be answered without it.
                key, name = DEFAULT_DEFLATOR
                insert_at = next((i + 1 for i, s in reversed(list(enumerate(plan.steps)))
                                  if s.op in ("fetch_series", "ingest_external")), 0)
                plan.steps.insert(insert_at, Step(op="fetch_series", key=key, source="macro", as_name=name))
                plan.reasoning = f"{plan.reasoning or ''} [decompose: {key} fetched as deflator]".strip()
                price = {"name": name}
            plan.steps.append(Step(op="analyze", method="decompose",
                                   column=nominal["name"], against=price["name"]))
        have.add(method)
    if route_result.wants_footnotes and not any(step.op == "footnotes" for step in plan.steps):
        plan.steps.append(Step(op="footnotes"))
    return plan


def apply_presentation(plan: Plan, route_result: Route, question: str) -> Plan:
    """A chart step exists in the plan iff the question asked for a chart.

    Enforced here, after planning, so it holds for every plan source alike --
    the model (which the prompt also tells, but a prompt is a request and this
    is a guarantee), the deterministic series plan and the templates. When a
    chart *was* asked for and no step draws one, one is appended over whatever
    the plan fetched.
    """
    plan.steps = [step for step in plan.steps if step.op != "chart"]
    if route_result.wants_chart and any(
            step.op in ("fetch_series", "transform", "ingest_external") for step in plan.steps):
        plan.steps.append(Step(op="chart", title=question[:80]))
    return plan


def make_plan(question: str, session: Session, route_result: Route,
              client: Optional[KloudeksClient]) -> Plan:
    """A validated plan: from the model when possible, from a template otherwise.

    Discovery runs once here and feeds three consumers -- the planner's
    context, the deterministic fallback and `apply_dimensions` -- so they
    cannot disagree about which candidates carry which currency slice.
    """
    series_intent = route_result.intent in ("series_analysis", "followup")
    found = (discover_concepts(question, limit=MAX_CANDIDATES_IN_CONTEXT)
             if client is not None or series_intent else {"candidates": [], "by_concept": []})
    if client is None:
        if series_intent:
            return apply_dimensions(deterministic_series_plan(question, route_result, discovery=found), found)
        return template_plan(route_result.intent, question, route_result.urls,
                             route_result.start, route_result.end)
    try:
        plan = client.structured(
            planner_messages(question, build_context(question, session, route_result, discovery=found)),
            Plan, max_tokens=1400)
    except LLMError:
        if series_intent:
            return apply_dimensions(deterministic_series_plan(question, route_result, discovery=found), found)
        return template_plan(route_result.intent, question, route_result.urls,
                             route_result.start, route_result.end)
    plan = apply_dimensions(plan, found)

    # The router's regex read of the date range beats the model's: it is exact,
    # and a plan that silently drops the window returns 67 months for a
    # question that asked for 60.
    if route_result.start and not plan.start:
        plan.start = route_result.start
    if route_result.end and not plan.end:
        plan.end = route_result.end

    # A follow-up inherits the window of the table it extends. "Bu tabloyu hic
    # bozmadan" states no dates, so without this the new column arrives with its
    # own full history and the outer join stretches the table from 60 rows to 67
    # -- disturbing precisely what the question said not to disturb.
    if route_result.is_followup and session.has_artifact():
        # The window of the table on screen, which a narrower focus may have
        # shortened -- "bozmadan" protects what the last turn showed.
        index = session.view().frame.index
        if len(index):
            plan.start = plan.start or index.min().strftime("%Y-%m-%d")
            plan.end = plan.end or index.max().strftime("%Y-%m-%d")
    if route_result.urls and not any(step.op == "read_url" for step in plan.steps):
        plan.steps = template_plan("url_analysis", question, route_result.urls).steps + plan.steps
    return plan


def _table_payload(session: Session) -> Dict[str, Any]:
    """The table as this turn presents it: `Session.focus`'s columns only.

    The session's artifact is unchanged and still holds everything -- a later
    follow-up can still reach a column this turn did not show.
    """
    view = session.view()
    return {"columns": view.column_names(), "units": view.units(), "rows": view.to_records(),
            "all_columns": session.artifact.column_names()}


def run_turn(question: str, session: Optional[Session] = None,
             client: Optional[KloudeksClient] = None,
             url_reader=None, web_search=None,
             compose_answer: bool = True) -> Dict[str, Any]:
    """One question through the whole pipeline. Returns the API payload."""
    session = session or Session()
    session.start_turn(question)
    timings: Dict[str, float] = {}
    turn_started = time.perf_counter()
    logger.info("turn start: %.120s", question)

    with _timed(timings, "route"):
        route_result = route(question, has_artifact=session.has_artifact(), client=client)
    logger.info("route -> %s (%s) chart=%s table=%s", route_result.intent,
                route_result.decided_by, route_result.wants_chart, route_result.wants_table)

    # `plan` includes discovery (`build_context`) and the planner's model call;
    # `KloudeksClient` logs each call separately, so the two are separable.
    with _timed(timings, "plan"):
        plan = make_plan(question, session, route_result, client)
        plan = apply_valuation_guard(plan, session)
        plan = apply_analysis(plan, route_result, session)
        plan = apply_presentation(plan, route_result, question)
    logger.info("plan -> %s", [step.op + (f":{step.method}" if step.method else "") for step in plan.steps])
    session.facts["intent"] = plan.intent
    session.facts["wants_analysis"] = list(route_result.wants_analysis)
    session.facts["is_followup"] = route_result.is_followup

    with _timed(timings, "execute"):
        Executor(session, url_reader=url_reader, web_search=web_search).run(plan)
    for step in session.audit:
        logger.info("  step %d %-16s %7.3fs %s %s", step.index, step.op, step.seconds,
                    "ok " if step.ok else "ERR", str(step.detail)[:100])

    # What this turn is about, before anything is checked or said about it.
    # The artifact keeps every column the conversation has built; the answer,
    # the checks and the table shown all narrow to the columns this turn
    # touched (plus the previous turn's, when the question said to keep them).
    visible = session.focus(keep_previous=route_result.is_followup)
    hidden = [c for c in session.artifact.column_names() if c not in visible]
    logger.info("focus -> %s%s", visible, f" (hidden: {hidden})" if hidden else "")

    with _timed(timings, "verify"):
        verification = verify(session)

    # Read by the composer, so an answer over an unrequested table does not
    # say "as the table shows".
    presentation = {"table": route_result.wants_table, "chart": route_result.wants_chart}
    session.facts["presentation"] = {"tablo": presentation["table"], "grafik": presentation["chart"]}

    with _timed(timings, "compose"):
        answer = compose(session, question, client) if compose_answer else {
            "summary": "", "composed_by": "skipped", "unsupported_numbers": [], "caveats": [],
            "sources": []}

    timings["total"] = round(time.perf_counter() - turn_started, 3)
    logger.info("turn done in %.3fs: %s", timings["total"],
                " ".join(f"{k}={v:.3f}" for k, v in timings.items() if k != "total"))

    return {
        "question": question,
        "route": route_result.model_dump(),
        "plan": plan.model_dump(exclude_none=True),
        "summary": answer["summary"],
        "composed_by": answer["composed_by"],
        "unsupported_numbers": answer["unsupported_numbers"],
        "sources": answer["sources"],
        "presentation": presentation,
        "table": _table_payload(session),
        "figure": session.facts.get("figure") if presentation["chart"] else None,
        "analysis": session.facts.get("analysis"),
        "find_periods": session.facts.get("find_periods"),
        "citations": session.citations,
        "verification": verification,
        "audit": [step.to_dict() for step in session.audit],
        "timings": timings,
        "session": session,
    }


class Agent:
    """A conversation: one Session per session_id, reused across turns."""

    def __init__(self, client: Optional[KloudeksClient] = None, url_reader=None, web_search=None):
        self.client = client
        self.url_reader = url_reader
        self.web_search = web_search
        self.sessions: Dict[str, Session] = {}

    def session(self, session_id: str) -> Session:
        if session_id not in self.sessions:
            self.sessions[session_id] = Session(session_id=session_id)
        return self.sessions[session_id]

    def ask(self, question: str, session_id: str = "default", **kwargs) -> Dict[str, Any]:
        return run_turn(question, self.session(session_id), client=self.client,
                        url_reader=self.url_reader, web_search=self.web_search, **kwargs)
