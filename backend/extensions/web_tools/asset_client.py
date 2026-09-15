"""Dependency-free public document/image tools; the service enforces policy too."""

from .asset_common import AssetFailure, ERRORS, failure, normalize
from .client import ToolFailure
from .security import UnsafeURL, validate_url_syntax


class AssetTools:
    def __init__(self, web, config):
        self.web, self.config = web, config

    def read_document(self, url, max_chars=None, max_pages=None, start_page=1,
                      ocr=None, vision=False, question="", refresh=False):
        """Extract a bounded public PDF, XLSX/XLS, CSV or DOCX; cite section locations."""
        return self._read(url, "document", max_chars, max_pages, start_page, ocr, vision, question, refresh)

    def read_image(self, url, ocr=None, vision=False, question="", max_chars=None, refresh=False):
        """Read a public PNG/JPEG/WebP/TIFF using allowed OCR/vision; no arbitrary model settings."""
        return self._read(url, "image", max_chars, 1, 1, ocr, vision, question, refresh)

    def _read(self, url, kind, maximum, pages, start, ocr, vision, question, refresh):
        payload = {"url": url, "kind": kind, "start_page": start, "vision": vision,
                   "question": question, "refresh": refresh}
        for key, value in (("max_chars", maximum), ("max_pages", pages), ("ocr", ocr)):
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
                return failure(error.get("code") if isinstance(error, dict) else "parse_error", requested)
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
        return {key: result.get(key) for key in ("status", "requested_url", "final_url", "title", "links", "images", "error", "warnings")}
