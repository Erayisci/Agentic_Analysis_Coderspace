"""Question-level provenance, budgets, coverage and retained-evidence scenarios."""

from copy import deepcopy
import hashlib
import json
import time
import unittest
from unittest.mock import Mock, patch

from backend.extensions.web_tools.agent_protocol import tool_schemas, validate_action
from backend.extensions.web_tools.asset_common import AssetFailure, normalize
from backend.extensions.web_tools.asset_config import AssetConfig
from backend.extensions.web_tools.client import ToolFailure, _request_json, research_deadline, remaining_timeout
from backend.extensions.web_tools.research import _fit_context, research
from backend.extensions.web_tools.research_evidence import EvidenceLedger, canonical_url


ENV = {"WEB_TOOLS_ENABLED": "true", "WEB_AGENT_ENABLED": "true", "WEB_DOCUMENTS_ENABLED": "true"}


def doc(url="https://example.com/a", text="Revenue: 100 TRY, March 2026.", *, location="Page 1", **extra):
    return {"status": "ok", "final_url": url, "fetched_at": "2026-09-17T10:00:00Z", "format": "pdf",
            "content": text, "sections": [{"location": location, "method": "pdf_text", "text": text}],
            "truncated": False, "downloaded_bytes": 2048, "error": None, **extra}


def answer(citations=("S1",), *, coverage=None, conflicts=None):
    if coverage is None:
        coverage = [{"requirement_id": "R1", "status": "supported", "citations": list(citations), "note": "Evidence read."}]
    return {"status": "ok", "decision": {"action": "answer", "answer": "Evidence summary. " + " ".join(f"[{s}]" for s in citations),
            "citations": list(citations), "coverage": coverage, "conflicts": conflicts or []}}


def call(name, **args):
    return {"status": "ok", "decision": {"action": "tool", "name": name, "arguments": args}}


class LedgerTests(unittest.TestCase):
    def test_redirect_aliases_pages_and_mirrored_files_are_one_document(self):
        ledger = EvidenceLedger(100000)
        fingerprint = hashlib.sha256(b"same full PDF, different page ranges").hexdigest()
        for url, location in (("https://example.com/report", "Page 1"),
                              ("https://example.com/report#page=2", "Page 2"),
                              ("https://mirror.example.com/copy.pdf", "Page 3")):
            evidence = doc(url, location, location=location, content_sha256=fingerprint, fingerprint_kind="file_bytes")
            index = ledger.retain("read_web_url", {"url": url}, evidence)
            ledger.add_read(url, evidence, index)
        self.assertEqual(ledger.document_count(), 1)
        self.assertEqual(len(ledger.sources), 3)
        self.assertEqual(len(ledger.documents[0]["urls"]), 2)
        alias = "https://example.com/download?id=12"
        output = doc("https://example.com/report", "Page 1", content_sha256=fingerprint, fingerprint_kind="file_bytes")
        index = ledger.retain("read_web_url", {"url": alias}, output)
        ledger.add_read(alias, output, index)
        self.assertEqual(ledger.identity(alias), ledger.identity("https://example.com/report"))

    def test_truncated_shared_introductions_are_not_deduplicated(self):
        ledger = EvidenceLedger(100000)
        for name in ("a", "b"):
            output = doc(f"https://example.com/{name}", "Annual report introduction.", truncated=True)
            index = ledger.retain("read_web_url", {}, output)
            ledger.add_read(output["final_url"], output, index)
        self.assertEqual(ledger.document_count(), 2)
        self.assertEqual(canonical_url("https://EXAMPLE.com:443/report?a=1#part"), "https://example.com/report?a=1")
        self.assertNotEqual(canonical_url("https://example.com/report?a=1"), canonical_url("https://example.com/report?a=2"))

    def test_context_trimming_keeps_full_evidence_and_inspection_gets_tail(self):
        ledger = EvidenceLedger(100000)
        text = "A" * 7000 + "Tail value: 987 TRY."
        output = doc(text=text)
        index = ledger.retain("read_web_url", {}, output)
        ledger.add_read(output["final_url"], output, index)
        original = deepcopy(ledger.records)
        context = _fit_context(ledger.context([{"id": "R1", "text": "Find value"}], [], 1, {}), 2000)
        self.assertLessEqual(len(json.dumps(context)), 2000)
        self.assertEqual(ledger.records, original)
        self.assertEqual(ledger.records[0]["output"]["content"], text)
        source = ledger.inspect("S1", offset=7000)["source"]
        self.assertIn("987 TRY", source["excerpt"])
        self.assertEqual(source["document_id"], "D1")
        self.assertEqual(ledger.document_count(), 1)


class MultiSourceTests(unittest.TestCase):
    def test_two_sources_with_explicit_coverage(self):
        outputs = [doc(), doc("https://official.example.net/b", "Methodology describes revenue recognition.")]
        coverage = [{"requirement_id": "R1", "status": "supported", "citations": ["S1"], "note": "Period and amount."},
                    {"requirement_id": "R2", "status": "supported", "citations": ["S2"], "note": "Definition."}]
        result = research("Explain revenue and methodology", environ=ENV, urls=[o["final_url"] for o in outputs],
                          requirements=["Revenue", "Methodology"], min_sources=2,
                          tools={"read_web_url": Mock(side_effect=outputs)}, decide=Mock(return_value=answer(("S1", "S2"), coverage=coverage)))
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["stop_reason"], "sufficient_evidence")
        self.assertEqual(result["usage"]["distinct_documents"], 2)
        self.assertEqual(result["usage"]["download_bytes_charged"], 4096)
        self.assertEqual(len(result["evidence"]), 2)
        self.assertEqual(result["missing_information"], [])

    def test_identical_copies_cannot_satisfy_minimum_and_prompt_requests_more(self):
        outputs = [doc(), doc("https://copy.example.com/report")]
        planner = Mock(return_value=answer(("S1",)))
        result = research("Corroborate revenue", environ=ENV, urls=[o["final_url"] for o in outputs], min_sources=2,
                          tools={"read_web_url": Mock(side_effect=outputs)}, decide=planner)
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual(result["usage"]["distinct_documents"], 1)
        self.assertEqual(result["stop_reason"], "incomplete_evidence")
        self.assertIn("Required 2", result["missing_information"][0])
        self.assertTrue(any(h.get("incomplete_answer") for h in planner.call_args.args[0]["context"]["history"]))

    def test_conflict_report_retains_both_values_and_is_partial(self):
        outputs = [doc(), doc("https://example.net/revision", "Revenue: 110 TRY, March 2026.")]
        coverage = [{"requirement_id": "R1", "status": "conflicting", "citations": ["S1", "S2"], "note": "Values disagree."}]
        conflicts = [{"requirement_id": "R1", "citations": ["S1", "S2"], "description": "Same stated period and unit; 100 versus 110."}]
        result = research("What is revenue?", environ=ENV, urls=[o["final_url"] for o in outputs],
                          tools={"read_web_url": Mock(side_effect=outputs)}, decide=Mock(return_value=answer(("S1", "S2"), coverage=coverage, conflicts=conflicts)))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"][0]["status"], "conflicting")
        self.assertEqual(result["conflicts"], conflicts)
        self.assertIn("110", result["evidence"][1]["output"]["content"])

    def test_fabricated_coverage_or_conflict_references_are_rejected(self):
        cases = [answer(coverage=[{"requirement_id": "R99", "status": "supported", "citations": ["S1"], "note": "Invented requirement"}]),
                 answer(coverage=[{"requirement_id": "R1", "status": "supported", "citations": ["S99"], "note": "Unread source"}]),
                 answer(conflicts=[{"requirement_id": "R1", "citations": ["S1", "S1"], "description": "Not two pieces of evidence"}])]
        for response in cases:
            result = research("q", environ=ENV, url=doc()["final_url"], tools={"read_web_url": Mock(return_value=doc())}, decide=Mock(return_value=response))
            self.assertEqual(result["error"]["code"], "invalid_citation")
            self.assertEqual(result["answer"], "")

    def test_missing_assessments_are_not_complete(self):
        result = research("q", environ=ENV, url=doc()["final_url"], requirements=["revenue", "period"],
                          tools={"read_web_url": Mock(return_value=doc())}, decide=Mock(return_value=answer()))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"][1]["status"], "not_assessed")
        self.assertIn("period", result["missing_information"])

    def test_missing_document_causes_another_read_before_final_answer(self):
        reader = Mock(side_effect=[doc(), doc("https://example.net/b", "Definition of reported revenue.")])
        planner = Mock(side_effect=[answer(), call("read_web_url", url="https://example.net/b"), answer(("S1", "S2"))])
        result = research("Compare reports", environ=ENV, url=doc()["final_url"], min_sources=2,
                          tools={"read_web_url": reader}, decide=planner)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(result["usage"]["model_calls"], 3)

    def test_current_worker_limits_restrict_host_and_old_worker_requires_restart(self):
        from backend.extensions.web_tools.research import LIMIT_NAMES
        config = AssetConfig()
        health = {"capabilities": {"agent": True}, "agent_limits": {name: getattr(config, name) for name in LIMIT_NAMES}}
        health["agent_limits"].update(model_max_calls_per_read=1, asset_max_bytes=2048,
                                      agent_max_sources=1, agent_max_download_bytes=4096)
        reader = Mock(return_value=doc())
        with patch("backend.extensions.web_tools.research._request_json", side_effect=[health, answer()]):
            result = research("q", environ=ENV, url=doc()["final_url"], tools={"read_web_url": reader})
        self.assertEqual(reader.call_args.kwargs["max_bytes"], 2048)
        self.assertEqual(result["limits"]["agent_max_sources"], 1)
        self.assertEqual(result["limits"]["agent_max_download_bytes"], 4096)
        with patch("backend.extensions.web_tools.research._request_json", return_value={"capabilities": {"agent": True}, "agent_limits": {}}):
            result = research("q", environ=ENV, tools={"read_web_url": reader})
        self.assertEqual(result["error"]["code"], "upstream_error")

    def test_retained_but_never_shown_excerpt_cannot_be_cited(self):
        output = doc(text="a" * 4000)
        output["sections"] = [{"location": f"Page {i}", "method": "pdf_text", "text": str(i) + "a" * 500} for i in range(10)]
        planner = Mock(return_value=answer(("S1",)))
        result = research("q", environ=ENV | {"WEB_AGENT_MAX_CONTEXT_CHARS": "2000"}, url=doc()["final_url"],
                          tools={"read_web_url": Mock(return_value=output)}, decide=planner)
        shown = {s["id"] for s in planner.call_args.args[0]["context"]["sources"]}
        self.assertNotIn("S1", shown)
        self.assertEqual(len(result["sources"]), 10)
        self.assertEqual(result["error"]["code"], "invalid_citation")

    def test_inspection_uses_retained_output_without_another_download(self):
        reader = Mock(return_value=doc(text="A" * 7000 + "987 TRY"))
        planner = Mock(side_effect=[call("inspect_evidence", source_id="S1", offset=7000), answer(("S2",))])
        result = research("Read tail", environ=ENV, url=doc()["final_url"], tools={"read_web_url": reader}, decide=planner)
        reader.assert_called_once()
        self.assertEqual(result["usage"]["download_bytes_charged"], 2048)
        self.assertEqual(result["sources"][1]["excerpt"], "987 TRY")
        self.assertEqual(result["citations"], ["S2"])

    def test_source_limit_stops_before_new_download_but_allows_final_answer(self):
        reader = Mock(return_value=doc())
        planner = Mock(side_effect=[call("read_web_url", url="https://other.example.com/b"), answer()])
        result = research("q", environ=ENV | {"WEB_AGENT_MAX_SOURCES": "1"}, url=doc()["final_url"],
                          tools={"read_web_url": reader}, decide=planner)
        reader.assert_called_once()
        self.assertEqual(result["stop_reason"], "source_limit")
        self.assertTrue(planner.call_args.args[0]["force_answer"])
        self.assertTrue(result["answer"])

    def test_failed_read_is_charged_conservatively_and_preserves_good_source(self):
        reader = Mock(side_effect=[doc(), {"status": "error", "error": {"code": "timeout"}}])
        planner = Mock(side_effect=[call("read_web_url", url="https://example.net/b"),
                                    call("read_web_url", url="https://example.net/c"), answer()])
        result = research("q", environ=ENV | {"WEB_AGENT_MAX_DOWNLOAD_BYTES": "4096"}, url=doc()["final_url"],
                          tools={"read_web_url": reader}, decide=planner)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(reader.call_args.kwargs["max_bytes"], 2048)
        self.assertEqual(result["usage"]["download_bytes_charged"], 4096)
        self.assertEqual(result["stop_reason"], "download_limit")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["evidence"]), 2)

    def test_evidence_byte_limit_keeps_previous_results(self):
        reader = Mock(side_effect=[doc(), doc("https://example.net/b", "A" * 10000)])
        planner = Mock(side_effect=[call("read_web_url", url="https://example.net/b"), answer()])
        result = research("q", environ=ENV | {"WEB_AGENT_MAX_EVIDENCE_BYTES": "4096"}, url=doc()["final_url"],
                          tools={"read_web_url": reader}, decide=planner)
        self.assertEqual(result["stop_reason"], "evidence_limit")
        self.assertEqual(len(result["evidence"]), 1)
        self.assertLessEqual(result["usage"]["evidence_bytes"], 4096)

    def test_deadline_preserves_completed_read_and_makes_no_model_call(self):
        now = [0]
        def slow(**kwargs):
            now[0] = 6
            return doc()
        planner = Mock()
        with patch("backend.extensions.web_tools.research.time.monotonic", side_effect=lambda: now[0]):
            result = research("q", environ=ENV | {"WEB_AGENT_TIMEOUT_SECONDS": "5"}, url=doc()["final_url"],
                              tools={"read_web_url": slow}, decide=planner)
        self.assertEqual(result["stop_reason"], "research_timeout")
        self.assertEqual(len(result["evidence"]), 1)
        self.assertEqual(result["status"], "partial")
        planner.assert_not_called()

    def test_network_deadline_is_scoped_and_expired_request_never_connects(self):
        with research_deadline(time.monotonic() - 1), patch("http.client.HTTPConnection") as connection:
            with self.assertRaises(ToolFailure) as error:
                _request_json("http://localhost/health", 10)
            connection.assert_not_called()
            self.assertEqual(error.exception.error["code"], "research_timeout")
        self.assertEqual(remaining_timeout(10), 10)

    def test_download_limit_clamped_by_worker_and_invalid_inputs_fail(self):
        config = AssetConfig(documents_enabled=True, asset_max_bytes=2048)
        payload = {"url": "https://example.com/a.pdf", "kind": "auto", "max_bytes": 4096}
        self.assertEqual(normalize(payload, config)["max_bytes"], 2048)
        for bad in (True, -1, 10, "4096"):
            with self.assertRaises(AssetFailure):
                normalize(payload | {"max_bytes": bad}, config)
        for kwargs in ({"min_sources": True}, {"urls": "https://example.com"}, {"requirements": [""]}):
            reader = Mock()
            result = research("q", environ=ENV, tools={"read_web_url": reader}, decide=Mock(), **kwargs)
            self.assertEqual(result["error"]["code"], "invalid_request")
            reader.assert_not_called()

    def test_repeated_defaulted_url_and_fragment_does_not_download_again(self):
        reader = Mock(return_value=doc())
        planner = Mock(side_effect=[call("read_web_url", url=doc()["final_url"] + "#page=1", max_chars=6000), answer()])
        result = research("q", environ=ENV, url=doc()["final_url"], tools={"read_web_url": reader}, decide=planner)
        reader.assert_called_once()
        self.assertEqual(result["stop_reason"], "repeated_action")

    def test_unknown_assessment_fields_and_missing_action_keys_are_rejected(self):
        schemas = tool_schemas(AssetConfig())
        for action in ({"action": "answer"}, answer()["decision"] | {"confidence": 1},
                       answer(coverage=[{"requirement_id": "R1", "status": [], "citations": [], "note": "x"}])["decision"]):
            with self.assertRaises(AssetFailure):
                validate_action(action, schemas)


if __name__ == "__main__":
    unittest.main()
