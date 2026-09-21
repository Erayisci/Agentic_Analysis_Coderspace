"""Optional multi-source research with retained evidence and bounded model use."""

from copy import deepcopy
import json
import time
from dataclasses import replace

from .agent_protocol import tool_schemas, validate_action
from .asset_common import AssetFailure, failure
from .asset_config import AssetConfig
from .client import ToolFailure, _request_json, research_deadline
from .config import WebToolsConfig
from .research_evidence import EvidenceLedger, assess_answer, canonical_url


LIMIT_NAMES = ("agent_max_tool_calls", "agent_max_model_calls", "agent_max_context_chars",
               "agent_max_sources", "agent_max_download_bytes", "agent_max_evidence_bytes", "agent_timeout_seconds")
BUDGET_ERRORS = {"source_limit", "download_limit", "evidence_limit", "repeated_action"}


def _fit_context(context, maximum):
    """Trim only a snapshot; the evidence ledger is never modified."""
    while len(json.dumps(context)) > maximum:
        candidates = [source for source in context["sources"] if len(source["excerpt"]) > 120]
        if candidates:
            largest = max(candidates, key=lambda source: len(source["excerpt"]))
            largest["excerpt"] = largest["excerpt"][:max(120, len(largest["excerpt"]) // 2)]
            largest["excerpt_truncated"] = True
        elif context["history"]:
            context["history"].pop(0)
        elif context["sources"]:
            context["sources"].pop(0)
        elif context["documents"]:
            context["documents"].pop(0)
        else:
            raise AssetFailure("invalid_request")
    return context


def research(question, *, environ=None, url=None, urls=None, requirements=None, min_sources=1,
             allow_vision=False, max_tool_calls=None, tools=None, decide=None, on_tool_result=None):
    """Run one question. Save returned JSON to keep all accepted extraction output.

    `tools` and `decide` are trusted synchronous test/embedding hooks. Custom tools
    must honor max_bytes; hook execution itself cannot be forcibly cancelled.
    Only a model decision can trigger subsequent tools; no recursive bulk crawl.
    on_tool_result commits complete outputs before context/ledger limits apply.
    Callback failures propagate so a caller cannot report unsaved evidence as saved.
    """
    from backend.tools import get_tools
    config = AssetConfig.from_environ(environ)
    started = time.monotonic()
    result = {"status": "error", "answer": "", "sources": [], "documents": [], "evidence": [],
              "citations": [], "coverage": [], "conflicts": [], "missing_information": [],
              "trace": [], "warnings": [], "stop_reason": None,
              "usage": {"tool_calls": 0, "model_calls": 0, "source_attempts": 0,
                        "distinct_documents": 0, "download_bytes_charged": 0, "evidence_bytes": 0}, "error": None}
    usage, trace = result["usage"], result["trace"]
    ledger = EvidenceLedger(config.agent_max_evidence_bytes)
    required = []
    try:
        if not config.agent_enabled:
            raise AssetFailure("feature_disabled")
        if (not isinstance(question, str) or not question.strip() or len(question) > 2000
                or type(allow_vision) is not bool or type(min_sources) is not int
                or not 1 <= min_sources <= config.agent_max_sources):
            raise AssetFailure("invalid_request")
        if allow_vision and not config.vision_enabled:
            raise AssetFailure("feature_disabled")
        if requirements is None:
            requirements = [question]
        if (not isinstance(requirements, (list, tuple)) or not 1 <= len(requirements) <= 8
                or any(not isinstance(item, str) or not item.strip() for item in requirements)
                or sum(len(item) for item in requirements) > 2000):
            raise AssetFailure("invalid_request")
        required = [{"id": f"R{index + 1}", "text": item.strip()} for index, item in enumerate(requirements)]
        if urls is None:
            urls = []
        if not isinstance(urls, (list, tuple)):
            raise AssetFailure("invalid_request")
        seeds = list(urls) + ([url] if url is not None else [])
        seeds = list(dict.fromkeys(canonical_url(seed) for seed in seeds))
        if len(seeds) > config.agent_max_sources:
            raise AssetFailure("invalid_request")
        cap = config.agent_max_tool_calls if max_tool_calls is None else max_tool_calls
        if type(cap) is not int or cap < 1:
            raise AssetFailure("invalid_request")
        cap = min(cap, config.agent_max_tool_calls)
        mapping = get_tools(environ) if tools is None else tools
        if not mapping:
            raise AssetFailure("feature_disabled")
        service = WebToolsConfig.from_environ(environ).crawler_url
        with research_deadline(started + config.agent_timeout_seconds):
            if decide is None:
                health = _request_json(service + "/health", 8)
                if not health.get("capabilities", {}).get("agent"):
                    raise AssetFailure("feature_disabled")
                limits = health.get("agent_limits", {})
                if any(type(limits.get(name)) is not int or limits[name] < 0 for name in (
                        *LIMIT_NAMES, "model_max_calls_per_read", "asset_max_bytes")):
                    raise AssetFailure("upstream_error")
                config = replace(config, **{name: min(getattr(config, name), limits[name]) for name in LIMIT_NAMES},
                                 model_max_calls_per_read=limits["model_max_calls_per_read"],
                                 asset_max_bytes=min(config.asset_max_bytes, limits["asset_max_bytes"]))
        if min_sources > config.agent_max_sources or len(seeds) > config.agent_max_sources:
            raise AssetFailure("invalid_request")
        ledger.maximum = config.agent_max_evidence_bytes
        cap = min(cap, config.agent_max_tool_calls)
        deadline = started + config.agent_timeout_seconds
        result["limits"] = {name: getattr(config, name) for name in LIMIT_NAMES}
        result["limits"].update(agent_max_tool_calls=cap, minimum_documents=min_sources)
        schemas = tool_schemas(config, allow_vision=allow_vision, include_research=True)
        history, seen, attempted_urls, shown = [], set(), set(), set()
        stop_reason = None
        incomplete_answers = 0

        def check_deadline():
            if time.monotonic() >= deadline:
                raise AssetFailure("research_timeout")

        def invoke(name, arguments):
            check_deadline()
            validate_action({"action": "tool", "name": name, "arguments": arguments}, schemas)
            if usage["tool_calls"] >= cap:
                raise AssetFailure("model_limit")
            arguments = dict(arguments)
            reserved_models, reserved_bytes = 0, 0
            if name == "read_web_url":
                arguments.setdefault("max_chars", min(6000, config.asset_max_chars))
                arguments.setdefault("max_pages", min(3, config.asset_max_pages))
                arguments.setdefault("start_page", 1)
                arguments.setdefault("ocr", config.ocr_enabled)
                arguments.setdefault("vision", False)
                if arguments["vision"] or (arguments["ocr"] and config.ocr_provider == "kloudeks"):
                    reserved_models = config.model_max_calls_per_read
                    if usage["model_calls"] + reserved_models >= config.agent_max_model_calls:
                        raise AssetFailure("model_limit")
                identity = ledger.identity(arguments["url"])
                if identity not in {ledger.identity(value) for value in attempted_urls}:
                    if usage["source_attempts"] >= config.agent_max_sources:
                        raise AssetFailure("source_limit")
                reserved_bytes = min(config.asset_max_bytes, config.agent_max_download_bytes - usage["download_bytes_charged"])
                if reserved_bytes < 1024:
                    raise AssetFailure("download_limit")
            elif name == "search_web":
                arguments.setdefault("max_results", 3)
            elif name == "inspect_evidence":
                arguments.setdefault("offset", 0)
                arguments.setdefault("max_chars", 2000)
            signature_args = dict(arguments)
            if "url" in signature_args:
                signature_args["url"] = ledger.identity(signature_args["url"])
            signature = json.dumps([name, signature_args], sort_keys=True)
            if signature in seen:
                raise AssetFailure("repeated_action")
            seen.add(signature)
            if name == "read_web_url":
                if ledger.identity(arguments["url"]) not in {ledger.identity(value) for value in attempted_urls}:
                    usage["source_attempts"] += 1
                attempted_urls.add(canonical_url(arguments["url"]))
                arguments["max_bytes"] = reserved_bytes
            usage["tool_calls"] += 1
            usage["model_calls"] += reserved_models
            usage["download_bytes_charged"] += reserved_bytes
            with research_deadline(deadline):
                if name == "inspect_evidence":
                    evidence = ledger.inspect(**arguments)
                else:
                    evidence = mapping[name](**arguments)
            if on_tool_result is not None:
                on_tool_result(name, arguments, evidence)
            if evidence.get("status") != "error":
                actual = evidence.get("model_calls", 0)
                usage["model_calls"] -= max(0, reserved_models - actual)
                if reserved_bytes:
                    size = evidence.get("downloaded_bytes")
                    if evidence.get("format") == "html" or evidence.get("cache", {}).get("hit"):
                        size = 0
                    if type(size) is int and 0 <= size <= reserved_bytes:
                        usage["download_bytes_charged"] -= reserved_bytes - size
            trace.append({"tool": name, "arguments": arguments, "status": evidence.get("status"),
                          "error": evidence.get("error"), "processing_errors": evidence.get("processing_errors", [])})
            index = ledger.retain(name, arguments, evidence)
            entry = {"tool": name, "arguments": arguments, "status": evidence.get("status"),
                     "error": evidence.get("error"), "warnings": evidence.get("warnings", [])[:4]}
            if name == "search_web":
                entry["results"] = [{"title": item.get("title", "")[:180], "url": item.get("url"),
                                     "snippet": item.get("snippet", "")[:500], "published_at": item.get("published_at")}
                                    for item in evidence.get("results", [])[:5]]
            elif name == "get_page_assets":
                for key in ("links", "images"):
                    entry[key] = [{"url": item.get("url"), "type_hint": item.get("type_hint"),
                                   "text": item.get("text", "")[:160]} for item in (evidence.get(key) or [])[:8]]
            elif name == "read_web_url":
                added = ledger.add_read(arguments["url"], evidence, index)
                entry["source_ids"] = [source["id"] for source in added]
                # Alias resolution may change a URL signature after this first read.
                signature_args["url"] = ledger.identity(arguments["url"])
                seen.add(json.dumps([name, signature_args], sort_keys=True))
            elif name == "inspect_evidence":
                entry["source_id"] = evidence["source"]["id"]
            history.append(entry)
            usage.update(distinct_documents=ledger.document_count(), evidence_bytes=ledger.bytes)
            check_deadline()

        def limited_invoke(name, arguments):
            nonlocal stop_reason
            try:
                invoke(name, arguments)
            except AssetFailure as error:
                if error.code not in BUDGET_ERRORS:
                    raise
                stop_reason = error.code
                history.append({"stopped": error.code, "instruction": "Return a partial answer from retained evidence, identifying gaps."})
                result["warnings"].append(failure(error.code)["error"]["message"])

        for seed in seeds:
            if usage["tool_calls"] >= cap:
                stop_reason = "tool_limit"
                break
            args = {"url": seed}
            if allow_vision:
                args.update(vision=True, question=question)
            limited_invoke("read_web_url", args)
            if stop_reason:
                break
        while usage["model_calls"] < config.agent_max_model_calls:
            check_deadline()
            if usage["tool_calls"] >= cap:
                stop_reason = stop_reason or "tool_limit"
            if usage["model_calls"] + 1 >= config.agent_max_model_calls:
                stop_reason = stop_reason or "model_limit"
            force_answer = stop_reason is not None
            context = _fit_context(ledger.context(required, history, min_sources, usage), config.agent_max_context_chars)
            shown.update(source["id"] for source in context["sources"])
            payload = {"question": question, "context": context, "allow_vision": allow_vision, "force_answer": force_answer,
                       "tool_policy": {name: getattr(config, name) for name in (
                           "documents_enabled", "images_enabled", "links_enabled", "ocr_enabled", "asset_max_chars", "asset_max_pages")}}
            usage["model_calls"] += 1
            with research_deadline(deadline):
                response = decide(deepcopy(payload)) if decide else _request_json(service + "/agent-model", config.asset_timeout_seconds + 5, payload)
            check_deadline()
            if response.get("status") == "error":
                detail = response.get("error") or {}
                error = AssetFailure(detail.get("code"))
                error.http_status = detail.get("http_status")
                raise error
            action = validate_action(response.get("decision"), schemas)
            if action["action"] == "tool":
                if force_answer:
                    raise AssetFailure("model_limit")
                limited_invoke(action["name"], action["arguments"])
                continue
            if not ledger.sources:
                raise AssetFailure("insufficient_evidence")
            coverage, conflicts, gaps = assess_answer(action, required, shown, ledger, min_sources)
            if gaps and not force_answer and incomplete_answers == 0:
                incomplete_answers += 1
                history.append({"incomplete_answer": True, "missing_information": gaps,
                                "instruction": "Collect missing evidence within limits, or return an explicitly partial answer with coverage and conflicts."})
                continue
            result.update(answer=action["answer"], citations=action["citations"], coverage=coverage,
                          conflicts=conflicts, missing_information=gaps, status="ok",
                          stop_reason=stop_reason or ("incomplete_evidence" if gaps else "sufficient_evidence"))
            if (gaps or stop_reason in BUDGET_ERRORS or any(item["status"] != "ok" for item in trace)
                    or any(s["truncated"] or s["excerpt_truncated"] for s in ledger.sources if s["id"] in action["citations"])):
                result["status"] = "partial"
            result["warnings"].append("Coverage and conflicts are model assessments, not verified facts. Distinct documents do not prove independent corroboration. Tables require validation before calculations.")
            return result
        raise AssetFailure("model_limit")
    except (AssetFailure, ToolFailure) as error:
        result["error"] = error.error if isinstance(error, ToolFailure) else failure(error.code)["error"]
        if time.monotonic() >= started + config.agent_timeout_seconds:
            result["error"] = failure("research_timeout")["error"]
        if type(getattr(error, "http_status", None)) is int and 100 <= error.http_status <= 599:
            result["error"]["http_status"] = error.http_status
        result["stop_reason"] = result["error"]["code"]
        result["status"] = "partial" if ledger.sources else "error"
        result["warnings"].append("Research stopped before a cited answer was completed; retained evidence is preserved.")
        return result
    finally:
        result.update(sources=ledger.sources, documents=ledger.documents, evidence=ledger.records)
        usage.update(distinct_documents=ledger.document_count(), evidence_bytes=ledger.bytes,
                     elapsed_seconds=round(time.monotonic() - started, 3))
        if not result["coverage"]:
            result["coverage"] = [{"requirement_id": item["id"], "requirement": item["text"], "status": "not_assessed",
                                   "citations": [], "note": "No final assessment was completed."} for item in required]
            result["missing_information"] = [item["text"] for item in required]
