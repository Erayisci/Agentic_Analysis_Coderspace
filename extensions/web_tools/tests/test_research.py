"""Deterministic routing, research budgets, citations and table validation."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.extensions.web_tools.agent_protocol import normalize_model_request, tool_schemas, validate_action
from backend.extensions.web_tools.asset_client import AssetTools
from backend.extensions.web_tools.asset_common import AssetFailure, normalize
from backend.extensions.web_tools.asset_config import AssetConfig
from backend.extensions.web_tools.asset_extract import Collector, Interpreter, extract
from backend.extensions.web_tools.research import research
from backend.extensions.web_tools.team_adapter import WebEvidenceError, get_team_tools
from backend.model_clients.kloudeks import ModelFailure
from backend.tools.table_validation import validate_extracted_table


ENV = {"WEB_TOOLS_ENABLED": "true", "WEB_AGENT_ENABLED": "true", "WEB_DOCUMENTS_ENABLED": "true"}


def document():
    return {"status": "ok", "final_url": "https://example.com/report", "fetched_at": "2026-09-16T10:00:00Z",
            "content": "Revenue was 100 TRY.", "format": "text", "sections": [
                {"location": "Text", "method": "plain_text", "text": "Revenue was 100 TRY."}],
            "truncated": False, "error": None}


def decision(action):
    return {"status": "ok", "decision": action}


class RoutingAndTextTests(unittest.TestCase):
    def test_auto_dispatch_only_follows_type_mismatch(self):
        web = Mock()
        reader = AssetTools(web, AssetConfig(documents_enabled=True))
        reader._read = Mock(return_value=document())
        for code in ("certificate_error", "invalid_url", "timeout", "upstream_error"):
            web.read_url.return_value = {"status": "error", "error": {"code": code}}
            self.assertEqual(reader.read_web_url("https://example.com/report")["error"]["code"], code)
        reader._read.assert_not_called()
        web.read_url.return_value = {"status": "error", "error": {"code": "unsupported_content_type"}}
        self.assertEqual(reader.read_web_url("https://example.com/report")["status"], "ok")
        self.assertEqual(reader._read.call_args.args[1], "auto")

    def test_html_still_works_when_file_features_are_off(self):
        web = Mock()
        web.read_url.return_value = document()
        reader = AssetTools(web, AssetConfig())
        self.assertEqual(reader.read_web_url("https://example.com")["format"], "html")
        web.read_url.reset_mock()
        for args in ({"url": "http://127.0.0.1/"}, {"url": "https://example.com", "vision": True},
                     {"url": "https://example.com", "max_pages": 0}):
            self.assertEqual(reader.read_web_url(**args)["status"], "error")
        web.read_url.assert_not_called()

    def test_text_encoding_bounds_and_binary_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "text"
            config = AssetConfig(documents_enabled=True)
            request = normalize({"url": "https://example.com/report.txt", "kind": "auto", "max_chars": 100}, config)
            for encoding in ("utf-8", "utf-16", "utf-32", "iso-8859-9"):
                path.write_bytes(("Türkçe içerik 12345\n" * 20).encode(encoding))
                result = extract(path, request, {"final_url": request["url"], "content_type": "text/plain", "charset": encoding}, config, "unused")
                self.assertIn("Türkçe içerik 12345", result["content"])
                self.assertEqual(result["format"], "text")
                self.assertTrue(result["truncated"])
                self.assertLessEqual(len(result["content"]), 100)
            path.write_bytes(b"binary\x00data")
            with self.assertRaises(AssetFailure):
                extract(path, request, {"final_url": request["url"], "content_type": "text/plain"}, config, "unused")
            path.write_bytes(b"\x89PNG\r\ninvalid")
            with self.assertRaises(AssetFailure) as error:
                extract(path, request, {"final_url": request["url"], "content_type": "image/png"}, config, "unused")
            self.assertEqual(error.exception.code, "feature_disabled")

    def test_model_failure_preserves_evidence_and_stops_further_attempts(self):
        config = AssetConfig(documents_enabled=True, vision_enabled=True, kloudeks_api_key="fixture")
        request = normalize({"url": "https://example.com/report", "kind": "document", "vision": True}, config)
        for native in (True, False):
            collector = Collector(request, {"final_url": request["url"]})
            if native:
                collector.add("Revenue 12345 TRY", "Page 1", "pdf_text")
            interpreter = Interpreter(request, config, "unused", collector)
            with patch("backend.extensions.web_tools.asset_cache.AssetStore") as store, patch(
                    "backend.model_clients.kloudeks.KloudeksClient.interpret",
                    side_effect=ModelFailure("model_access_denied", 401)) as model:
                self.assertFalse(interpreter.model([b"png"], ["Page 1"], ocr=False))
                self.assertFalse(interpreter.model([b"png"], ["Page 2"], ocr=False))
                model.assert_called_once()
                store.return_value.consume_model_call.assert_called_once()
            result = collector.finish()
            self.assertEqual(result["status"], "partial" if native else "error")
            self.assertEqual(result["processing_errors"][0]["http_status"], 401)
            if native:
                self.assertIn("12345", result["content"])
                self.assertIsNone(result["error"])


class ResearchTests(unittest.TestCase):
    def test_file_model_calls_share_question_budget(self):
        env = ENV | {"WEB_VISION_ENABLED": "true", "WEB_AGENT_MAX_MODEL_CALLS": "1"}
        reader = Mock(return_value=document())
        planner = Mock()
        result = research("question", environ=env, url="https://example.com/report", allow_vision=True,
                          tools={"read_web_url": reader}, decide=planner)
        self.assertEqual(result["error"]["code"], "model_limit")
        reader.assert_not_called()
        planner.assert_not_called()

    def test_search_read_answer_and_citations(self):
        tools = {"search_web": Mock(return_value={"status": "ok", "results": [{"url": "https://example.com/report", "title": "Report"}]}),
                 "read_web_url": Mock(return_value=document())}
        actions = [{"action": "tool", "name": "search_web", "arguments": {"query": "report"}},
                   {"action": "tool", "name": "read_web_url", "arguments": {"url": "https://example.com/report"}},
                   {"action": "answer", "answer": "Revenue was 100 TRY. [S1]", "citations": ["S1"],
                    "coverage": [{"requirement_id": "R1", "status": "supported", "citations": ["S1"], "note": "Revenue given in source."}]}]
        planner = Mock(side_effect=[decision(action) for action in actions])
        result = research("What was revenue?", environ=ENV, tools=tools, decide=planner)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["usage"]["tool_calls"], 2)
        self.assertEqual(result["usage"]["model_calls"], 3)
        self.assertEqual(result["sources"][0]["url"], "https://example.com/report")

    def test_unread_citations_and_search_only_answers_are_rejected(self):
        for reference in ("S99", ""):
            planner = Mock(return_value=decision({"action": "answer", "answer": f"Claim [{reference}]",
                                                 "citations": [reference] if reference else []}))
            result = research("question", environ=ENV, url="https://example.com/report",
                              tools={"read_web_url": Mock(return_value=document())}, decide=planner)
            self.assertEqual(result["error"]["code"], "invalid_citation")
            self.assertEqual(result["answer"], "")
            self.assertTrue(result["sources"])
        result = research("question", environ=ENV, tools={"search_web": Mock()}, decide=Mock(return_value=decision(
            {"action": "answer", "answer": "Invented claim", "citations": []})))
        self.assertEqual(result["error"]["code"], "insufficient_evidence")

    def test_limits_unknown_tools_and_model_failure_preserve_sources(self):
        for action, code in [({"action": "tool", "name": "shell", "arguments": {}}, "invalid_request"),
                             ({"action": "tool", "name": "read_web_url", "arguments": {"url": "https://example.com", "vision": True}}, "invalid_request")]:
            tool = Mock(return_value=document())
            result = research("question", environ=ENV, tools={"read_web_url": tool}, decide=Mock(return_value=decision(action)))
            self.assertEqual(result["error"]["code"], code)
            tool.assert_not_called()
        tool = Mock(return_value=document())
        planner = Mock(return_value={"status": "error", "error": {"code": "model_timeout"}})
        result = research("question", environ=ENV, url="https://example.com/report", tools={"read_web_url": tool}, decide=planner)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["sources"])
        self.assertEqual(result["error"]["code"], "model_timeout")
        planner = Mock(return_value=decision({"action": "tool", "name": "read_web_url", "arguments": {"url": "https://example.com/next"}}))
        result = research("question", environ=ENV, url="https://example.com/report", max_tool_calls=1,
                          tools={"read_web_url": tool}, decide=planner)
        self.assertEqual(result["usage"]["tool_calls"], 1)
        self.assertEqual(result["error"]["code"], "model_limit")
        self.assertTrue(planner.call_args.args[0]["force_answer"])

    def test_protocol_flags_and_bounds(self):
        with self.assertRaises(AssetFailure):
            normalize_model_request({"question": "q", "context": {}}, AssetConfig())
        config = AssetConfig(agent_enabled=True)
        for values in ({"context": {"text": "x" * 25000}}, {"allow_vision": True}, {"api_key": "secret"}):
            with self.assertRaises(AssetFailure):
                normalize_model_request({"question": "q", "context": {}} | values, config)
        with self.assertRaises(AssetFailure):
            validate_action({"action": "tool", "name": "search_web", "arguments": {"query": "q", "max_results": True}}, tool_schemas(config))

    def test_caller_policy_restricts_planner_and_allows_explicit_no_ocr(self):
        config = AssetConfig(agent_enabled=True, documents_enabled=True, links_enabled=True, ocr_enabled=True)
        request = normalize_model_request({"question": "q", "context": {}, "tool_policy": {
            "links_enabled": False, "ocr_enabled": False, "asset_max_chars": 500}}, config)
        from dataclasses import replace
        schemas = tool_schemas(replace(config, **request["tool_policy"]))
        self.assertNotIn("get_page_assets", [s["function"]["name"] for s in schemas])
        action = {"action": "tool", "name": "read_web_url", "arguments": {"url": "https://example.com", "ocr": False}}
        self.assertEqual(validate_action(action, schemas), action)
        with self.assertRaises(AssetFailure):
            validate_action(action | {"arguments": {"url": "https://example.com", "ocr": True}}, schemas)
        with self.assertRaises(AssetFailure):
            validate_action(action | {"arguments": {"url": "https://example.com", "max_chars": 800}}, schemas)

    def test_team_adapter_raises_on_failures_and_preserves_text_contract(self):
        tool = Mock(return_value=document())
        with patch("backend.tools.get_tools", return_value={"read_web_url": tool, "search_web": Mock()}):
            adapter = get_team_tools(ENV)
            self.assertEqual(adapter["url_reader"]("https://example.com/report")["text"], document()["content"])
            tool.return_value = {"status": "error", "error": {"code": "model_timeout"}}
            with self.assertRaises(WebEvidenceError):
                adapter["url_reader"]("https://example.com/report")


class TableValidationTests(unittest.TestCase):
    def test_explicit_turkish_number_contract_and_source_gate(self):
        evidence = document()
        evidence["sections"][0]["rows"] = [["Month", "Revenue"], ["March", "1.234,56"]]
        contract = dict(expected_columns=["Month", "Revenue"], numeric_columns=["Revenue"],
                        unit="TRY", period="2026-03", decimal_separator=",", thousands_separator=".")
        self.assertFalse(validate_extracted_table(evidence, 0, **contract)["ready_for_calculation"])
        result = validate_extracted_table(evidence, 0, source_verified=True, **contract)
        self.assertEqual(result["rows"][0]["Revenue"], "1234.56")
        self.assertTrue(result["ready_for_calculation"])
        for changed in ({"status": "partial"}, {"truncated": True}, {"fetched_at": None}):
            self.assertFalse(validate_extracted_table(evidence | changed, 0, source_verified=True, **contract)["ready_for_calculation"])
        for rows in ([["Month", "Revenue"], ["March"]], [["Month", "Revenue"], ["March", "12.34,56"]],
                     [["Wrong", "Revenue"], ["March", "123,45"]], [["Month", "Revenue"], ["March", "NaN"]]):
            changed = deepcopy(evidence)
            changed["sections"][0]["rows"] = rows
            self.assertFalse(validate_extracted_table(changed, 0, source_verified=True, **contract)["ready_for_calculation"])


if __name__ == "__main__":
    unittest.main()
