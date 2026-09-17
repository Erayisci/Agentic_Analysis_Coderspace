"""Validated web-only settings. No dotenv loading or baseline configuration edits."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit


def _service_url(value: str, name: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and not any(ord(char) < 33 or ord(char) > 126 for char in value)
            and "\\" not in value
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError(f"{name} must be an HTTP(S) service URL without credentials, query or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class WebToolsConfig:
    searxng_url: str = "http://127.0.0.1:8888"
    crawler_url: str = "http://127.0.0.1:8932"
    search_timeout_seconds: float = 15
    crawl_timeout_seconds: float = 45
    max_results: int = 10
    max_content_chars: int = 20000
    max_concurrency: int = 2
    retries: int = 1

    def __post_init__(self):
        for field in ("searxng_url", "crawler_url"):
            object.__setattr__(self, field, _service_url(getattr(self, field), "WEB_" + field.upper()))
        for field, lower, upper, integer in (
            ("search_timeout_seconds", 1, 120, False),
            ("crawl_timeout_seconds", 1, 180, False),
            ("max_results", 1, 50, True),
            ("max_content_chars", 100, 100000, True),
            ("max_concurrency", 1, 8, True),
            ("retries", 0, 3, True),
        ):
            value = getattr(self, field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not lower <= value <= upper
                or (integer and not isinstance(value, int))
            ):
                raise ValueError(f"WEB_{field.upper()} must be {'an integer' if integer else 'a number'} from {lower} to {upper}")

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "WebToolsConfig":
        environ = os.environ if environ is None else environ
        values = {}
        for field in cls.__dataclass_fields__:
            name = "WEB_" + field.upper()
            if name not in environ:
                continue
            value = environ[name]
            if not field.endswith("_url"):
                try:
                    value = float(value) if field.endswith("_seconds") else int(value)
                except (ValueError, TypeError):
                    raise ValueError(f"{name} must be numeric") from None
            values[field] = value
        return cls(**values)
