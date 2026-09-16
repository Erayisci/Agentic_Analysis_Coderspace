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
"""
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..agent.pipeline import Agent
from ..core.config import kloudeks_api_key
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.agent = Agent(client=_build_client(), url_reader=read_url, web_search=None)
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
    result = _agent().ask(request.question.strip(), session_id=request.session_id)
    return {key: value for key, value in result.items() if key != "session"}


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
