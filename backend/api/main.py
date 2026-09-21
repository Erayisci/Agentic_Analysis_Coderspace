"""FastAPI service exposing the agent over HTTP for the frontend.

One conversational endpoint (`POST /ask`) wrapping `agent.pipeline.Agent`. The
Kloudeks client is built once at startup from `KLOUDEKS_API_KEY`; when that is
not set (a dev machine without the hackathon credential, or the endpoint being
unreachable on demo day), the API still starts and every turn falls back to
the deterministic planner path instead of refusing to run -- the same
resilience `agent/planner.py`'s `template_plan` already gives the pipeline
itself, extended to "the API has no key at all", not just "the model failed
this turn".

Working tables live in memory. Web evidence and completed responses are saved
in research.duckdb and can be inspected after a restart or a chat reset.

Web search is optional: with `WEB_TOOLS_ENABLED=true` (and the SearXNG /
crawler containers from `extensions/web_tools/` running) the extension's
`search_web` becomes the agent's search backend; otherwise it is None. The URL
reader is always the in-process `backend.tools.web_url.read_url`.
With WEB_AGENT_ENABLED=true, search questions and explicit research mode use
the extension's adaptive search/read/model loop with per-tool evidence storage.
"""
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from ..agent.executor import Executor
from ..agent.evidence_store import EvidenceStore, EvidenceStorageError
from ..agent.pipeline import Agent
from ..agent.planner import Plan, Step
from ..agent.verifier import verify
from ..core.config import DATA_DIR, ROOT, kloudeks_api_key
from ..llm import KloudeksClient
from ..tools.web_url import read_url

logger = logging.getLogger("kkb.api")


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


def _build_research_runner():
    from ..extensions.web_tools.asset_config import AssetConfig
    from ..extensions.web_tools.research import research
    try:
        from ..tools import get_tools
        if AssetConfig.from_environ().agent_enabled and get_tools():
            return research
    except ValueError as exc:
        logger.warning("Web research misconfigured (%s); research disabled.", exc)
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.evidence_store = EvidenceStore(
        os.environ.get("RESEARCH_DB_PATH", DATA_DIR / "research.duckdb"),
        legacy_path=None if os.environ.get("RESEARCH_DB_PATH") else DATA_DIR / "research.sqlite3")
    app.state.agent = Agent(client=_build_client(), url_reader=read_url,
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
    mode: Literal["auto", "research"] = "auto"

    @field_validator("question")
    @classmethod
    def nonblank_question(cls, value):
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


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "model_configured": _agent().client is not None,
            "web_search_configured": _agent().web_search is not None,
            "research_configured": _agent().research_runner is not None,
            "evidence_storage": "enabled" if _agent().evidence_store is not None else "disabled",
            "evidence_storage_format": "duckdb" if _agent().evidence_store is not None else None}


@app.exception_handler(EvidenceStorageError)
async def evidence_storage_error(request, exc):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.post("/ask")
def ask(request: AskRequest) -> Dict[str, Any]:
    """Run one turn and return the API payload `run_turn` already builds --
    everything except the raw `Session` object, which carries a pandas
    DataFrame and is not JSON-serialisable."""
    if request.mode == "research" and _agent().research_runner is None:
        raise HTTPException(503, "Web research is disabled. Enable WEB_TOOLS_ENABLED=true and "
                            "WEB_AGENT_ENABLED=true on the API and web-tools worker, then start the services.")
    result = _agent().ask(request.question, session_id=request.session_id, mode=request.mode)
    return {key: value for key, value in result.items() if key != "session"}


@app.get("/session/{session_id}/research")
def list_research(session_id: str, limit: int = Query(50, ge=1, le=100)):
    return {"runs": app.state.evidence_store.list_runs(session_id, limit)}


@app.get("/session/{session_id}/research/{run_id}")
def get_research(session_id: str, run_id: str):
    result = app.state.evidence_store.get_run(session_id, run_id)
    if result is None:
        raise HTTPException(404, "Research run not found for this session.")
    return result


@app.post("/debug/ingest_external")
def debug_ingest_external(request: IngestExternalRequest) -> Dict[str, Any]:
    """Run one ingest_external step directly, bypassing the planner.

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
    store = app.state.evidence_store
    run_id = store.start(request.session_id, session.turns[-1]["question"], "debug_ingest")
    Executor(session, url_reader=_agent().url_reader, web_search=_agent().web_search,
             on_tool_result=lambda tool, arguments, output: store.record(run_id, tool, arguments, output)).run(plan)
    verification = verify(session)
    result = {
        "table": {
            "columns": session.artifact.column_names(),
            "units": session.artifact.units(),
            "rows": session.artifact.to_records(),
        },
        "citations": session.citations,
        "verification": verification,
        "audit": [a.to_dict() for a in session.audit],
    }
    result["ingestion"] = store.finish(run_id, result, "ok" if verification["passed"] else "partial")
    return result


@app.get("/session/{session_id}")
def get_session(session_id: str) -> Dict[str, Any]:
    """The current table without asking a new question -- for a frontend
    that reconnects to an existing conversation."""
    if session_id not in _agent().sessions:
        raise HTTPException(404, f"no session {session_id!r}")
    session = _agent().sessions[session_id]
    return {
        "session_id": session_id,
        "table": {
            "columns": session.artifact.column_names(),
            "units": session.artifact.units(),
            "rows": session.artifact.to_records(),
        },
        "citations": session.citations,
        "n_turns": len(session.turns),
    }


@app.delete("/session/{session_id}")
def reset_session(session_id: str) -> Dict[str, str]:
    """Start a fresh conversation under the same id -- a frontend's 'new chat'."""
    _agent().sessions.pop(session_id, None)
    return {"status": "reset", "session_id": session_id}


# A built frontend can use the API's origin. Vite proxies the same paths in dev.
if (ROOT / "frontend" / "dist").is_dir():
    app.mount("/", StaticFiles(directory=ROOT / "frontend" / "dist", html=True), name="website")
