"""Tests for the FastAPI service. No network call to Kloudeks, ever, on any
machine: the `client` fixture forces the app's agent to client=None right
after startup, regardless of whether the local .env happens to carry a real
KLOUDEKS_API_KEY. Without that override, a dev box that has added one for
manual testing (see main.py's lifespan) would make every /ask test a slow,
non-deterministic real call -- exactly what this suite exists to not do."""
import pytest
from fastapi.testclient import TestClient

from backend.agent.pipeline import Agent
from backend.api.main import app
from backend.core.config import DUCKDB_PATH


def needs_lakehouse():
    if not DUCKDB_PATH.exists():
        pytest.skip("run python -m backend.lakehouse.build first")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "research.sqlite3"))
    with TestClient(app) as test_client:
        # Overrides whatever the real lifespan built from the ambient
        # environment (see module docstring) -- url_reader/web_search stay
        # None too, so a URL in a test question can't reach the network either.
        app.state.agent = Agent(client=None)
        yield test_client
    app.state.agent.sessions.clear()


def test_health_reports_whether_a_model_is_configured(client):
    assert app.state.agent.client is None  # this fixture's guarantee
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["model_configured"] is False

    # /health only checks "is it None" -- but the lifespan's shutdown calls
    # .close() on whatever's left in app.state.agent.client when the `with
    # TestClient(...)` block above exits, so a bare object() would crash
    # teardown; give the sentinel a no-op close().
    from unittest.mock import MagicMock

    app.state.agent.client = MagicMock()
    assert client.get("/health").json()["model_configured"] is True


def test_ask_returns_a_json_serialisable_payload_without_the_raw_session(client):
    needs_lakehouse()
    response = client.post("/ask", json={"question": "konut kredisi verilerini goster"})
    assert response.status_code == 200
    data = response.json()
    assert "session" not in data          # the non-serialisable object must not leak out
    assert "table" in data and "columns" in data["table"]
    assert data["table"]["rows"], "expected at least one row for a real series"


def test_ask_rejects_an_empty_question(client):
    response = client.post("/ask", json={"question": ""})
    assert response.status_code == 422  # pydantic min_length=1


def test_ask_rejects_a_missing_question_field(client):
    response = client.post("/ask", json={})
    assert response.status_code == 422


def test_conversation_state_persists_across_turns_in_one_session(client):
    """The whole point of session_id: turn 2 must see turn 1's table."""
    needs_lakehouse()
    client.post("/ask", json={"question": "konut kredisi verilerini goster", "session_id": "s1"})
    second = client.post("/ask", json={
        "question": "bu tabloyu hic bozmadan enflasyon verisini de ekle", "session_id": "s1"})
    assert second.status_code == 200
    assert len(second.json()["table"]["columns"]) >= 1


def test_different_session_ids_do_not_share_state(client):
    needs_lakehouse()
    client.post("/ask", json={"question": "konut kredisi verilerini goster", "session_id": "a"})
    fresh = client.get("/session/b")
    assert fresh.status_code == 404


def test_get_session_returns_the_current_table_without_asking_again(client):
    needs_lakehouse()
    client.post("/ask", json={"question": "konut kredisi verilerini goster", "session_id": "s2"})
    response = client.get("/session/s2")
    assert response.status_code == 200
    assert response.json()["table"]["columns"]


def test_get_unknown_session_is_404(client):
    response = client.get("/session/does-not-exist")
    assert response.status_code == 404


def test_reset_session_clears_its_table(client):
    needs_lakehouse()
    client.post("/ask", json={"question": "konut kredisi verilerini goster", "session_id": "s3"})
    reset = client.delete("/session/s3")
    assert reset.status_code == 200

    after = client.get("/session/s3")
    assert after.status_code == 404  # gone, not just emptied


def test_reset_on_a_session_that_never_existed_is_not_an_error(client):
    response = client.delete("/session/never-asked-anything")
    assert response.status_code == 200
