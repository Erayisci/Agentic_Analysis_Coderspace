"""The document layer: a URL -> the web-tools extension's evidence dict.

Two routes, one contract. The extension (`backend/extensions/web_tools`)
already turns PDF, XLSX/XLS, CSV, DOCX, images and rendered HTML into
`{"format", "sections": [{"location", "method", "text", "rows"?}], ...}`,
and that is the only shape the rest of this package reads:

- **container route** -- when `WEB_TOOLS_ENABLED=true` and the extension's
  containers are up, `read_web_url` runs the extractor in its isolated worker
  (Crawl4AI for JavaScript pages, service-enforced limits, an egress proxy)
  and `get_page_assets` discovers the links on a page;
- **in-process route** -- otherwise the same `asset_extract.extract()` runs
  inside this process, on bytes fetched through `tools.web_url._fetch` (the
  repo's SSRF guard, every redirect hop re-validated). HTML is read with
  BeautifulSoup here, tables and links included, because Crawl4AI is a
  container-only dependency.

Demo day therefore works with or without Docker, and a source landed through
either route parses identically downstream (`tables.tables_from_evidence`).

Extracted text is evidence, never instructions: nothing here reaches a model
except through the extractor's own OCR call, whose prompt is fixed.
"""
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ...core.config import EXTERNAL_CACHE_DIR, EXTERNAL_MAX_DOWNLOAD_BYTES, kloudeks_api_key
from ...core.labels import TURKISH_TO_ASCII
from ...extensions.web_tools.asset_common import AssetFailure, failure, normalize
from ...extensions.web_tools.page_assets import public_assets
from ...tools import web_url
from ...tools.lakehouse import _terms

DOCUMENT_TYPES = {"pdf", "xlsx", "xls", "csv", "docx", "document"}
EXTENSIONS = {"pdf": "pdf", "xlsx": "xlsx", "xls": "xls", "csv": "csv", "docx": "docx",
              "image": "png", "html": "html", "text": "txt"}
MAX_LINKS = 200

_TOOLS: Optional[dict] = None
_TOOLS_RESOLVED = False


def available_tools() -> dict:
    """The extension's tool registry when it is enabled, else {}. Resolved once
    per process (the registry's docstring asks for that: its tools share a
    concurrency limit)."""
    global _TOOLS, _TOOLS_RESOLVED
    if not _TOOLS_RESOLVED:
        from ...tools import get_tools
        try:
            _TOOLS = get_tools() or {}
        except ValueError:                         # WEB_TOOLS_ENABLED=garbage or a bad WEB_* value
            _TOOLS = {}
        _TOOLS_RESOLVED = True
    return _TOOLS


def configure_tools(tools: Optional[dict]) -> None:
    """Pin the registry (tests, or an API that resolved it at startup)."""
    global _TOOLS, _TOOLS_RESOLVED
    _TOOLS, _TOOLS_RESOLVED = (tools or {}), True


def extraction_route(tools: Optional[dict] = None) -> str:
    tools = available_tools() if tools is None else tools
    return "container" if tools and "read_web_url" in tools else "in_process"


# -- public entry point ----------------------------------------------------------------

def read_document(url: str, *, hint: str = "", tools: Optional[dict] = None) -> dict:
    """Fetch and extract one URL. Never raises for a document problem -- the
    evidence dict carries `status="error"` and `error` then; it raises
    ValueError only for a URL the SSRF guard refuses or a download over the cap."""
    tools = available_tools() if tools is None else tools
    if tools and "read_web_url" in tools:
        return _read_container(url, tools)
    return _read_in_process(url)


# -- container route ----------------------------------------------------------------------

def _read_container(url: str, tools: dict) -> dict:
    result = dict(tools["read_web_url"](url))
    result.setdefault("requested_url", url)
    result["extraction_route"] = "container"
    if result.get("status") == "error":
        return result
    result["n_bytes"] = result.get("downloaded_bytes")
    result["kind"] = _kind(result.get("format"))
    if result.get("format") == "html" and "get_page_assets" in tools:
        assets = tools["get_page_assets"](url)
        result["links"] = list(assets.get("links") or [])
    return result


# -- in-process route ------------------------------------------------------------------------

def _read_in_process(url: str) -> dict:
    response = web_url._fetch(url)                      # SSRF guard + redirect validation live here
    content = response.content
    declared = response.headers.get("Content-Length")
    if (declared and declared.isdigit() and int(declared) > EXTERNAL_MAX_DOWNLOAD_BYTES) \
            or len(content) > EXTERNAL_MAX_DOWNLOAD_BYTES:
        raise ValueError(f"{url!r} exceeds the {EXTERNAL_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB download cap")
    header = response.headers.get("Content-Type", "") or ""
    content_type = header.split(";")[0].strip().lower()
    charset = _charset(header)
    final_url = getattr(response, "url", None) or url
    head = content[:2048].lower()
    is_html = content_type in ("text/html", "application/xhtml+xml") or b"<html" in head or b"<!doctype html" in head
    native = _native_kind(content, content_type, url)
    if is_html:
        result = _read_html(url, content, final_url)
    elif native == "csv":
        # The extractor caps a table at 2,000 rows and keeps the OLDEST ones,
        # which is exactly the wrong end of a daily series that starts in 1987.
        # CSV and XLSX are simple enough to read whole; PDF, XLS, DOCX and
        # images stay with the extractor.
        result = _read_csv_native(url, content, charset, final_url)
    elif native == "xlsx":
        result = _read_xlsx_native(url, content, final_url)
    else:
        result = _extract_file(url, content, content_type, charset, final_url)
    result["requested_url"] = url
    result["content_sha256"] = hashlib.sha256(content).hexdigest()
    result["n_bytes"] = len(content)
    result["content_type"] = result.get("content_type") or content_type
    result["extraction_route"] = "in_process"
    result["_raw_bytes"] = content
    return result


MAX_NATIVE_GRID_ROWS = 100_000
MAX_NATIVE_SHEETS = 20


def _native_kind(content: bytes, content_type: str, url: str) -> Optional[str]:
    """'csv' or 'xlsx' when this process can read the bytes whole, else None."""
    if content.startswith(b"PK") and b"xl/workbook.xml" in content[:200_000]:
        return "xlsx"
    if content.startswith(b"PK"):
        import zipfile
        from io import BytesIO
        try:
            if "xl/workbook.xml" in zipfile.ZipFile(BytesIO(content)).namelist():
                return "xlsx"
        except zipfile.BadZipFile:
            return None
        return None
    path = urlsplit(url).path.lower()
    if content_type in web_url.CSV_CONTENT_TYPES or path.endswith(".csv") or (
            content_type in ("text/plain", "application/octet-stream", "") and path.endswith(".txt")
            and b"," in content[:4096]):
        return "csv"
    return None


def _native_result(url: str, final_url: str, fmt: str, sections: list, warnings: list) -> dict:
    from ...extensions.web_tools.asset_common import timestamp
    return {"status": "ok" if any(s.get("rows") for s in sections) else "empty", "format": fmt,
            "kind": _kind(fmt), "final_url": final_url, "requested_url": url, "title": None,
            "content": "", "sections": sections, "warnings": warnings, "processing_errors": [],
            "error": None, "fetched_at": timestamp(), "source_trust": "untrusted_external"}


def _read_csv_native(url: str, content: bytes, charset: Optional[str], final_url: str) -> dict:
    import csv
    from io import StringIO
    text = content.decode(charset or "utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    csv.field_size_limit(1_048_576)
    rows, warnings = [], []
    for index, row in enumerate(csv.reader(StringIO(text), dialect)):
        if index >= MAX_NATIVE_GRID_ROWS:
            warnings.append(f"CSV truncated at {MAX_NATIVE_GRID_ROWS} rows")
            break
        rows.append([str(cell)[:512] for cell in row])
    return _native_result(url, final_url, "csv", [{"location": "CSV rows starting at 1", "method": "table",
                                                   "rows": rows, "text": ""}], warnings)


def _read_xlsx_native(url: str, content: bytes, final_url: str) -> dict:
    import openpyxl
    from datetime import date, datetime
    from io import BytesIO
    book = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True, keep_links=False)
    sections, warnings = [], []
    try:
        if len(book.sheetnames) > MAX_NATIVE_SHEETS:
            warnings.append(f"only the first {MAX_NATIVE_SHEETS} of {len(book.sheetnames)} sheets were read")
        for sheet in book.worksheets[:MAX_NATIVE_SHEETS]:
            rows = []
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= MAX_NATIVE_GRID_ROWS:
                    warnings.append(f"sheet {sheet.title!r} truncated at {MAX_NATIVE_GRID_ROWS} rows")
                    break
                rows.append(["" if v is None else (v.isoformat() if isinstance(v, (date, datetime)) else str(v)[:512])
                             for v in row])
            sections.append({"location": f"Sheet {sheet.title}, from A1", "method": "table", "rows": rows, "text": ""})
    finally:
        book.close()
    warnings.append("Spreadsheet formulas are not executed; cached formula values may be missing or stale.")
    return _native_result(url, final_url, "xlsx", sections, warnings)


def _charset(header: str) -> Optional[str]:
    match = re.search(r"charset=([\w-]+)", header or "", re.I)
    return match.group(1) if match else None


def asset_config():
    """The extractor's policy for ingestion: every document feature on, the
    ceilings the extension allows, OCR through MIA when a key is configured."""
    from ...extensions.web_tools.asset_config import AssetConfig
    try:
        key = kloudeks_api_key()
    except RuntimeError:
        key = ""
    return AssetConfig(
        documents_enabled=True, images_enabled=True, links_enabled=True,
        ocr_enabled=bool(key), ocr_provider="kloudeks", vision_enabled=False,
        asset_max_rows=2000, asset_max_sheets=20, asset_max_pages=50, asset_max_columns=200,
        asset_max_chars=50000, asset_max_bytes=EXTERNAL_MAX_DOWNLOAD_BYTES,
        model_max_calls_per_read=5, model_max_tokens=8192, ocr_max_pages=9,
        asset_cache_enabled=False, kloudeks_api_key=key,
    )


def _extract_file(url: str, content: bytes, content_type: str, charset: Optional[str], final_url: str) -> dict:
    """The extension's extractor, in this process, on a temp copy of the bytes."""
    from ...extensions.web_tools.asset_extract import extract
    # The extractor's model-call counter is a SQLite file at a container path
    # by default; on the host it lives under the external zone.
    os.environ.setdefault("WEB_ASSET_CACHE_DIR", str(EXTERNAL_CACHE_DIR))
    config = asset_config()
    metadata = {"final_url": final_url, "content_type": content_type or "application/octet-stream",
                "charset": charset, "downloaded_bytes": len(content)}
    try:
        request = normalize({"url": url, "kind": "auto", "max_pages": config.asset_max_pages,
                             "max_chars": config.asset_max_chars, "ocr": config.ocr_enabled}, config)
    except AssetFailure as error:
        return failure(error.code, url)
    with tempfile.TemporaryDirectory(prefix="kkb-ingest-") as directory:
        path = Path(directory) / "download.bin"
        path.write_bytes(content)
        try:
            result = extract(str(path), request, metadata, config, None)
        except AssetFailure as error:
            return failure(error.code, url)
        except (ModuleNotFoundError, ImportError) as error:
            out = failure("missing_dependency", url)
            out["error"]["message"] += f" ({error}); pip install -e '.[ingest]'"
            return out
    result["kind"] = _kind(result.get("format"))
    if result.get("format") == "pdf":
        _add_word_grids(result, content)
    return result


def _usable_table(section: dict) -> bool:
    rows = section.get("rows") or []
    return len(rows) >= 4 and max((len(r) for r in rows), default=0) >= 2


def _add_word_grids(result: dict, content: bytes) -> None:
    """Typeset (unruled) PDF tables the extractor's line-based table finder
    misses: add a word-position grid for every page without a usable table."""
    from .pdf_words import grids_from_pdf
    covered = {s["location"].split(",")[0] for s in result.get("sections") or []
               if s.get("method") == "table" and _usable_table(s)}
    try:
        extra = grids_from_pdf(content)
    except Exception as error:                                   # noqa: BLE001 -- optional path
        result.setdefault("warnings", []).append(f"word-position table parsing failed: {error}")
        return
    result["sections"] = list(result.get("sections") or []) + [
        s for s in extra if s["location"].split(",")[0] not in covered]


def _read_html(url: str, content: bytes, final_url: str) -> dict:
    """Static HTML without a browser: visible text, every <table> as a row
    grid, every <a href> as a link candidate."""
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    from .ocr import html_tables_to_grids
    sections = []
    for number, table in enumerate(soup.find_all("table"), start=1):
        grids = html_tables_to_grids(str(table))       # spans expanded
        caption = table.find("caption")
        for rows in grids:
            sections.append({"location": f"HTML table {number}", "method": "table", "rows": rows,
                             "text": (caption.get_text(" ", strip=True) if caption else "")})
        table.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    if text:
        sections.insert(0, {"location": "Webpage", "method": "html_text", "text": text[:50000]})
    records = [{"url": urljoin(final_url, a["href"]), "text": a.get_text(" ", strip=True),
                "download": a.has_attr("download")} for a in soup.find_all("a", href=True)]
    return {
        "status": "ok" if sections else "empty", "format": "html", "kind": "html",
        "final_url": final_url, "title": title, "content": text[:50000], "sections": sections,
        "links": public_assets(records, MAX_LINKS), "warnings": [], "processing_errors": [], "error": None,
    }


# -- helpers ---------------------------------------------------------------------------------

def _kind(fmt: Optional[str]) -> str:
    return {"xlsx": "excel", "xls": "excel"}.get(fmt or "", fmt or "unknown")


def raw_extension(evidence: dict) -> str:
    return EXTENSIONS.get(evidence.get("format") or "", "bin")


def rank_links(links: List[dict], hint: str, base_url: str, maximum: int = 3) -> List[dict]:
    """Which linked documents to follow from a landing page: file links only
    (never navigation), scored by the question's terms against the link text
    and path, spreadsheets slightly above PDFs, same host slightly above others."""
    terms = [(term.translate(TURKISH_TO_ASCII), weight) for term, weight in _terms(hint or "")]
    base_host = (urlsplit(base_url).hostname or "").lower()
    scored = []
    for position, link in enumerate(links or []):
        if link.get("type_hint") not in DOCUMENT_TYPES:
            continue
        parts = urlsplit(link.get("url") or "")
        haystack = f"{link.get('text', '')} {parts.path}".lower().translate(TURKISH_TO_ASCII)
        score = sum(weight for term, weight in terms if term and term in haystack)
        score += {"xlsx": 1.0, "xls": 1.0, "csv": 1.0, "pdf": 0.5}.get(link.get("type_hint"), 0.25)
        if (parts.hostname or "").lower() == base_host:
            score += 0.5
        scored.append((-score, position, link))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [link for _, _, link in scored[:maximum]]
