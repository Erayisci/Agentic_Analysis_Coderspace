"""Unified URL entry point for teammates using İlmay's proposed import path.

The callable delegates to the optional Docker service. Its return value follows
the documented structured-evidence contract, not the earlier branch's text dict.
No parsing dependencies are imported into the baseline Python environment.
"""


def read_url(url, **options):
    from backend.tools import get_tools
    from backend.extensions.web_tools.asset_common import failure
    tool = get_tools().get("read_web_url")
    return tool(url, **options) if tool else failure("feature_disabled")
