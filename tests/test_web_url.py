"""Tests for backend.tools.web_url -- no real network calls.

Every test that goes through `read_url` mocks `requests.get` with a canned
response, matching the rest of the repo's philosophy that the test suite
never depends on a remote server being reachable.
"""
from io import BytesIO
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.tools import web_url


def _response(content_type, content=b"", status_code=200, encoding=None, redirect_to=None):
    """A minimal stand-in for requests.Response covering what read_url uses."""
    headers = {"Content-Type": content_type}
    is_redirect = redirect_to is not None
    if is_redirect:
        headers["Location"] = redirect_to

    def raise_for_status():
        if status_code >= 400:
            raise web_url.requests.HTTPError(f"{status_code} error")

    return SimpleNamespace(
        headers=headers,
        content=content,
        status_code=status_code,
        encoding=encoding,
        is_redirect=is_redirect,
        is_permanent_redirect=False,
        raise_for_status=raise_for_status,
    )


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every hostname in these tests resolves as a public address, so the
    SSRF guard (tested separately below) never interferes with the dispatch
    and extraction tests."""
    monkeypatch.setattr(
        web_url.socket, "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )


# --- content-kind dispatch ---------------------------------------------------

def test_dispatches_pdf_by_content_type(monkeypatch):
    reader = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: "hello")])
    monkeypatch.setattr(web_url, "PdfReader", lambda buf: reader)
    monkeypatch.setattr(web_url, "_fetch", lambda url: _response("application/pdf", b"%PDF-fake"))

    result = web_url.read_url("https://example.com/report")

    assert result["kind"] == "pdf"
    assert result["text"] == "hello"
    assert result["n_pages"] == 1


def test_dispatches_pdf_by_extension_when_content_type_is_generic(monkeypatch):
    reader = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: "x")])
    monkeypatch.setattr(web_url, "PdfReader", lambda buf: reader)
    monkeypatch.setattr(web_url, "_fetch", lambda url: _response("application/octet-stream", b"%PDF-fake"))

    result = web_url.read_url("https://example.com/files/report.pdf")

    assert result["kind"] == "pdf"


def test_dispatches_excel_and_previews_every_sheet(monkeypatch):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame({"a": [1, 2], "b": [3, 4]}).to_excel(writer, sheet_name="Sheet1", index=False)
        pd.DataFrame({"x": [9]}).to_excel(writer, sheet_name="Sheet2", index=False)
    content = buf.getvalue()

    monkeypatch.setattr(
        web_url, "_fetch", lambda url: _response(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", content,
        ),
    )

    result = web_url.read_url("https://example.com/data.xlsx")

    assert result["kind"] == "excel"
    assert set(result["sheet_names"]) == {"Sheet1", "Sheet2"}
    assert result["sheets"]["Sheet1"]["columns"] == ["a", "b"]
    assert result["sheets"]["Sheet1"]["n_rows"] == 2


def test_dispatches_html_and_strips_scripts(monkeypatch):
    html = b"<html><head><title> My Page </title></head><body><script>evil()</script><p>Hello world</p></body></html>"
    monkeypatch.setattr(web_url, "_fetch", lambda url: _response("text/html; charset=utf-8", html))

    result = web_url.read_url("https://example.com/page")

    assert result["kind"] == "html"
    assert result["title"] == "My Page"
    assert "evil()" not in result["text"]
    assert "Hello world" in result["text"]


def test_dispatches_plain_text(monkeypatch):
    monkeypatch.setattr(web_url, "_fetch", lambda url: _response("text/plain", b"raw data", encoding="utf-8"))

    result = web_url.read_url("https://example.com/data.txt")

    assert result["kind"] == "text"
    assert result["text"] == "raw data"


def test_image_without_ocr_raises_runtime_error(monkeypatch):
    monkeypatch.setattr(web_url, "_fetch", lambda url: _response("image/png", b"\x89PNG"))

    with pytest.raises(RuntimeError, match="no OCR callable configured"):
        web_url.read_url("https://example.com/chart.png")


def test_image_with_ocr_returns_extracted_text(monkeypatch):
    monkeypatch.setattr(web_url, "_fetch", lambda url: _response("image/png", b"\x89PNG-fake-bytes"))
    seen = {}

    def fake_ocr(image_bytes):
        seen["bytes"] = image_bytes
        return "Tablo: Ocak 100, Subat 120"

    result = web_url.read_url("https://example.com/chart.png", ocr=fake_ocr)

    assert seen["bytes"] == b"\x89PNG-fake-bytes"
    assert result["kind"] == "image"
    assert result["text"] == "Tablo: Ocak 100, Subat 120"
    assert result["truncated"] is False


def test_unrecognised_content_type_raises(monkeypatch):
    monkeypatch.setattr(web_url, "_fetch", lambda url: _response("application/x-bogus", b"???"))

    with pytest.raises(ValueError, match="unrecognised content type"):
        web_url.read_url("https://example.com/mystery")


def test_long_text_is_truncated(monkeypatch):
    long_text = "a" * (web_url.MAX_TEXT_CHARS + 100)
    monkeypatch.setattr(web_url, "_fetch", lambda url: _response("text/plain", long_text.encode(), encoding="utf-8"))

    result = web_url.read_url("https://example.com/big.txt")

    assert result["truncated"] is True
    assert len(result["text"]) == web_url.MAX_TEXT_CHARS


def test_json_content_type_dispatches_as_text(monkeypatch):
    monkeypatch.setattr(web_url, "_fetch", lambda url: _response("application/json", b'{"a": 1}', encoding="utf-8"))

    result = web_url.read_url("https://example.com/api/data")

    assert result["kind"] == "text"
    assert result["text"] == '{"a": 1}'


def test_non_utf8_encoding_is_honoured(monkeypatch):
    content = "Türkçe metin".encode("iso-8859-9")
    monkeypatch.setattr(web_url, "_fetch", lambda url: _response("text/plain", content, encoding="iso-8859-9"))

    result = web_url.read_url("https://example.com/legacy.txt")

    assert result["text"] == "Türkçe metin"


@pytest.mark.parametrize("extractor,kind", [
    (lambda text: web_url._extract_html(f"<p>{text}</p>".encode()), "html"),
    (lambda text: web_url._extract_text(text.encode(), "utf-8"), "text"),
])
def test_truncation_boundary_is_exact_for_every_extractor(extractor, kind):
    """Every text-bearing extractor shares _bounded(), so the boundary
    behaves identically for all of them instead of drifting independently."""
    exactly_at_limit = "a" * web_url.MAX_TEXT_CHARS
    one_over = exactly_at_limit + "a"

    assert extractor(exactly_at_limit)["truncated"] is False
    assert extractor(one_over)["truncated"] is True
    assert len(extractor(one_over)["text"]) == web_url.MAX_TEXT_CHARS


# --- HTTP errors and redirects ------------------------------------------------

def test_http_error_status_propagates(monkeypatch):
    """Exercises the real _fetch (only requests.get is faked), so this also
    covers that raise_for_status() is actually reached on the happy path."""
    monkeypatch.setattr(
        web_url.requests, "get",
        lambda url, timeout, headers, allow_redirects: _response("text/html", b"", status_code=404),
    )

    with pytest.raises(web_url.requests.HTTPError):
        web_url.read_url("https://example.com/missing")


def test_follows_a_safe_redirect_to_completion(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, timeout, headers, allow_redirects):
        calls["n"] += 1
        if calls["n"] == 1:
            return _response("text/html", redirect_to="https://example.com/final")
        return _response("text/plain", b"landed", encoding="utf-8")

    monkeypatch.setattr(web_url.requests, "get", fake_get)

    result = web_url.read_url("https://example.com/start")

    assert result["kind"] == "text"
    assert result["text"] == "landed"
    assert calls["n"] == 2


def test_too_many_redirects_raises(monkeypatch):
    def fake_get(url, timeout, headers, allow_redirects):
        return _response("text/html", redirect_to="https://example.com/next")

    monkeypatch.setattr(web_url.requests, "get", fake_get)

    with pytest.raises(ValueError, match="too many redirects"):
        web_url.read_url("https://example.com/loop")


# --- SSRF guard ---------------------------------------------------------------

@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1"])
def test_rejects_non_public_addresses(monkeypatch, ip):
    monkeypatch.setattr(web_url.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", (ip, 0))])

    with pytest.raises(ValueError, match="non-public address"):
        web_url.read_url("http://internal.example/x")


def test_rejects_unsupported_scheme():
    with pytest.raises(ValueError, match="unsupported URL scheme"):
        web_url.read_url("ftp://example.com/file")


def test_rejects_unresolvable_host(monkeypatch):
    import socket as socket_module

    def raise_gaierror(host, port):
        raise socket_module.gaierror("nope")

    monkeypatch.setattr(web_url.socket, "getaddrinfo", raise_gaierror)

    with pytest.raises(ValueError, match="could not resolve host"):
        web_url.read_url("http://this-does-not-resolve.invalid/x")


def test_redirect_to_private_address_is_blocked(monkeypatch):
    """A public host redirecting to a private one must not be followed --
    this is the SSRF-via-redirect bypass the hop-by-hop guard exists for.

    Overrides the module-level `public_dns` fixture with a hostname-aware
    fake, since this test needs the *redirect target* to resolve privately
    while the original host still resolves publicly.
    """
    def fake_getaddrinfo(host, port):
        ip = "169.254.169.254" if host == "169.254.169.254" else "93.184.216.34"
        return [(2, 1, 6, "", (ip, 0))]

    monkeypatch.setattr(web_url.socket, "getaddrinfo", fake_getaddrinfo)

    calls = {"n": 0}

    def fake_get(url, timeout, headers, allow_redirects):
        calls["n"] += 1
        if calls["n"] == 1:
            return _response("text/html", redirect_to="http://169.254.169.254/secret")
        raise AssertionError("must not follow the redirect to a private address")

    monkeypatch.setattr(web_url.requests, "get", fake_get)

    with pytest.raises(ValueError, match="non-public address"):
        web_url.read_url("https://public.example/redirector")
