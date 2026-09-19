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
import re
from typing import Any, Dict, List, Optional

from ..llm import KloudeksClient, LLMError
from ..tools.lakehouse import discover, discover_concepts
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

# Measured against the live model, two separate failure modes for the same
# question ("faiz krediyi etkiliyor mu"): (1) it fetches both series -- so
# discovery and key selection work fine -- but then only charts them, never
# emitting the analyze step the question actually asked for; (2) it tries to,
# but folds both series into `against` and leaves `column` empty, which fails
# Plan validation outright and discards the *entire* plan, landing on the
# discovery-based deterministic fallback -- same failure shape as no model
# being reachable at all. Both are repaired the same way as a missing read_url
# step is repaired below: insert the step the question named, on top of
# whichever plan (model's or fallback's) made it through, rather than betting
# on the next prompt tweak to fix the model.
CAUSALITY_TRIGGER = re.compile(
    r"\b(nedensellik|neden[- ]sonu[çc]|etkiliyor\s*mu|etkiler\s*mi|etkiledi[ğg]ini|"
    r"öncü\s*g[öo]sterge|granger|causalit(y|e)|causal\b)", re.I)


def _ensure_causality_step(plan: Plan, question: str) -> Plan:
    """If the question asks for causality and the plan fetched two series but
    never ran the test, run it instead of only charting (or discovering) them."""
    if not CAUSALITY_TRIGGER.search(question or ""):
        return plan
    if any(step.op == "analyze" and step.method == "causality" for step in plan.steps):
        return plan
    producing = [step for step in plan.steps if step.op in ("fetch_series", "transform", "ingest_external")]
    if len(producing) < 2:
        return plan  # nothing to point `column`/`against` at -- not this repair's job

    def _name(step: Step) -> str:
        fallback = _normalise_key(step.key) if step.key else "series"
        return _column_name(step, fallback)

    # The first two series are what the question named; anything fetched after
    # that is more likely exploratory (or, per the mis-route this repairs,
    # spurious) than the intended second half of the pair.
    cause, effect = _name(producing[0]), _name(producing[1])
    if cause == effect:
        return plan
    insert_at = next((i for i, step in enumerate(plan.steps) if step.op == "chart"), len(plan.steps))
    plan.steps.insert(insert_at, Step(op="analyze", method="causality", column=effect, against=cause))
    return plan


def build_context(question: str, session: Session, route_result: Route) -> str:
    """What the planner needs to know before it can name a key.

    Discovery runs first, deterministically, for exactly this reason: the plan
    then chooses among real keys instead of inventing plausible ones. This is
    the highest-leverage prompt decision in the system.
    """
    blocks: List[str] = []

    if session.has_artifact():
        columns = "\n".join(
            f"  - {name} ({line.unit}, {line.temporal_semantics}) = {line.label}"
            for name, line in session.artifact.lineage.items())
        blocks.append(f"MEVCUT TABLO ({len(session.artifact.frame)} satir):\n{columns}")
        if route_result.is_followup:
            blocks.append("Bu bir DEVAM sorusu: mevcut sutunlari KORU, sadece yeni sutun ekle.")
    else:
        blocks.append("MEVCUT TABLO: bos.")

    found = discover_concepts(question, limit=MAX_CANDIDATES_IN_CONTEXT)
    if found["candidates"]:
        lines = "\n".join(
            f"  - key={c['key']} | source={c['source']}"
            + (f" | dataset={c['dataset']}" if c["source"] == "bulletin" else "")
            + f" | {c['name']} | {c['unit']} | {c['temporal_semantics']}"
            for c in found["candidates"])
        blocks.append("BULUNAN ANAHTARLAR (key alanina TAM olarak bunlardan birini yaz):\n" + lines)
    else:
        blocks.append("BULUNAN ANAHTARLAR: yok -- once discover adimi kullan.")

    if route_result.start or route_result.end:
        blocks.append(f"TARIH ARALIGI: start={route_result.start} end={route_result.end}")
    return "\n\n".join(blocks)


def deterministic_series_plan(question: str, route_result: Route, limit: int = 3) -> Plan:
    """A real plan with no model: fetch what discovery ranked highest, then chart.

    The bare template (`discover` then `chart`) produces an empty table, which
    is a worse failure than no fallback at all -- it looks like an answer. This
    fetches the top candidate plus the best from each other corpus, which is
    usually the shape of the question anyway: one BDDK series and one macro
    series that contextualises it.
    """
    found = discover(question, limit=12)
    chosen, seen_sources = [], set()
    for candidate in found["candidates"]:
        first_of_source = candidate["source"] not in seen_sources
        if first_of_source or len(chosen) == 0:
            chosen.append(candidate)
            seen_sources.add(candidate["source"])
        if len(chosen) >= limit:
            break
    if not chosen:
        return template_plan("metadata", question)

    steps = [Step(op="fetch_series", key=c["key"], source=c["source"],
                  dataset=c["dataset"] if c["source"] == "bulletin" else None,
                  as_name=None) for c in chosen]
    steps.append(Step(op="chart", title=question[:80]))
    return Plan(intent="series_analysis", start=route_result.start, end=route_result.end,
                steps=steps, reasoning="deterministic: top-ranked discovery candidates")


def _fallback_plan(question: str, route_result: Route) -> Plan:
    if route_result.intent in ("series_analysis", "followup"):
        return deterministic_series_plan(question, route_result)
    return template_plan(route_result.intent, question, route_result.urls,
                         route_result.start, route_result.end)


DATA_PRODUCING_OPS = ("fetch_series", "transform", "ingest_external")


def make_plan(question: str, session: Session, route_result: Route,
              client: Optional[KloudeksClient]) -> Plan:
    """A validated plan: from the model when possible, from a template otherwise.

    Both paths go through the same repairs below, `_ensure_causality_step`
    included -- a model that names the wrong field for its second series (seen
    live: both series folded into `against`, `column` left empty) fails
    validation entirely and lands on the *deterministic* fallback same as a
    model that isn't reachable at all, so the fallback needs the same repair
    the model's own plan gets, not a lesser one.
    """
    if client is None:
        plan = _fallback_plan(question, route_result)
    else:
        try:
            plan = client.structured(
                planner_messages(question, build_context(question, session, route_result)),
                Plan, max_tokens=1400)
        except LLMError:
            plan = _fallback_plan(question, route_result)

        # A plan that only discovers (and maybe charts nothing) is a *valid*
        # plan but a dead end -- measured live, a model handed a starting-from-
        # empty question sometimes emits just `discover` and stops rather than
        # committing to the fetch it just found candidates for. Rule 1 in the
        # prompt ("don't invent a key") makes this the *safe* failure, but a
        # safe non-answer is still not an answer: the same deterministic
        # fallback used for an unreachable model recovers a real table here too.
        if (route_result.intent in ("series_analysis", "followup") and not session.has_artifact()
                and not any(step.op in DATA_PRODUCING_OPS for step in plan.steps)):
            plan = _fallback_plan(question, route_result)

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
        index = session.artifact.frame.index
        plan.start = plan.start or index.min().strftime("%Y-%m-%d")
        plan.end = plan.end or index.max().strftime("%Y-%m-%d")
    if route_result.urls and not any(step.op == "read_url" for step in plan.steps):
        plan.steps = template_plan("url_analysis", question, route_result.urls).steps + plan.steps
    plan = _ensure_causality_step(plan, question)
    return plan


def run_turn(question: str, session: Optional[Session] = None,
             client: Optional[KloudeksClient] = None,
             url_reader=None, web_search=None,
             compose_answer: bool = True) -> Dict[str, Any]:
    """One question through the whole pipeline. Returns the API payload."""
    session = session or Session()
    session.start_turn(question)

    route_result = route(question, has_artifact=session.has_artifact(), client=client)
    plan = make_plan(question, session, route_result, client)

    Executor(session, url_reader=url_reader, web_search=web_search).run(plan)
    verification = verify(session)
    answer = compose(session, question, client) if compose_answer else {
        "summary": "", "composed_by": "skipped", "unsupported_numbers": [], "caveats": []}

    return {
        "question": question,
        "route": route_result.model_dump(),
        "plan": plan.model_dump(exclude_none=True),
        "summary": answer["summary"],
        "composed_by": answer["composed_by"],
        "unsupported_numbers": answer["unsupported_numbers"],
        "table": {
            "columns": session.artifact.column_names(),
            "units": session.artifact.units(),
            "rows": session.artifact.to_records(),
        },
        "figure": session.facts.get("figure"),
        "analysis": session.facts.get("analysis"),
        "find_periods": session.facts.get("find_periods"),
        "citations": session.citations,
        "verification": verification,
        "audit": [step.to_dict() for step in session.audit],
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
