"""FastAPI service exposing the agent over HTTP for the frontend.

One conversational endpoint (`POST /ask`) wrapping `agent.pipeline.Agent`. The
Kloudeks client is built once at startup from `KLOUDEKS_API_KEY`; when that is
not set (a dev machine without the hackathon credential, or the endpoint being
unreachable on demo day), the API still starts and every turn falls back to
the deterministic planner path instead of refusing to run -- the same
resilience `agent/planner.py`'s `template_plan` already gives the pipeline
itself, extended to "the API has no key at all", not just "the model failed
this turn".

Working tables live in one process-lifetime `Agent` instance, in memory, keyed
by `session_id`: a restart loses every open conversation's table. Two things
do persist. Numeric series a URL yields land in the lakehouse's external zone
(`data/external/`, see backend/ingestion/external). Web evidence -- every
search hit, read page and research decision a turn used, plus the finished
answer -- is saved in `data/research.duckdb` (`agent/evidence_store.py`,
`RESEARCH_DB_PATH` relocates it) and can be replayed after a restart or a new
chat through `/session/{id}/research`.

With `WEB_TOOLS_ENABLED=true` and `WEB_AGENT_ENABLED=true`, search questions
and the website's explicit research mode run the extension's bounded
search/read/model loop, each tool result recorded before the model sees it.

Web search is optional: with `WEB_TOOLS_ENABLED=true` (and the SearXNG /
crawler containers from `extensions/web_tools/` running) the extension's
`search_web` becomes the agent's search backend; otherwise it is None. The URL
reader is always the in-process `backend.tools.web_url.read_url`, with its
image path bound to `KloudeksClient.ocr` when a Kloudeks key is configured --
without one it falls back to `read_url`'s own "no OCR callable configured"
error for that one content kind, the same degradation every other model-backed
path in this file already gets from a missing key.
"""
import logging
import os
import time
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, Dict, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from ..agent.evidence_store import EvidenceStorageError, EvidenceStore
from ..agent.executor import Executor
from ..agent.pipeline import Agent
from ..agent.planner import Plan, Step
from ..agent.verifier import verify
from ..core.config import DATA_DIR, kloudeks_api_key
from ..ingestion.external import ingest_url
from ..ingestion.external.documents import extraction_route
from ..lakehouse import external_store
from ..llm import KloudeksClient
from ..tools.web_url import read_url

logger = logging.getLogger("kkb.api")

# uvicorn configures only its own loggers; without a root handler the `kkb.*`
# INFO lines (stage and model-call timings) never reach the console. No-op if
# the process already configured logging. KKB_LOG_LEVEL=DEBUG widens it.
logging.basicConfig(
    level=os.environ.get("KKB_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def _build_client() -> Optional[KloudeksClient]:
    try:
        kloudeks_api_key()
    except RuntimeError as exc:
        logger.warning("Kloudeks not configured (%s); running deterministic-only.", exc)
        return None
    return KloudeksClient()


def _build_web_search():
    """The web-tools extension's SearXNG search when WEB_TOOLS_ENABLED=true;
    None otherwise, so a `search` step fails as one step with the executor's
    own "no web search backend configured" error rather than at startup."""
    from ..extensions.web_tools.team_adapter import WebEvidenceError, get_team_tools
    try:
        return get_team_tools()["web_search"]
    except WebEvidenceError:
        return None
    except ValueError as exc:  # WEB_TOOLS_ENABLED=garbage or a bad WEB_* value
        logger.warning("Web tools misconfigured (%s); search disabled.", exc)
        return None


def _build_url_reader(client: Optional[KloudeksClient]):
    return read_url if client is None else partial(read_url, ocr=client.ocr)


def _build_research_runner():
    """The extension's bounded research loop when WEB_TOOLS_ENABLED and
    WEB_AGENT_ENABLED are both on; None otherwise, so research mode answers
    503 with the switches to flip rather than failing inside a turn."""
    from ..extensions.web_tools.asset_config import AssetConfig
    from ..extensions.web_tools.research import research
    try:
        from ..tools import get_tools
        if AssetConfig.from_environ().agent_enabled and get_tools():
            return research
    except ValueError as exc:
        logger.warning("Web research misconfigured (%s); research disabled.", exc)
    return None


def _build_evidence_store() -> EvidenceStore:
    configured = os.environ.get("RESEARCH_DB_PATH")
    return EvidenceStore(configured or DATA_DIR / "research.duckdb",
                         legacy_path=None if configured else DATA_DIR / "research.sqlite3")


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = _build_client()
    app.state.evidence_store = _build_evidence_store()
    app.state.agent = Agent(client=client, url_reader=_build_url_reader(client),
                            web_search=_build_web_search(), research_runner=_build_research_runner(),
                            evidence_store=app.state.evidence_store)
    try:
        yield
    finally:
        if app.state.agent.client is not None:
            app.state.agent.client.close()


app = FastAPI(title="KKB Agentic Analytics API", lifespan=lifespan)

# Demo-day default: any origin may call the API (the frontend's own origin is
# not yet fixed -- it may be served from a different host/port than this
# service). Tighten to the deployed frontend's origin before a real rollout.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000, description="the user's question, in Turkish or English")
    session_id: str = Field(default="default", description="conversation to continue, or a new one")
    mode: Literal["auto", "research"] = Field(
        "auto", description="'research' runs the web-tools research loop instead of the analysis pipeline")

    @field_validator("question")
    @classmethod
    def nonblank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value.strip()


class IngestExternalRequest(BaseModel):
    url: str = Field(min_length=1)
    value_column: str = Field(min_length=1, description="which column of the file holds the numbers")
    session_id: str = "default"
    as_name: Optional[str] = None
    period_column: Optional[str] = None
    sheet: Optional[str] = None
    unit: Optional[str] = None
    monthly_rule: str = "last"


def _agent() -> Agent:
    return app.state.agent


class SourceRequest(BaseModel):
    url: str = Field(min_length=1, description="an Excel/CSV/PDF/HTML/image URL, or a page linking to such files")
    hint: Optional[str] = Field(None, description="what the source should answer; ranks linked documents")
    force: bool = Field(False, description="re-land even when the bytes are unchanged")
    follow_links: bool = Field(True, description="also land the best-matching documents a page links to")


def _records(frame) -> list:
    return frame.astype(object).where(frame.notna(), None).to_dict("records")


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "model_configured": _agent().client is not None,
            "web_search_configured": _agent().web_search is not None,
            "research_configured": _agent().research_runner is not None,
            "evidence_storage": "enabled" if _agent().evidence_store is not None else "disabled",
            "extraction_route": extraction_route(),
            "n_external_sources": int(len(external_store.list_sources()))}


@app.exception_handler(EvidenceStorageError)
async def evidence_storage_error(request, exc):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/session/{session_id}/research")
def list_research(session_id: str, limit: int = Query(50, ge=1, le=100)) -> Dict[str, Any]:
    """The saved research runs of one session: question, status, how many
    tool results were kept. Survives restarts and 'Yeni Sohbet'."""
    return {"runs": app.state.evidence_store.list_runs(session_id, limit)}


@app.get("/session/{session_id}/research/{run_id}")
def get_research(session_id: str, run_id: str) -> Dict[str, Any]:
    result = app.state.evidence_store.get_run(session_id, run_id)
    if result is None:
        raise HTTPException(404, "Research run not found for this session.")
    return result


@app.post("/sources")
def add_source(request: SourceRequest) -> Dict[str, Any]:
    """Land a URL in the lakehouse's external zone now, outside any turn --
    the same `ingest_url` a URL in an /ask question triggers automatically.
    Writes Parquet under data/external/; the DuckDB file is never written."""
    try:
        result = ingest_url(request.url, request.hint, force=request.force, follow_links=request.follow_links,
                            client=_agent().client)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return result.to_dict()


@app.get("/sources")
def list_sources() -> Dict[str, Any]:
    sources = external_store.list_sources()
    return {"n_sources": int(len(sources)), "sources": _records(sources)}


@app.get("/sources/{source_id}")
def get_source(source_id: str) -> Dict[str, Any]:
    manifest = external_store.read_manifest(source_id)
    if manifest is None:
        raise HTTPException(404, f"no external source {source_id!r}")
    parts = external_store.read_source(source_id)
    return {"manifest": manifest, "series": _records(parts["series"]), "quality": _records(parts["quality"])}


@app.delete("/sources/{source_id}")
def delete_source(source_id: str) -> Dict[str, str]:
    try:
        removed = external_store.remove_source(source_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if not removed:
        raise HTTPException(404, f"no external source {source_id!r}")
    return {"status": "removed", "source_id": source_id}


class AddColumnRequest(BaseModel):
    series_key: str = Field(min_length=1, description="an external_series.series_key")
    session_id: str = "default"
    as_name: Optional[str] = None


@app.post("/session/{session_id}/columns")
def add_external_column(session_id: str, request: AddColumnRequest) -> Dict[str, Any]:
    """Add one landed series to a session's table, deterministically -- the
    frontend's 'tabloya ekle' on the sources panel. Runs the same executor
    step a plan would (`fetch_series`, source='external'), so the column
    carries its citation and the verifier sees it like any other."""
    session = _agent().session(session_id)
    session.start_turn(f"[sources] add {request.series_key}")
    plan = Plan(intent="followup", steps=[
        Step(op="fetch_series", key=request.series_key, source="external", as_name=request.as_name)])
    Executor(session, url_reader=_agent().url_reader, web_search=_agent().web_search).run(plan)
    verification = verify(session)
    failed = [a for a in session.audit if not a.ok]
    if failed:
        raise HTTPException(422, failed[0].detail)
    return {
        "table": {"columns": session.artifact.column_names(), "units": session.artifact.units(),
                  "rows": session.artifact.to_records()},
        "citations": session.citations, "verification": verification,
        "audit": [a.to_dict() for a in session.audit],
    }


@app.post("/ask")
def ask(request: AskRequest) -> Dict[str, Any]:
    """Run one turn and return the API payload `run_turn` already builds --
    everything except the raw `Session` object, which carries a pandas
    DataFrame and is not JSON-serialisable."""
    started = time.perf_counter()
    if request.mode == "research" and _agent().research_runner is None:
        raise HTTPException(503, "Web research is disabled. Enable WEB_TOOLS_ENABLED=true and "
                            "WEB_AGENT_ENABLED=true on the API and web-tools worker, then start the services.")
    result = _agent().ask(request.question, session_id=request.session_id, mode=request.mode)
    payload = {key: value for key, value in result.items() if key != "session"}
    # Request time minus the pipeline's own total is serialisation: a 67-row
    # table and a Plotly figure are cheap, but this is where that would show.
    logger.info("/ask session=%s %.3fs (pipeline %.3fs)", request.session_id,
                time.perf_counter() - started, result["timings"]["total"])
    return payload


@app.post("/debug/ingest_external")
def debug_ingest_external(request: IngestExternalRequest) -> Dict[str, Any]:
    """Run one ingest_external step directly, bypassing the planner.

    DEPRECATED in favour of `POST /sources` (and of simply putting the URL in
    an /ask question): those land every table in the file in the lakehouse's
    external zone with no column name needed. Kept for the one case where a
    user names a single column to add to this session only.

    ingest_external needs to see a file's columns before it can name
    value_column -- normally the model previews the file with read_url first,
    then decides. The deterministic fallback (no Kloudeks key) never does
    this: it plans a single fixed template with no file-preview step, so it
    can never emit this op from natural language alone. This endpoint exists
    so the feature itself (fetch, parse, add to the session's table) can be
    exercised end to end without a working Kloudeks key -- a deployment
    with one exercises the same executor code through /ask instead, once the
    model has actually chosen to call it.
    """
    session = _agent().session(request.session_id)
    session.start_turn(f"[debug] ingest_external {request.url}")
    step = Step(op="ingest_external", url=request.url, value_column=request.value_column,
                as_name=request.as_name, period_column=request.period_column,
                sheet=request.sheet, unit=request.unit, monthly_rule=request.monthly_rule)
    plan = Plan(intent="url_analysis", steps=[step])
    Executor(session, url_reader=_agent().url_reader, web_search=_agent().web_search).run(plan)
    # Adding a file's column to the table is an extension of it, so the
    # columns already on screen stay -- the same rule a "bozmadan" follow-up
    # gets in the pipeline.
    session.focus(keep_previous=True)
    verification = verify(session)
    view = session.view()
    return {
        "table": {
            "columns": view.column_names(),
            "units": view.units(),
            "rows": view.to_records(),
            "all_columns": session.artifact.column_names(),
        },
        "citations": session.turn_citations(),
        "verification": verification,
        "audit": [a.to_dict() for a in session.audit],
    }


@app.get("/session/{session_id}")
def get_session(session_id: str) -> Dict[str, Any]:
    """The current table without asking a new question -- for a frontend
    that reconnects to an existing conversation."""
    if session_id not in _agent().sessions:
        raise HTTPException(404, f"no session {session_id!r}")
    session = _agent().sessions[session_id]
    # What the last turn showed, not everything the conversation has built.
    view = session.view()
    return {
        "session_id": session_id,
        "table": {
            "columns": view.column_names(),
            "units": view.units(),
            "rows": view.to_records(),
            "all_columns": session.artifact.column_names(),
        },
        "citations": session.turn_citations(),
        "n_turns": len(session.turns),
    }


@app.delete("/session/{session_id}")
def reset_session(session_id: str) -> Dict[str, str]:
    """Start a fresh conversation under the same id -- a frontend's 'new chat'."""
    _agent().sessions.pop(session_id, None)
    return {"status": "reset", "session_id": session_id}
