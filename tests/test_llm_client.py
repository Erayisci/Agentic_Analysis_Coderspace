"""Tests for backend.llm.client -- no real network calls.

`KloudeksClient._post` goes through `self._client.post` (an `httpx.Client`),
so every test here replaces that one method with a fake that records the
request and returns a canned response, matching the rest of the repo's
philosophy that the test suite never depends on a remote server.
"""
from types import SimpleNamespace

import pytest

from backend.core.config import KLOUDEKS_OCR_MODEL
from backend.llm.client import KloudeksClient, LLMError


def _client(monkeypatch, response_json, status_code=200):
    client = KloudeksClient(api_key="test-key")
    calls = []

    def fake_post(url, json):
        calls.append((url, json))
        return SimpleNamespace(status_code=status_code, json=lambda: response_json)

    monkeypatch.setattr(client._client, "post", fake_post)
    return client, calls


def test_ocr_sends_the_exact_payload_the_mia_guide_specifies(monkeypatch):
    client, calls = _client(monkeypatch, {
        "choices": [{"message": {"content": "Extracted table text"}}],
    })

    result = client.ocr(b"\x89PNG-fake-bytes", max_tokens=512)

    assert result == "Extracted table text"
    assert len(calls) == 1
    url, payload = calls[0]
    assert url.endswith("/chat/completions")
    assert payload["model"] == KLOUDEKS_OCR_MODEL
    assert payload["max_tokens"] == 512
    assert payload["temperature"] == 0.0
    assert payload["skip_special_tokens"] is False
    assert payload["vllm_xargs"] == {"ngram_size": 35, "window_size": 128}
    # No chat_template_kwargs/reasoning-headroom multiplication: those are
    # _chat_payload's rules for the planning/composing models, not OCR's.
    assert "chat_template_kwargs" not in payload

    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1] == {"type": "text", "text": "<image>\ndocument parsing"}


def test_ocr_raises_llm_error_on_empty_content(monkeypatch):
    client, _ = _client(monkeypatch, {"choices": [{"message": {}}]})

    with pytest.raises(LLMError, match="no message content"):
        client.ocr(b"\x89PNG")


def test_ocr_raises_llm_error_on_http_failure(monkeypatch):
    client, _ = _client(monkeypatch, {}, status_code=500)

    with pytest.raises(LLMError, match="HTTP 500"):
        client.ocr(b"\x89PNG")
