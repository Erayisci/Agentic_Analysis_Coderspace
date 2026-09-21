"""FastAPI service exposing the agent over HTTP for the frontend.

One conversational endpoint (`POST /ask`) wrapping `agent.pipeline.Agent`. The
Kloudeks client is built once at startup from `KLOUDEKS_API_KEY`; when that is
not set (a dev machine without the hackathon credential, or the endpoint being
unreachable on demo day), the API still starts and every turn falls back to
the deterministic planner path instead of refusing to run -- the same
resilience `agent/planner.py`'s `template_plan` already gives the pipeline
itself, extended to "the API has no key at all", not just "the model failed
this turn".

Sessions live in one process-lifetime `Agent` instance, in memory, keyed by
`session_id` -- there is no persistence layer. That is a known, deliberate
scope boundary for a hackathon demo (a restart loses every open conversation)
and not a decision the API layer should quietly grow past on its own; adding
one is future work, not a bug in this file.

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
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..agent.executor import Executor
from ..agent.pipeline import Agent
from ..agent.planner import Plan, Step
from ..agent.verifier import verify
from ..core.config import kloudeks_api_key
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = _build_client()
    app.state.agent = Agent(client=client, url_reader=_build_url_reader(client),
                            web_search=_build_web_search())
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
    question: str = Field(min_length=1, description="the user's question, in Turkish or English")
    session_id: str = Field(default="default", description="conversation to continue, or a new one")


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
    return {"status": "ok", "model_configured": _agent().client is not None}


@app.post("/ask")
def ask(request: AskRequest) -> Dict[str, Any]:
    """Run one turn and return the API payload `run_turn` already builds --
    everything except the raw `Session` object, which carries a pandas
    DataFrame and is not JSON-serialisable."""
    started = time.perf_counter()
    result = _agent().ask(request.question.strip(), session_id=request.session_id)
    payload = {key: value for key, value in result.items() if key != "session"}
    # Request time minus the pipeline's own total is serialisation: a 67-row
    # table and a Plotly figure are cheap, but this is where that would show.
    logger.info("/ask session=%s %.3fs (pipeline %.3fs)", request.session_id,
                time.perf_counter() - started, result["timings"]["total"])
    return payload


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
        "citations": session.citations,
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
        "citations": session.citations,
        "n_turns": len(session.turns),
    }


@app.delete("/session/{session_id}")
def reset_session(session_id: str) -> Dict[str, str]:
    """Start a fresh conversation under the same id -- a frontend's 'new chat'."""
    _agent().sessions.pop(session_id, None)
    return {"status": "reset", "session_id": session_id}
