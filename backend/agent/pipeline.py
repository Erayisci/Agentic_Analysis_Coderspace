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
from typing import Any, Dict, List, Optional

from ..llm import KloudeksClient, LLMError
from ..tools.lakehouse import discover, discover_concepts
from .composer import compose
from .evidence_store import EvidenceStorageError
from .executor import Executor
from .planner import Plan, Step, planner_messages, template_plan
from .router import Route, route
from .state import Session
from .verifier import verify

# Six was too few: a question with several interest-rate clauses filled every
# slot with rate series and the planner never saw the loan series it was asked
# about. Twelve costs ~200 prompt tokens and gives each corpus four seats.
MAX_CANDIDATES_IN_CONTEXT = 12


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


def make_plan(question: str, session: Session, route_result: Route,
              client: Optional[KloudeksClient]) -> Plan:
    """A validated plan: from the model when possible, from a template otherwise."""
    if client is None:
        if route_result.intent in ("series_analysis", "followup"):
            return deterministic_series_plan(question, route_result)
        return template_plan(route_result.intent, question, route_result.urls,
                             route_result.start, route_result.end)
    try:
        plan = client.structured(
            planner_messages(question, build_context(question, session, route_result)),
            Plan, max_tokens=1400)
    except LLMError:
        if route_result.intent in ("series_analysis", "followup"):
            return deterministic_series_plan(question, route_result)
        return template_plan(route_result.intent, question, route_result.urls,
                             route_result.start, route_result.end)

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
    return plan


def _run_turn(question: str, session: Optional[Session] = None,
             client: Optional[KloudeksClient] = None,
             url_reader=None, web_search=None,
             compose_answer: bool = True, research_runner=None, mode="auto",
             on_tool_result=None) -> Dict[str, Any]:
    """One question through the whole pipeline. Returns the API payload."""
    session = session or Session()
    session.start_turn(question)

    route_result = (Route(intent="search", reason="website web research mode") if mode == "research"
                    else route(question, has_artifact=session.has_artifact(), client=client))
    if route_result.intent == "search" and research_runner is not None:
        return _research_turn(question, session, route_result, research_runner, on_tool_result)
    if mode == "research":
        raise ValueError("Web research is disabled; enable WEB_TOOLS_ENABLED and WEB_AGENT_ENABLED.")
    plan = make_plan(question, session, route_result, client)

    Executor(session, url_reader=url_reader, web_search=web_search, on_tool_result=on_tool_result).run(plan)
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


def _research_turn(question, session, route_result, runner, on_tool_result):
    result = runner(question, on_tool_result=on_tool_result)
    sources = result.get("sources", [])
    citations = [{**s, "source": "web", "cited": s["id"] in result.get("citations", [])}
                 for s in sources]
    caveats = result.get("warnings", []) + result.get("missing_information", [])
    if result.get("error"):
        caveats.append(result["error"].get("message", result["error"].get("code", "Research failed")))
    # This checks provenance/coverage only, never the truth of the model's prose.
    checks = [{"check": "research_completed", "passed": result["status"] == "ok",
               "severity": "error" if result["status"] == "error" else "warning",
               "detail": result.get("stop_reason") or result["status"]}]
    return {
        "question": question, "route": route_result.model_dump(),
        "plan": {"intent": "search", "reasoning": "bounded model-selected research tools"},
        "summary": result.get("answer") or "Araştırma tamamlanamadı. Kaynaklar ve araç hatalarını inceleyin.",
        "composed_by": "llm" if result.get("answer") else "unavailable",
        "unsupported_numbers": [],
        "table": {"columns": session.artifact.column_names(), "units": session.artifact.units(),
                  "rows": session.artifact.to_records()},
        "figure": None, "analysis": None, "find_periods": None,
        "citations": citations,
        "verification": {"scope": "web_provenance", "passed": result["status"] == "ok", "n_checks": 1,
                         "n_errors": int(result["status"] == "error"),
                         "n_warnings": len(caveats), "checks": checks, "caveats": caveats},
        "audit": [{"index": i + 1, "op": t["tool"], "arguments": t["arguments"],
                   "ok": t["status"] != "error", "detail": str(t.get("error") or t["status"])}
                  for i, t in enumerate(result.get("trace", []))],
        "research": {k: v for k, v in result.items() if k != "evidence"},
        "session": session,
    }


def run_turn(question: str, session: Optional[Session] = None,
             client: Optional[KloudeksClient] = None, url_reader=None, web_search=None,
             compose_answer: bool = True, research_runner=None, evidence_store=None,
             mode="auto") -> Dict[str, Any]:
    """Save each web result before it is used, and link the final answer to it."""
    session = session or Session()
    run_id = evidence_store.start(session.session_id, question, mode) if evidence_store else None

    def record(name, arguments, output):
        if evidence_store:
            evidence_store.record(run_id, name, arguments, output)

    def recorded(tool, name, argument):
        if tool is None:
            return None

        def call(value):
            try:
                result = tool(value)
            except EvidenceStorageError:
                raise
            except Exception as exc:
                record(name, {argument: value}, {"status": "error", "error": {"code": type(exc).__name__}})
                raise
            record(name, {argument: value}, result)
            return result
        return call

    try:
        result = _run_turn(question, session, client=client,
                           url_reader=recorded(url_reader, "read_url", "url"),
                           web_search=recorded(web_search, "search_web", "query"),
                           compose_answer=compose_answer, research_runner=research_runner,
                           mode=mode, on_tool_result=record)
        if evidence_store:
            status = result.get("research", {}).get("status") or (
                "partial" if any(not step["ok"] for step in result["audit"]) else "ok")
            result["ingestion"] = evidence_store.finish(
                run_id, {k: v for k, v in result.items() if k != "session"}, status)
        return result
    except EvidenceStorageError:
        raise
    except Exception as exc:
        if evidence_store:
            evidence_store.finish(run_id, {"error": type(exc).__name__}, "error")
        raise


class Agent:
    """A conversation: one Session per session_id, reused across turns."""

    def __init__(self, client: Optional[KloudeksClient] = None, url_reader=None, web_search=None,
                 research_runner=None, evidence_store=None):
        self.client = client
        self.url_reader = url_reader
        self.web_search = web_search
        self.research_runner = research_runner
        self.evidence_store = evidence_store
        self.sessions: Dict[str, Session] = {}

    def session(self, session_id: str) -> Session:
        if session_id not in self.sessions:
            self.sessions[session_id] = Session(session_id=session_id)
        return self.sessions[session_id]

    def ask(self, question: str, session_id: str = "default", **kwargs) -> Dict[str, Any]:
        return run_turn(question, self.session(session_id), client=self.client,
                        url_reader=self.url_reader, web_search=self.web_search,
                        research_runner=self.research_runner, evidence_store=self.evidence_store, **kwargs)
