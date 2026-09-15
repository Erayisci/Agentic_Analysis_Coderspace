"""Structured discovery from page DOM; linked files are not fetched here."""

from urllib.parse import urlsplit
from .security import UnsafeURL, validate_url_syntax


def public_assets(records, maximum):
    results, seen = [], set()
    for record in records[:1000]:
        try:
            url = validate_url_syntax(record.get("url"))
        except (UnsafeURL, TypeError, AttributeError):
            continue
        if url in seen:
            continue
        seen.add(url)
        suffix = urlsplit(url).path.rsplit(".", 1)[-1].lower()
        hint = suffix if suffix in {"pdf", "xlsx", "xls", "csv", "docx", "png", "jpg", "jpeg", "webp", "tif", "tiff"} else "html"
        results.append({"url": url, "text": str(record.get("text", ""))[:500], "type_hint": hint})
    # Navigation often appears before report downloads in the DOM. Rank known
    # file types first so a small discovery budget still surfaces useful files.
    results.sort(key=lambda item: item["type_hint"] == "html")
    return results[:maximum]
