"""Asset flags, limits, caching and MIA protocol without optional packages/network."""

from dataclasses import replace
import json
import http.client
import hashlib
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from backend.extensions.web_tools.asset_config import AssetConfig
from backend.extensions.web_tools.asset_common import AssetFailure, normalize
from backend.extensions.web_tools.asset_cache import AssetStore
from backend.extensions.web_tools.asset_download import download
from backend.extensions.web_tools import asset_worker
from backend.extensions.web_tools.worker import WorkerHandler
from backend.extensions.web_tools.egress import BoundedHTTPServer
from backend.extensions.web_tools.page_assets import public_assets
from backend.extensions.web_tools.security import UnsafeURL
from backend.model_clients.kloudeks import KloudeksClient, ModelFailure
from backend.tools import get_tools


class AssetPolicyTests(unittest.TestCase):
    def test_flags_control_registration_without_importing_parsers(self):
        script = """
import sys
from backend.tools import get_tools
assert get_tools({'WEB_TOOLS_ENABLED': 'false', 'WEB_DOCUMENTS_ENABLED': 'true'}) == {}
assert set(get_tools({'WEB_TOOLS_ENABLED': 'true'})) == {'search_web', 'read_url', 'read_web_url'}
tools = get_tools({'WEB_TOOLS_ENABLED': 'true', 'WEB_DOCUMENTS_ENABLED': 'true', 'WEB_IMAGES_ENABLED': 'true', 'WEB_LINKS_ENABLED': 'true'})
assert {'read_document', 'read_image', 'get_page_assets'} <= tools.keys()
assert not any(name in sys.modules for name in ('pypdf', 'pdfplumber', 'openpyxl', 'PIL', 'playwright', 'crawl4ai'))
"""
        subprocess.run([__import__('sys').executable, '-S', '-c', script], check=True)

    def test_disabled_features_are_rejected_before_download(self):
        payload = {"url": "https://example.com/file.pdf", "kind": "document"}
        with patch("backend.extensions.web_tools.asset_download.download") as request:
            with self.assertRaises(AssetFailure) as error:
                asset_worker.process(payload, "http://egress:3128", AssetConfig())
            self.assertEqual(error.exception.code, "feature_disabled")
            request.assert_not_called()
        config = AssetConfig(documents_enabled=True)
        for toggle in ("ocr", "vision"):
            with self.subTest(toggle=toggle), self.assertRaises(AssetFailure):
                normalize(payload | {toggle: True}, config)

    def test_service_caps_requests_and_rejects_extra_configuration(self):
        config = AssetConfig(documents_enabled=True, asset_max_pages=2, asset_max_chars=1000)
        payload = {"url": "https://example.com/report.pdf", "kind": "document", "max_pages": 100, "max_chars": 99999}
        result = normalize(payload, config)
        self.assertEqual((result["max_pages"], result["max_chars"]), (2, 1000))
        for extra in ({"model": "other"}, {"api_key": "private"}, {"proxy": "http://localhost"},
                      {"ocr": "false"}, {"refresh": "false"}, {"start_page": 0}, {"max_pages": True}):
            with self.subTest(extra=extra), self.assertRaises(AssetFailure):
                normalize(payload | extra, config)
        for url in ("http://127.0.0.1/a.pdf", "http://169.254.169.254/", "file:///secret"):
            with self.subTest(url=url), self.assertRaises(AssetFailure):
                normalize(payload | {"url": url}, config)

    def test_invalid_configuration_never_discloses_secret(self):
        for env in ({"WEB_DOCUMENTS_ENABLED": "maybe"}, {"WEB_ASSET_MAX_PAGES": "0"},
                    {"WEB_ASSET_TIMEOUT_SECONDS": "nan"}, {"WEB_OCR_PROVIDER": "paid-other"},
                    {"WEB_MODEL_MAX_TOKENS": "999999"},
                    {"WEB_KLOUDEKS_BASE_URL": "https://user:secret@example.org/v1"}):
            with self.subTest(env=env), self.assertRaises(ValueError) as error:
                AssetConfig.from_environ(env | {"WEB_KLOUDEKS_API_KEY": "supersecret"})
            self.assertNotIn("supersecret", str(error.exception))
        self.assertNotIn("supersecret", repr(AssetConfig(kloudeks_api_key="supersecret")))

    def test_team_kloudeks_api_key_is_accepted_but_web_specific_name_wins(self):
        self.assertEqual(AssetConfig.from_environ({"KLOUDEKS_API_KEY": "team"}).kloudeks_api_key, "team")
        self.assertEqual(AssetConfig.from_environ({"MIA_API_KEY": "mia", "KLOUDEKS_API_KEY": "team"}).kloudeks_api_key, "mia")
        self.assertEqual(AssetConfig.from_environ(
            {"WEB_KLOUDEKS_API_KEY": "web", "KLOUDEKS_API_KEY": "team"}).kloudeks_api_key, "web")

    def test_assets_are_public_deduplicated_and_bounded(self):
        values = [{"url": "https://example.com/navigation", "text": "Menu"},
                  {"url": "https://example.com/report.pdf", "text": "Report"},
                  {"url": "https://example.com/report.pdf", "text": "Duplicate"},
                  {"url": "http://127.0.0.1/secret"}, {"url": "data:image/png;base64,abc"},
                  {"url": "https://example.com/chart.png", "text": "Chart"}]
        self.assertEqual([v["type_hint"] for v in public_assets(values, 2)], ["pdf", "png"])
        self.assertEqual(len(public_assets(values, 1)), 1)
        downloads = [{"url": "https://example.com/menu"},
                     {"url": "https://example.com/file?id=12", "text": "Dokuman Linki Annual report"}]
        self.assertEqual(public_assets(downloads, 1)[0]["type_hint"], "document")
        self.assertEqual(public_assets([{"url": "https://example.com/file", "download": True}], 1)[0]["type_hint"], "document")

    def test_child_timeout_kills_descendants(self):
        process = Mock(pid=54321)
        process.communicate.side_effect = [subprocess.TimeoutExpired("asset", 1), (b"", b"")]
        with patch.object(asset_worker.os, "killpg") as kill:
            result = asset_worker.run_isolated({"url": "https://example.com/a.pdf"}, "http://egress:3128", 1,
                                               popen=Mock(return_value=process))
        self.assertEqual(result["error"]["code"], "timeout")
        kill.assert_called_once_with(54321, asset_worker.signal.SIGKILL)

    def test_http_service_enforces_flags_and_caps_independently_of_client(self):
        with BoundedHTTPServer(("127.0.0.1", 0), WorkerHandler) as server:
            server.read_slots = threading.BoundedSemaphore(2)
            server.asset_slots = threading.BoundedSemaphore(1)
            server.proxy = "http://egress:3128"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for enabled in (False, True):
                    server.asset_config = AssetConfig(documents_enabled=enabled, asset_max_pages=2, asset_max_chars=1000)
                    client = http.client.HTTPConnection(*server.server_address, timeout=3)
                    with patch.object(asset_worker, "run_isolated", return_value={"status": "ok"}) as run:
                        try:
                            client.request("POST", "/asset", body=json.dumps({"kind": "document", "url": "https://example.com/file.pdf",
                                                                            "max_pages": 999, "max_chars": 99999}),
                                           headers={"Content-Type": "application/json"})
                            result = json.loads(client.getresponse().read())
                            if enabled:
                                self.assertEqual(run.call_args.args[0]["max_pages"], 2)
                                self.assertEqual(run.call_args.args[0]["max_chars"], 1000)
                            else:
                                self.assertEqual(result["error"]["code"], "feature_disabled")
                                run.assert_not_called()
                        finally:
                            client.close()
            finally:
                server.shutdown()
                thread.join()

    def test_download_rejects_private_redirects_and_oversize(self):
        with tempfile.TemporaryDirectory() as directory:
            for status, headers, code in ((302, {"Location": "http://127.0.0.1/file"}, UnsafeURL),
                                          (200, {"Content-Length": "5000"}, AssetFailure)):
                response = Mock(status=status)
                response.getheader.side_effect = lambda key, default=None: headers.get(key, default)
                connection = Mock()
                connection.getresponse.return_value = response
                with self.subTest(status=status), self.assertRaises(code):
                    download("https://example.com/a.pdf", "http://egress:3128", Path(directory) / "asset", 1024, 1,
                             connection_factory=Mock(return_value=connection))
                connection.close.assert_called_once()

    def test_download_fingerprint_covers_full_bytes_and_worker_uses_lower_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = b"column,value\nMarch,12345\n"
            headers = {"Content-Type": "text/csv", "Content-Length": str(len(raw))}
            response = Mock(status=200)
            response.getheader.side_effect = lambda key, default=None: headers.get(key, default)
            response.read.side_effect = [raw[:10], raw[10:], b""]
            connection = Mock()
            connection.getresponse.return_value = response
            path = Path(directory) / "asset"
            metadata = download("https://example.com/file", "http://egress:3128", path, 1024, 1,
                                connection_factory=Mock(return_value=connection))
            self.assertEqual(metadata["content_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(metadata["downloaded_bytes"], len(raw))
            self.assertEqual(path.read_bytes(), raw)
        with patch("backend.extensions.web_tools.asset_download.download", return_value=metadata) as fetch, patch(
                "backend.extensions.web_tools.asset_extract.extract", return_value={"status": "ok"}):
            asset_worker.process({"url": "https://example.com/a.pdf", "kind": "auto", "max_bytes": 2048},
                                 "http://egress:3128", AssetConfig(documents_enabled=True))
        self.assertEqual(fetch.call_args.args[3], 2048)

    def test_research_endpoint_checks_server_flag_and_context_limit(self):
        with BoundedHTTPServer(("127.0.0.1", 0), WorkerHandler) as server:
            server.read_slots = threading.BoundedSemaphore(2)
            server.asset_slots = threading.BoundedSemaphore(1)
            server.proxy = "http://egress:3128"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for enabled, context, expected in ((False, {}, "feature_disabled"),
                                                    (True, {"text": "x" * 2100}, "invalid_request"),
                                                    (True, {}, None)):
                    server.asset_config = AssetConfig(agent_enabled=enabled, agent_max_context_chars=2000)
                    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                    with patch.object(asset_worker, "run_isolated", return_value={"status": "ok"}) as run:
                        try:
                            connection.request("POST", "/agent-model", body=json.dumps({"question": "question", "context": context}),
                                               headers={"Content-Type": "application/json"})
                            result = json.loads(connection.getresponse().read())
                            if expected:
                                self.assertEqual(result["error"]["code"], expected)
                                run.assert_not_called()
                            else:
                                self.assertEqual(run.call_args.args[0]["operation"], "agent-model")
                                self.assertEqual(result["status"], "ok")
                        finally:
                            connection.close()
            finally:
                server.shutdown()
                thread.join()

    def test_cache_expiry_eviction_and_shared_model_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            config = AssetConfig(asset_cache_ttl_seconds=60, asset_cache_max_bytes=1048576, model_max_calls_per_hour=1)
            store = AssetStore(config, directory)
            result = {"status": "ok", "content": "x" * 600000, "fetched_at": "source-time"}
            with patch("backend.extensions.web_tools.asset_cache.time.time", return_value=10000):
                store.put("first", result)
                store.put("second", result)
                self.assertIsNone(store.get("first"))
                self.assertTrue(store.get("second")["cache"]["hit"])
                store.consume_model_call()
                with self.assertRaises(AssetFailure):
                    AssetStore(config, directory).consume_model_call()
            with patch("backend.extensions.web_tools.asset_cache.time.time", return_value=10100):
                self.assertIsNone(store.get("second"))
            key = store.key({"url": "https://example.com"})
            self.assertEqual(key, AssetStore(replace(config, kloudeks_api_key="secret"), directory).key({"url": "https://example.com"}))


class MIAProtocolTests(unittest.TestCase):
    def connection(self, response_data=None, status=200):
        response = Mock(status=status)
        response.read.return_value = json.dumps(response_data or {
            "choices": [{"message": {"content": "Observed text"}, "finish_reason": "stop"}]}).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        client = KloudeksClient("https://mia.csp.kloudeks.com/v1", "private-test-key", "http://egress:3128",
                               connection_factory=Mock(return_value=connection))
        return client, connection

    def test_exact_ocr_payload_and_image_caps(self):
        for count, window in ((1, 128), (3, 1024)):
            client, connection = self.connection()
            result = client.interpret([b"fixture-image"] * count, model="kkbhackathon2026/Unlimited-OCR", max_tokens=512, ocr=True)
            self.assertEqual(result["text"], "Observed text")
            connection.set_tunnel.assert_called_once_with("mia.csp.kloudeks.com", 443)
            args, kwargs = connection.request.call_args
            self.assertEqual(args, ("POST", "/v1/chat/completions"))
            payload = json.loads(kwargs["body"])
            self.assertFalse(payload["skip_special_tokens"])
            self.assertEqual(payload["vllm_xargs"], {"ngram_size": 35, "window_size": window})
            self.assertEqual(payload["messages"][0]["content"][-1]["text"], "<image>\ndocument parsing")
            self.assertEqual(payload["max_tokens"], 512)
            self.assertTrue(payload["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,"))
        with self.assertRaises(ModelFailure):
            client.interpret([b"x"] * 4, model="kkbhackathon2026/Unlimited-OCR", max_tokens=512, ocr=True)

    def test_vision_truncation_and_error_redaction_without_retries(self):
        client, connection = self.connection({"choices": [{"message": {"content": "Chart text"}, "finish_reason": "length"}]})
        result = client.interpret([b"image"], model="kkbhackathon2026/Qwen3.8-27B", max_tokens=128, question="Describe the trend")
        self.assertTrue(result["truncated"])
        payload = json.loads(connection.request.call_args.kwargs["body"])
        self.assertNotIn("vllm_xargs", payload)
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        for status in (302, 401, 403, 429, 500):
            client, connection = self.connection(status=status)
            with self.subTest(status=status), self.assertRaises(ModelFailure) as error:
                client.interpret([b"image"], model="kkbhackathon2026/Qwen3.8-27B", max_tokens=128)
            self.assertNotIn("private-test-key", str(error.exception))
            self.assertEqual(connection.request.call_count, 1)

    def test_empty_reasoning_only_response_and_http_diagnostics(self):
        client, connection = self.connection({"choices": [{"message": {"content": "", "reasoning_content": "private reasoning"}, "finish_reason": "length"}]})
        with self.assertRaises(ModelFailure) as error:
            client.chat([{"role": "user", "content": "hello"}], model="kkbhackathon2026/Qwen3.8-27B", max_tokens=128)
        self.assertEqual(error.exception.code, "model_output_limit")
        self.assertNotIn("private reasoning", str(error.exception))
        client, connection = self.connection(status=401)
        with self.assertRaises(ModelFailure) as error:
            client.chat([], model="kkbhackathon2026/Qwen3.8-27B", max_tokens=128)
        self.assertEqual(error.exception.http_status, 401)
        self.assertEqual(error.exception.code, "model_access_denied")
        connection.getresponse.return_value.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
