"""Analytical tools the agent calls: pure functions over lakehouse series.

Every function takes plain Python / pandas inputs and returns a JSON-serialisable
dict, so it can be registered as an LLM tool without an adapter. None of them
call a model: the agent decides *which* tool runs, the tool decides what the
numbers are.

`get_tools()` is the opt-in registry for the web-tools extension
(`WEB_TOOLS_ENABLED`, see `extensions/web_tools/`); importing this package
starts nothing.
"""

import os
from collections.abc import Callable, Mapping


def get_tools(environ: Mapping[str, str] | None = None) -> dict[str, Callable]:
    """Return enabled tools without loading crawler packages or starting services.

    Keep the returned mapping for the lifetime of the caller so its web tools
    share a concurrency limit. Optional web research lives in web_tools.research.
    """
    environ = os.environ if environ is None else environ
    enabled = environ.get("WEB_TOOLS_ENABLED", "false").strip().lower()
    if enabled in {"", "0", "false", "no", "off"}:
        return {}
    if enabled not in {"1", "true", "yes", "on"}:
        raise ValueError("WEB_TOOLS_ENABLED must be true or false")

    from backend.extensions.web_tools.client import WebTools
    from backend.extensions.web_tools.config import WebToolsConfig
    from backend.extensions.web_tools.asset_config import AssetConfig

    web = WebTools(WebToolsConfig.from_environ(environ))
    assets = AssetConfig.from_environ(environ)
    web.asset_config = assets
    from backend.extensions.web_tools.asset_client import AssetTools
    extra = AssetTools(web, assets)
    result = {"search_web": web.search_web, "read_url": web.read_url, "read_web_url": extra.read_web_url}
    if assets.needs_image or assets.links_enabled:
        if assets.documents_enabled:
            result["read_document"] = extra.read_document
        if assets.images_enabled:
            result["read_image"] = extra.read_image
        if assets.links_enabled:
            result["get_page_assets"] = extra.get_page_assets
    return result
