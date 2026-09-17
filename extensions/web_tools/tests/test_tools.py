"""Run with unittest: no Docker, third-party packages, or external websites."""

import json
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from backend.extensions.web_tools import client
from backend.extensions.web_tools.client import ToolFailure, WebTools, _request_json
from backend.extensions.web_tools.config import WebToolsConfig
from backend.tools import get_tools


def search_result(**overrides):
    return {"title": "<b>Bank</b> &amp; credit", "url": "https://www.example.com/report?q=credit#table",
            "content": "A <em>source</em>.<script>secret()</script>", "engines": ["duckduckgo", "bing"], **overrides}


def crawl_result(**overrides):
    return {"status": "ok", "requested_url": "https://example.com/", "final_url": "https://example.com/article",
            "title": "Page", "content": "Readable content", "content_type": "text/html",
            "fetched_at": "2026-09-13T10:00:00+00:00", "original_chars": 16, "returned_chars": 16,
            "max_chars": 20000, "truncated": False, "error": None, **overrides}


class ToolContractTests(unittest.TestCase):
    def setUp(self):
        self.web = WebTools(WebToolsConfig())

    def test_disabled_registry_does_not_import_extensions(self):
        script = """
import sys
from backend.tools import get_tools
assert get_tools({'WEB_TOOLS_ENABLED': 'false', 'WEB_SEARXNG_URL': 'bad', 'WEB_RETRIES': 'invalid'}) == {}
assert not any(name.startswith(('crawl4ai', 'playwright', 'backend.extensions')) for name in sys.modules)
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_enabled_registration_and_invocation(self):
        def request(url, timeout, body=None):
            return {"results": [search_result()]} if body is None else crawl_result()

        with patch.object(client, "_request_json", side_effect=request) as http:
            tools = get_tools({"WEB_TOOLS_ENABLED": "true"})
            self.assertEqual(set(tools), {"search_web", "read_url", "read_web_url"})
            self.assertEqual(tools["search_web"]("bank")["status"], "ok")
            self.assertEqual(tools["read_url"]("https://example.com")["status"], "ok")
            self.assertEqual(http.call_args.args[2], {"url": "https://example.com/", "max_chars": 20000})
            self.assertEqual(http.call_args.args[1], 50)
        self.assertFalse(any(name.startswith(("crawl4ai", "playwright")) for name in sys.modules))

    def test_invalid_configuration_is_redacted(self):
        settings = [
            {"WEB_TOOLS_ENABLED": "tru"}, {"WEB_MAX_RESULTS": "0"}, {"WEB_CRAWL_TIMEOUT_SECONDS": "nan"},
            {"WEB_SEARCH_TIMEOUT_SECONDS": "inf"}, {"WEB_MAX_CONTENT_CHARS": "99"},
            {"WEB_MAX_CONCURRENCY": "100"}, {"WEB_RETRIES": "4"},
            {"WEB_SEARXNG_URL": "http://user:supersecret@search:8080"},
            {"WEB_SEARXNG_URL": "http://@search:8080"},
            {"WEB_CRAWLER_URL": "http://crawler:8000/?key=supersecret"},
        ]
        for values in settings:
            with self.subTest(values=values), self.assertRaises(ValueError) as exc:
                get_tools({"WEB_TOOLS_ENABLED": "true", **values})
            self.assertNotIn("supersecret", str(exc.exception))

    def test_internal_endpoints_allow_container_names(self):
        config = WebToolsConfig.from_environ({"WEB_SEARXNG_URL": "http://searxng:8080/", "WEB_CRAWLER_URL": "http://crawler:8932"})
        self.assertEqual(config.searxng_url, "http://searxng:8080")

    def test_search_sources_markup_and_filters(self):
        payload = {"results": [search_result(publishedDate="2026-06-01"),
                               search_result(url="https://example.com.evil.org/report"),
                               search_result(url="https://example.com/second", content=None)]}
        with patch.object(client, "_request_json", return_value=payload) as request:
            result = self.web.search_web("credit", max_results=200, language="tr-TR", time_range="year", domains=["example.com"])
        params = parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertEqual(params["format"], ["json"])
        self.assertEqual(params["language"], ["tr-TR"])
        self.assertEqual(params["time_range"], ["year"])
        self.assertIn("site:example.com", params["q"][0])
        self.assertEqual(result["results"][0], {
            "title": "Bank & credit", "url": "https://www.example.com/report?q=credit#table",
            "snippet": "A source.", "engines": ["duckduckgo", "bing"], "published_at": "2026-06-01"})
        self.assertEqual(result["source_trust"], "untrusted_external")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][1]["snippet"], "")

    def test_search_count_dates_and_deduplication(self):
        payload = {"results": [search_result(), search_result(), search_result(url="https://example.com/other"), search_result(url="https://example.com/third")]}
        with patch.object(client, "_request_json", return_value=payload):
            result = WebTools(WebToolsConfig(max_results=2)).search_web("query", max_results=9)
        self.assertEqual(len(result["results"]), 2)
        self.assertTrue(all("published_at" not in item for item in result["results"]))

    def test_empty_partial_and_malformed_search(self):
        cases = [
            ({"results": []}, "empty"),
            ({"results": [], "unresponsive_engines": [["bing", "Suspended: too many requests"]]}, "partial"),
            ({"results": [search_result()], "unresponsive_engines": [["google", "CAPTCHA https://user:supersecret@x"]]}, "partial"),
            ({"results": [search_result(), {"url": 1}]}, "partial"),
            ({"results": [{"url": 1}]}, "error"), ({"results": {}}, "error"), ({"oops": []}, "error"),
        ]
        for payload, status in cases:
            with self.subTest(payload=payload), patch.object(client, "_request_json", return_value=payload):
                result = self.web.search_web("query")
                self.assertEqual(result["status"], status)
                self.assertNotIn("supersecret", json.dumps(result))

    def test_invalid_search_never_reaches_network(self):
        cases = [
            {"query": ""}, {"query": "x" * 2001}, {"query": "a\nb"}, {"query": None},
            {"query": "q", "max_results": True}, {"query": "q", "max_results": 0},
            {"query": "q", "time_range": "week"}, {"query": "q", "language": "tr&engines=paid"},
            {"query": "q", "domains": "example.com"}, {"query": "q", "domains": ["example.com OR evil.org"]},
            {"query": "q", "domains": ["http://example.com"]}, {"query": "q", "domains": ["localhost"]},
        ]
        with patch.object(client, "_request_json") as request:
            for kwargs in cases:
                with self.subTest(kwargs=kwargs):
                    self.assertEqual(self.web.search_web(**kwargs)["error"]["code"], "invalid_request")
            request.assert_not_called()

    def test_retry_limits_and_recovery(self):
        for retryable, retries, expected in [(True, 0, 1), (True, 2, 3), (False, 3, 1)]:
            with self.subTest(retries=retries, retryable=retryable):
                failure = ToolFailure("timeout" if retryable else "malformed_response", "safe failure", retryable)
                with patch.object(client, "_request_json", side_effect=failure) as request, patch.object(client.time, "sleep") as sleep:
                    result = WebTools(WebToolsConfig(retries=retries)).search_web("query")
                self.assertEqual(result["status"], "error")
                self.assertEqual(request.call_count, expected)
                self.assertEqual(sleep.call_count, expected - 1)
        with patch.object(client, "_request_json", side_effect=[ToolFailure("timeout", "timeout", True), {"results": []}]), patch.object(client.time, "sleep"):
            self.assertEqual(self.web.search_web("query")["status"], "empty")

    def test_concurrency_bounded_and_released(self):
        web = WebTools(WebToolsConfig(max_concurrency=1, retries=0))
        entered, release = threading.Event(), threading.Event()

        def request(*args):
            entered.set()
            release.wait(2)
            return {"results": []}

        with patch.object(client, "_request_json", side_effect=request):
            thread = threading.Thread(target=web.search_web, args=("first",))
            thread.start()
            self.assertTrue(entered.wait(2))
            try:
                self.assertEqual(web.search_web("second")["error"]["code"], "busy")
            finally:
                release.set()
                thread.join(2)
            self.assertEqual(web.search_web("third")["status"], "empty")

    def test_read_truncation_and_metadata(self):
        with patch.object(client, "_request_json", return_value=crawl_result(content="x" * 300, original_chars=300, returned_chars=300)):
            result = self.web.read_url("https://example.com", max_chars=100)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["content"], "x" * 100)
        self.assertEqual(result["returned_chars"], 100)
        self.assertEqual(result["original_chars"], 300)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["final_url"], "https://example.com/article")
        self.assertEqual(result["fetched_at"], "2026-09-13T10:00:00+00:00")
        self.assertEqual(result["source_trust"], "untrusted_external")

    def test_read_unsafe_inputs_and_limits(self):
        with patch.object(client, "_request_json") as request:
            for url in ["file:///etc/passwd", "http://127.0.0.1", "http://localhost", "http://user:supersecret@example.com", "http://169.254.169.254/latest/meta-data"]:
                with self.subTest(url=url):
                    result = self.web.read_url(url)
                    self.assertEqual(result["error"]["code"], "invalid_url")
                    self.assertNotIn("supersecret", json.dumps(result))
            for limit in [0, 99, True, "100", 1.5]:
                with self.subTest(limit=limit):
                    self.assertEqual(self.web.read_url("https://example.com", max_chars=limit)["error"]["code"], "invalid_request")
            request.assert_not_called()

    def test_read_error_redaction_and_payload_validation(self):
        cases = [
            ({"status": "error", "error": {"code": "unsupported_content_type", "message": "supersecret"}}, "unsupported_content_type"),
            ({"status": "error", "error": {"code": "invalid_url", "message": "supersecret"}}, "invalid_url"),
            ({"status": "error", "error": {"code": "certificate_error", "message": "supersecret"}}, "certificate_error"),
            ({"status": "error", "error": None}, "malformed_response"),
            (crawl_result(final_url="http://127.0.0.1/secret"), "malformed_response"),
            (crawl_result(content=None), "malformed_response"), (crawl_result(original_chars=-1), "malformed_response"),
            (crawl_result(content_type=None), "malformed_response"), (crawl_result(content_type="application/pdf"), "malformed_response"),
            (crawl_result(returned_chars=1), "malformed_response"), (crawl_result(original_chars=100), "malformed_response"),
            (crawl_result(status="empty"), "malformed_response"),
        ]
        for payload, code in cases:
            with self.subTest(payload=payload), patch.object(client, "_request_json", return_value=payload):
                result = self.web.read_url("https://example.com")
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], code)
                self.assertNotIn("supersecret", json.dumps(result))

    def test_empty_page_and_worker_truncation(self):
        with patch.object(client, "_request_json", return_value=crawl_result(status="empty", content="", original_chars=0, returned_chars=0)):
            self.assertEqual(self.web.read_url("https://example.com")["status"], "empty")
        with patch.object(client, "_request_json", return_value=crawl_result(original_chars=100, truncated=True)):
            result = self.web.read_url("https://example.com")
            self.assertTrue(result["truncated"])
            self.assertEqual(result["original_chars"], 100)


class HttpTransportTests(unittest.TestCase):
    def exchange(self, data, delay=0):
        """Real HTTP over AF_UNIX; no network listening port or website required."""
        local, remote = socket.socketpair()
        received = []

        def connect(connection):
            connection.sock = local

        def serve():
            try:
                received.append(remote.recv(65536))
                if delay:
                    for byte in data:
                        remote.sendall(bytes([byte]))
                        time.sleep(delay)
                else:
                    remote.sendall(data)
            except (BrokenPipeError, OSError):
                pass
            finally:
                remote.close()

        patcher = patch.object(client.http.client.HTTPConnection, "connect", connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 1)
        self.addCleanup(local.close)
        return received

    def test_json_and_environment_proxy_ignored(self):
        received = self.exchange(b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: 14\r\n\r\n{"results":[]}')
        with patch.dict("os.environ", {"HTTP_PROXY": "http://user:supersecret@untrusted.invalid:80"}):
            self.assertEqual(_request_json("http://search:8080/search?q=bank&format=json", 1), {"results": []})
        self.assertIn(b"GET /search?q=bank&format=json HTTP/1.1", received[0])
        self.assertNotIn(b"Authorization", received[0])
        self.assertNotIn(b"untrusted", received[0])

    def test_failure_statuses_and_redirects(self):
        for status, code, retryable in [(403, "json_forbidden", False), (429, "service_http_error", True), (503, "service_http_error", True), (302, "service_redirect", False)]:
            with self.subTest(status=status):
                self.exchange(f"HTTP/1.0 {status} Error\r\nLocation: http://user:supersecret@127.0.0.1/\r\n\r\n".encode())
                with self.assertRaises(ToolFailure) as exc:
                    _request_json("http://search:8080/search", 1)
                self.assertEqual(exc.exception.error["code"], code)
                self.assertEqual(exc.exception.error["retryable"], retryable)
                self.assertNotIn("supersecret", str(exc.exception))

    def test_malformed_and_oversized_json(self):
        for body, content_type, code in [(b"broken", "application/json", "malformed_response"),
                                         (b"[]", "application/json", "malformed_response"),
                                         (b"html", "text/html", "malformed_response"),
                                         (b"x" * 101, "application/json", "response_too_large")]:
            with self.subTest(code=code, body=body):
                self.exchange(f"HTTP/1.0 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
                with patch.object(client, "MAX_RESPONSE_BYTES", 100), self.assertRaises(ToolFailure) as exc:
                    _request_json("http://search:8080/search", 1)
                self.assertEqual(exc.exception.error["code"], code)

    def test_total_deadline_stops_trickling_headers(self):
        self.exchange(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{}", delay=0.02)
        started = time.monotonic()
        with self.assertRaises(ToolFailure) as exc:
            _request_json("http://search:8080/search", 0.12)
        self.assertEqual(exc.exception.error["code"], "timeout")
        self.assertLess(time.monotonic() - started, 0.6)

    def test_connection_error_redacted(self):
        with patch.object(client.http.client.HTTPConnection, "connect", side_effect=OSError("http://user:supersecret@service")), self.assertRaises(ToolFailure) as exc:
            _request_json("http://search:8080/search", 1)
        self.assertEqual(exc.exception.error["code"], "service_unavailable")
        self.assertNotIn("supersecret", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
