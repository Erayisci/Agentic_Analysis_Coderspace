"""Website -> the extension's bounded research loop -> durable evidence, without network or model quota.

Ported from web-search-tool/injestion. The research loop answers explicit research mode
and search-intent questions; a URL question goes through the external zone (pre_ingest),
so those tests stub `pipeline.ingest_url` to stay off the network.
"""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from backend.agent.evidence_store import EvidenceStore, EvidenceStorageError
from backend.agent.pipeline import Agent
from backend.api.main import app
from backend.extensions.web_tools.research import research


ENV = {"WEB_TOOLS_ENABLED": "true", "WEB_AGENT_ENABLED": "true", "WEB_DOCUMENTS_ENABLED": "true"}
URL = "https://example.com/report"


def call(name, **arguments):
    return {"status": "ok", "decision": {"action": "tool", "name": name, "arguments": arguments}}


def answer():
    return {"status": "ok", "decision": {
        "action": "answer", "answer": "Revenue is 100 TRY. [S1]", "citations": ["S1"],
        "coverage": [{"requirement_id": "R1", "status": "supported", "citations": ["S1"], "note": "Read report."}]}}


def document(text="Revenue is 100 TRY."):
    return {"status": "ok", "final_url": URL, "fetched_at": "2026-09-21T12:00:00Z", "format": "html",
            "content": text, "sections": [{"location": "Page", "method": "html", "text": text}],
            "truncated": False, "error": None}


def runner(actions=None, text="Revenue is 100 TRY.", env=None):
    actions = actions or [call("search_web", query="official revenue report"), call("read_web_url", url=URL), answer()]
    return partial(research, environ=env or ENV, decide=Mock(side_effect=actions), tools={
        "search_web": Mock(return_value={"status": "ok", "results": [
            {"url": URL, "title": "Official report", "snippet": "Revenue report"},
            {"url": "https://example.com/unused", "snippet": "Not selected, still saved"}]}),
        "read_web_url": Mock(return_value=document(text))})


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "research.sqlite3"))
    monkeypatch.setattr("backend.api.main._build_client", lambda: None)
    monkeypatch.setattr("backend.api.main._build_web_search", lambda: None)
    monkeypatch.setattr("backend.api.main._build_research_runner", lambda: runner())
    with TestClient(app) as http:
        yield http


def test_website_research_saves_all_results_and_survives_restart_and_chat_reset(client):
    response = client.post("/ask", json={"question": "Find official revenue", "mode": "research", "session_id": "s1"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["summary"] == "Revenue is 100 TRY. [S1]"
    assert [item["op"] for item in data["audit"]] == ["search_web", "read_web_url"]
    assert data["evidence"]["tool_results"] == 2
    run_id = data["evidence"]["run_id"]
    # A new connection/store and empty session map simulate application restart.
    app.state.evidence_store = EvidenceStore(app.state.evidence_store.path)
    client.delete("/session/s1")
    saved = client.get(f"/session/s1/research/{run_id}").json()
    assert saved["status"] == "ok"
    assert saved["response"]["summary"] == data["summary"]
    assert len(saved["tool_results"][0]["output"]["results"]) == 2
    assert saved["tool_results"][1]["output"] == document()
    assert client.get("/session/s1/research").json()["runs"][0]["id"] == run_id
    assert client.get(f"/session/other/research/{run_id}").status_code == 404
    assert client.get("/session/other/research").json()["runs"] == []


def test_search_route_uses_research_without_explicit_mode(client):
    data = client.post("/ask", json={"question": "Search the web for the official revenue report"}).json()
    assert data["research"]["usage"]["tool_calls"] == 2
    assert data["evidence"]["status"] == "saved"


def test_url_request_follows_discovered_attachment_and_saves_pdf_before_answer(client):
    mode = "research"
    landing_url = "https://example.com/market-data"
    pdf_url = "https://example.com/gold.pdf"
    landing = {**document("Gold transactions. Error! File not found!"), "final_url": landing_url}
    pdf = {**document("January 2026: 33,584 kg. February 2026: 32,719 kg."),
           "final_url": pdf_url, "format": "pdf"}
    tools = {"read_web_url": Mock(side_effect=[landing, pdf]), "get_page_assets": Mock(return_value={
        "status": "ok", "links": [{"url": pdf_url, "text": "Gold transactions", "type_hint": "pdf"}]})}
    final = {"status": "ok", "decision": {
        "action": "answer", "answer": "January: 33,584 kg; February: 32,719 kg. [S2]", "citations": ["S2"],
        "coverage": [{"requirement_id": "R1", "status": "supported", "citations": ["S2"], "note": "PDF page 1."}]}}

    def decide(payload):
        history = payload["context"]["history"]
        # The first model decision must see the supplied page, not an empty context.
        if len(history) == 1:
            assert history[0]["tool"] == "read_web_url"
            assert payload["context"]["sources"][0]["url"] == landing_url
            return call("get_page_assets", url=landing_url)
        if len(history) == 2:
            assert history[-1]["links"][0]["url"] == pdf_url
            return call("read_web_url", url=pdf_url)
        assert payload["context"]["sources"][-1]["url"] == pdf_url
        return final

    app.state.agent.research_runner = partial(
        research, environ={**ENV, "WEB_LINKS_ENABLED": "true"}, tools=tools, decide=decide)
    question = f"Open [{landing_url}]({landing_url}), find Gold transactions and read the linked PDF."
    response = client.post("/ask", json={"question": question, "mode": mode})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["research"]["status"] == "ok"
    assert data["summary"] == final["decision"]["answer"]
    assert [item["op"] for item in data["audit"]] == ["read_web_url", "get_page_assets", "read_web_url"]
    assert data["citations"][-1]["url"] == pdf_url
    assert data["citations"][-1]["cited"] is True
    saved = client.get(f"/session/default/research/{data['evidence']['run_id']}").json()
    assert len(saved["tool_results"]) == 3
    assert saved["tool_results"][-1]["output"] == pdf


def test_complete_result_is_saved_before_evidence_budget_rejects_it(client):
    text = "Türkçe full returned extraction. " * 500
    app.state.agent.research_runner = runner(
        [call("read_web_url", url=URL), answer()], text=text,
        env={**ENV, "WEB_AGENT_MAX_EVIDENCE_BYTES": "4096"})
    data = client.post("/ask", json={"question": "Read report", "mode": "research"}).json()
    saved = client.get(f"/session/default/research/{data['evidence']['run_id']}").json()
    assert saved["tool_results"][0]["output"]["content"] == text
    assert saved["status"] == "error"
    assert not data["research"]["answer"]


def test_model_failure_keeps_prior_searches_and_documents(client):
    app.state.agent.research_runner = runner([call("search_web", query="report"), call("read_web_url", url=URL),
                                            {"status": "error", "error": {"code": "model_timeout"}}])
    data = client.post("/ask", json={"question": "Read report", "mode": "research"}).json()
    saved = client.get(f"/session/default/research/{data['evidence']['run_id']}").json()
    assert saved["status"] == "partial"
    assert len(saved["tool_results"]) == 2
    assert data["research"]["error"]["code"] == "model_timeout"
    assert data["composed_by"] == "unavailable"


def test_tool_error_is_saved_even_without_any_readable_source(client):
    app.state.agent.research_runner = partial(
        research, environ=ENV, tools={"search_web": lambda **kw: {"status": "error", "error": {"code": "timeout"}}},
        decide=Mock(side_effect=[call("search_web", query="report"), answer()]))
    data = client.post("/ask", json={"question": "Read report", "mode": "research"}).json()
    saved = client.get(f"/session/default/research/{data['evidence']['run_id']}").json()
    assert saved["tool_results"][0]["status"] == "error"
    assert saved["status"] == "error"
    assert "timeout" in data["audit"][0]["detail"]


def test_no_success_response_when_database_write_fails(client, monkeypatch):
    def unavailable(*args):
        raise EvidenceStorageError("Research evidence could not be saved or loaded.")
    monkeypatch.setattr(app.state.evidence_store, "record", unavailable)
    response = client.post("/ask", json={"question": "Find report", "mode": "research"})
    assert response.status_code == 503
    assert "could not be saved" in response.json()["detail"]


def test_normal_url_path_saves_content_before_composer_truncation(client, monkeypatch):
    from backend.ingestion.external import IngestResult
    monkeypatch.setattr("backend.agent.pipeline.ingest_url", lambda url, **kw: IngestResult(
        source_id="stub", url=url, status="empty"))
    text = "Full returned URL content. " * 500
    app.state.agent = Agent(url_reader=lambda url: {"text": text, "url": url, "kind": "html"},
                            evidence_store=app.state.evidence_store)
    data = client.post("/ask", json={"question": f"Read {URL}"}).json()
    saved = client.get(f"/session/default/research/{data['evidence']['run_id']}").json()
    assert saved["tool_results"][0]["output"]["text"] == text
    assert len(app.state.agent.session("default").facts["documents"][0]["text"]) == 6000


def test_disabled_research_and_invalid_input_are_actionable(client):
    app.state.agent.research_runner = None
    assert client.get("/health").json()["research_configured"] is False
    assert client.post("/ask", json={"question": "research", "mode": "research"}).status_code == 503
    for payload in ({"question": "   "}, {"question": "x", "mode": "invalid"}, {"question": "x" * 2001}):
        assert client.post("/ask", json=payload).status_code == 422


def test_research_keeps_existing_analysis_artifact_and_its_citations(client):
    import pandas as pd
    from backend.agent.state import ColumnLineage
    session = app.state.agent.session("default")
    citation = {"table": "macro_observations", "unit": "%"}
    session.artifact.add_column("rate", pd.Series([10.0], index=pd.to_datetime(["2026-01-01"])),
                                ColumnLineage("rate", "Rate", "macro", "%", "rate", citation=citation))
    session.cite(citation)
    response = client.post("/ask", json={"question": "Read report", "mode": "research"})
    assert response.status_code == 200
    assert session.artifact.frame["rate"].tolist() == [10.0]
    assert response.json()["table"]["rows"] == [{"period": "2026-01-01", "rate": 10.0}]
    assert session.citations == [citation]



def test_concurrent_writers_keep_results_attached_to_their_own_run(tmp_path):
    store = EvidenceStore(tmp_path / "research.sqlite3")

    def save(i):
        session = f"s{i}"
        run_id = store.start(session, "question", "research")
        for j in range(4):
            store.record(run_id, "search_web", {"query": f"q{j}"}, {"status": "ok", "session": session})
        store.finish(run_id, {"summary": session}, "ok")
        return session, run_id

    with ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(save, range(8)))
    for session, run_id in runs:
        saved = store.get_run(session, run_id)
        assert len(saved["tool_results"]) == 4
        assert all(item["output"]["session"] == session for item in saved["tool_results"])
