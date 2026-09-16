"""Read a URL given in the prompt and extract meaning from its content.

Dispatch is decided from the response's Content-Type header (falling back to
the URL's file extension when a server sends something generic, like
application/octet-stream), never from a hint the caller supplies -- so a
mislabelled or unexpected resource fails loudly instead of being parsed as
the wrong format.

Every request resolves its hostname -- and every hop of any redirect it
follows -- and rejects private, loopback, link-local and other non-public
addresses before connecting. This tool fetches an arbitrary URL that ends up
here from natural-language input, so it must not become a way to make the
deployed agent reach internal services (SSRF).

The image path needs a vision-capable model behind the Kloudeks client
(Launch.MD S5.1: "The image path requires a VLM"); that client does not exist
in this repo yet, so `read_url` raises NotImplementedError for image content
instead of silently returning nothing for it.
"""
import ipaddress
import socket
from io import BytesIO
from typing import Optional
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

REQUEST_TIMEOUT = 20
MAX_REDIRECTS = 5
MAX_TEXT_CHARS = 20_000  # keeps tool output bounded for the LLM's context window
USER_AGENT = "kkb-hackathon-web-url-agent/0.1"

EXCEL_CONTENT_TYPES = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
)


def _bounded(text: str) -> tuple:
    """(possibly-truncated text, was it truncated). One place for the limit
    so pdf/html/text extraction can never disagree on how it's applied."""
    return text[:MAX_TEXT_CHARS], len(text) > MAX_TEXT_CHARS


def _reject_non_public(hostname: str, url: str) -> None:
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"could not resolve host {hostname!r}: {exc}") from exc
    for family, _, _, _, sockaddr in addresses:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"refusing to fetch {url!r}: {hostname!r} resolves to a non-public address ({ip})")


def _guard_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError(f"URL has no hostname: {url!r}")
    _reject_non_public(parsed.hostname, url)


def _fetch(url: str) -> requests.Response:
    """GET url, validating every redirect hop before following it."""
    for _ in range(MAX_REDIRECTS + 1):
        _guard_url(url)
        response = requests.get(
            url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT},
            allow_redirects=False,
        )
        if response.is_redirect or response.is_permanent_redirect:
            url = urljoin(url, response.headers["Location"])
            continue
        response.raise_for_status()
        return response
    raise ValueError(f"too many redirects while fetching {url!r}")


def _detect_kind(content_type: str, url: str) -> str:
    content_type = (content_type or "").split(";")[0].strip().lower()
    path = urlparse(url).path.lower()

    if content_type == "application/pdf" or path.endswith(".pdf"):
        return "pdf"
    if content_type in EXCEL_CONTENT_TYPES or path.endswith((".xlsx", ".xls")):
        return "excel"
    if content_type.startswith("image/") or path.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
        return "image"
    if content_type in ("text/html", "application/xhtml+xml") or path.endswith((".html", ".htm")):
        return "html"
    if content_type.startswith("text/") or content_type == "application/json":
        return "text"
    raise ValueError(f"unrecognised content type {content_type!r} for {url!r}")


def _extract_pdf(content: bytes) -> dict:
    reader = PdfReader(BytesIO(content))
    pages = [page.extract_text() or "" for page in reader.pages]
    text, truncated = _bounded("\n\n".join(pages))
    return {
        "kind": "pdf",
        "n_pages": len(reader.pages),
        "text": text,
        "truncated": truncated,
    }


def _extract_excel(content: bytes) -> dict:
    sheets = pd.read_excel(BytesIO(content), sheet_name=None)
    return {
        "kind": "excel",
        "sheet_names": list(sheets.keys()),
        "sheets": {
            name: {
                "columns": [str(c) for c in frame.columns],
                "n_rows": int(len(frame)),
                "preview": frame.head(20).astype(str).to_dict(orient="records"),
            }
            for name, frame in sheets.items()
        },
    }


def _extract_html(content: bytes) -> dict:
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    raw_text = " ".join(soup.get_text(separator=" ").split())
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    text, truncated = _bounded(raw_text)
    return {
        "kind": "html",
        "title": title,
        "text": text,
        "truncated": truncated,
    }


def _extract_text(content: bytes, encoding: Optional[str]) -> dict:
    text, truncated = _bounded(content.decode(encoding or "utf-8", errors="replace"))
    return {
        "kind": "text",
        "text": text,
        "truncated": truncated,
    }


def read_url(url: str) -> dict:
    """Fetch a URL and return its content as a JSON-serialisable dict.

    Supports PDF (pypdf), Excel (pandas/openpyxl) and text/HTML content, each
    truncated to MAX_TEXT_CHARS so a large document cannot blow out the
    calling LLM's context window. Raises NotImplementedError for images --
    see the module docstring -- and ValueError for anything else (bad scheme,
    unresolvable host, non-public address, unrecognised content type, too
    many redirects).
    """
    response = _fetch(url)
    kind = _detect_kind(response.headers.get("Content-Type", ""), url)

    if kind == "pdf":
        result = _extract_pdf(response.content)
    elif kind == "excel":
        result = _extract_excel(response.content)
    elif kind == "html":
        result = _extract_html(response.content)
    elif kind == "text":
        result = _extract_text(response.content, response.encoding)
    elif kind == "image":
        raise NotImplementedError(
            "image URLs need a vision-capable model via the Kloudeks client, "
            "which does not exist in this repo yet (see Launch.MD Phase 3)"
        )
    else:
        raise ValueError(f"unhandled content kind: {kind!r}")

    result["url"] = url
    result["status_code"] = response.status_code
    return result
