"""Adapters for the injectable tools in the team's five-stage Agent pipeline.

Keep backend.agent imports in the caller: perhat does not require or merge that
branch's lakehouse, LLM client, dependencies or application code.
"""


class WebEvidenceError(RuntimeError):
    """Sanitized failure the team's executor can record as a failed step."""


def get_team_tools(environ=None, *, vision=False):
    from backend.tools import get_tools
    tools = get_tools(environ)
    if not tools:
        raise WebEvidenceError("Web tools are disabled; set WEB_TOOLS_ENABLED=true.")

    def checked(result):
        if result.get("status") == "error":
            error = result.get("error") or {}
            raise WebEvidenceError(str(error.get("code", "upstream_error")))
        return result

    def url_reader(url):
        result = checked(tools["read_web_url"](url, max_chars=6000, max_pages=3, vision=vision))
        if not result.get("content"):
            raise WebEvidenceError("No readable content; check OCR/vision settings for scans and images.")
        format_name = result.get("format", "html")
        return {"text": result["content"], "url": result.get("final_url"), "requested_url": url,
                "kind": "excel" if format_name in {"xls", "xlsx"} else format_name,
                "status": result["status"], "truncated": result.get("truncated", False),
                "warnings": result.get("warnings", []), "fetched_at": result.get("fetched_at"),
                "processing_errors": result.get("processing_errors", []),
                "locations": [s.get("location") for s in result.get("sections", [])],
                "source_trust": "untrusted_external", "ready_for_calculation": False}

    def web_search(query):
        return checked(tools["search_web"](query, max_results=3))

    return {"url_reader": url_reader, "web_search": web_search}
