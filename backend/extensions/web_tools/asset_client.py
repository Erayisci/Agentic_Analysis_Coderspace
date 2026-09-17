"""Dependency-free public document/image tools; the service enforces policy too."""

from .asset_common import AssetFailure, failure, normalize
from .client import ToolFailure
from .security import UnsafeURL, validate_url_syntax


class AssetTools:
    def __init__(self, web, config):
        self.web, self.config = web, config

    def read_web_url(self, url, max_chars=None, max_pages=None, start_page=1,
                     ocr=None, vision=False, question="", refresh=False, *, max_bytes=None):
        """Route HTML, documents, text and images by response type and file bytes."""
        payload = {"url": url, "kind": "auto", "start_page": start_page,
                   "vision": vision, "question": question, "refresh": refresh}
        for name, value in (("max_chars", max_chars), ("max_pages", max_pages), ("ocr", ocr), ("max_bytes", max_bytes)):
            if value is not None:
                payload[name] = value
        try:
            request = normalize(payload, self.config, check_enabled=False)
        except AssetFailure as error:
            return failure(error.code)
        # The HTML reader checks the actual response and every redirect. Only a
        # type mismatch dispatches to a file reader; never retry arbitrary errors.
        page = self.web.read_url(request["url"], max_chars=request["max_chars"])
        if page.get("status") != "error":
            return {**page, "kind": "html", "format": "html", "sections": [
                {"location": "Webpage", "method": "html_text", "text": page.get("content", "")}]}
        if (page.get("error") or {}).get("code") != "unsupported_content_type":
            return page
        return self._read(url, "auto", max_chars, max_pages, start_page, ocr, vision, question, refresh, max_bytes=max_bytes)

    def read_document(self, url, max_chars=None, max_pages=None, start_page=1,
                      ocr=None, vision=False, question="", refresh=False):
        """Extract a bounded public PDF, XLSX/XLS, CSV, DOCX or text; cite locations."""
        return self._read(url, "document", max_chars, max_pages, start_page, ocr, vision, question, refresh)

    def read_image(self, url, ocr=None, vision=False, question="", max_chars=None, refresh=False):
        """Read a public PNG/JPEG/WebP/TIFF using allowed OCR/vision; no arbitrary model settings."""
        return self._read(url, "image", max_chars, 1, 1, ocr, vision, question, refresh)

    def _read(self, url, kind, maximum, pages, start, ocr, vision, question, refresh, *, max_bytes=None):
        payload = {"url": url, "kind": kind, "start_page": start, "vision": vision,
                   "question": question, "refresh": refresh}
        for key, value in (("max_chars", maximum), ("max_pages", pages), ("ocr", ocr), ("max_bytes", max_bytes)):
            if value is not None:
                payload[key] = value
        requested = None
        try:
            request = normalize(payload, self.config)
            requested = request["url"]
            # Do not retry POSTs: a lost response could otherwise repeat a model
            # call and incur cost twice. The operator can explicitly retry.
            from .client import _request_json
            if not self.web._slots.acquire(blocking=False):
                return failure("invalid_request", requested) | {"error": {
                    "code": "busy", "message": "The web tools concurrency limit is reached", "retryable": True}}
            try:
                result = _request_json(self.web.config.crawler_url + "/asset",
                                       self.config.asset_timeout_seconds + 5, request)
            finally:
                self.web._slots.release()
            if result.get("status") == "error":
                error = result.get("error", {})
                safe = failure(error.get("code") if isinstance(error, dict) else "parse_error", requested)
                if isinstance(error, dict) and type(error.get("http_status")) is int and 100 <= error["http_status"] <= 599:
                    safe["error"]["http_status"] = error["http_status"]
                return safe
            if (result.get("status") not in {"ok", "empty", "partial"}
                    or not isinstance(result.get("content"), str) or not isinstance(result.get("sections"), list)
                    or len(result["content"]) > request["max_chars"]):
                raise AssetFailure("parse_error")
            validate_url_syntax(result.get("final_url"))
            if result.get("returned_chars") != len(result["content"]):
                raise AssetFailure("parse_error")
            result["requested_url"] = requested
            result["source_trust"] = "untrusted_external"
            return result
        except (AssetFailure, UnsafeURL) as error:
            return failure(error.code if isinstance(error, AssetFailure) else "invalid_url", requested)
        except ToolFailure as error:
            return failure("upstream_error", requested) | {"error": error.error}

    def get_page_assets(self, url):
        """Discover public document/image links without downloading linked files."""
        if not self.config.links_enabled:
            return failure("feature_disabled")
        result = self.web.read_url(url, max_chars=100)
        discovery = {key: result.get(key) for key in (
            "status", "requested_url", "final_url", "title", "fetched_at", "source_trust",
            "links", "images", "links_truncated", "images_truncated", "error", "warnings")}
        if result.get("status") == "ok" and (result.get("links_truncated") or result.get("images_truncated")):
            discovery["status"] = "partial"
        return discovery
