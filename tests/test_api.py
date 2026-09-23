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
def client():
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


def test_sources_round_trip_lands_and_removes_an_external_source(client, monkeypatch, tmp_path):
    """POST /sources is the same ingest a URL in /ask triggers, exposed for the
    frontend; the zone is the real one, so the test removes what it lands."""
    import pandas as pd

    from backend.ingestion.external import documents
    from backend.lakehouse import external_store as store
    from tests.test_agent import _mock_excel_fetch

    monkeypatch.setenv("WEB_ASSET_CACHE_DIR", str(tmp_path / "cache"))
    documents.configure_tools({})
    # Before the fetch is mocked: the SSRF guard must refuse a private address.
    assert client.post("/sources", json={"url": "http://127.0.0.1/x.xlsx"}).status_code == 422
    url = "https://example.org/api-demo.xlsx"
    _mock_excel_fetch(monkeypatch, pd.DataFrame({
        "Tarih": pd.date_range("2021-01-01", periods=6, freq="MS"),
        "Faiz (%)": [17.0, 17.5, 18.0, 18.5, 19.0, 19.5]}))
    try:
        health = client.get("/health").json()
        assert health["extraction_route"] == "in_process" and "n_external_sources" in health

        landed = client.post("/sources", json={"url": url, "hint": "faiz"})
        assert landed.status_code == 200, landed.text
        body = landed.json()
        assert body["status"] == "ok" and body["n_series"] == 1 and body["extraction_route"] == "in_process"
        source_id = body["source_id"]

        listed = client.get("/sources").json()
        assert source_id in [s["source_id"] for s in listed["sources"]]
        detail = client.get(f"/sources/{source_id}").json()
        assert detail["series"][0]["unit"] == "%" and detail["series"][0]["unit_verified"] is False

        # The sources panel's "tabloya ekle": one landed series into the session's table.
        key = detail["series"][0]["series_key"]
        added = client.post("/session/panel-test/columns", json={"series_key": key, "as_name": "faiz"})
        assert added.status_code == 200, added.text
        assert added.json()["table"]["columns"] == ["faiz"] and len(added.json()["table"]["rows"]) == 6
        assert added.json()["citations"][0]["table"] == "external_observations"
        missing = client.post("/session/panel-test/columns", json={"series_key": "nope/nope/nope"})
        assert missing.status_code == 422

        assert client.delete(f"/sources/{source_id}").json()["status"] == "removed"
        assert client.get(f"/sources/{source_id}").status_code == 404
    finally:
        store.remove_source(store.source_id_for(url))
        documents.configure_tools(None)
        documents._TOOLS_RESOLVED = False
