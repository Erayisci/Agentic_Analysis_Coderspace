"""Render a URL in a headless browser and return the resulting HTML.

`web_url._extract_html` reads whatever the server sent back -- for a
JS-rendered page (a React/Angular/Vue front end that fetches its own data
after load) that is the empty shell, not the numbers a question asked for.
Measured on `borsaistanbul.com/endeks/xtumy`: the server response has empty
`<td id="totalMarketValue">` and friends; rendering the page in a browser
and reading it back after network activity settles fills them in.

This is a distinct, optional capability injected into `read_url` the same
way `ocr` is -- a plain `str -> bytes` callable, not a hard dependency of
`web_url` itself, so the module (and its tests) work with no browser
installed. The caller (`backend.api.main`) binds it unconditionally, since
rendering is local and needs no API key, unlike the `ocr` binding which
needs a Kloudeks client.

`web_url._guard_url` (private SSRF check) is reused verbatim before and
after navigation -- once for the URL as given, again for `page.url` after
the browser follows any redirect or client-side navigation, so a page that
redirects toward a private address after load is still caught.
"""
from typing import Optional

from .web_url import _guard_url

RENDER_TIMEOUT_MS = 20_000


def render(url: str, timeout_ms: int = RENDER_TIMEOUT_MS) -> bytes:
    """Return the fully-rendered HTML of `url` after JS has run.

    Raises RuntimeError if Playwright (or its browser binary) is not
    installed, and ValueError for anything `_guard_url` rejects.
    """
    _guard_url(url)
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is not installed -- `pip install playwright && "
            "playwright install chromium` to enable JS-rendered page reads"
        ) from exc

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                _guard_url(page.url)
                content = page.content()
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise RuntimeError(f"headless render of {url!r} failed: {exc}") from exc
    return content.encode("utf-8")
