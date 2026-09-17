"""Opt-in document/image policies shared by the CLI and isolated worker."""

from dataclasses import dataclass, field, fields
import os
from urllib.parse import urlsplit


def boolean(value, name):
    value = str(value).strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off", ""}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class AssetConfig:
    documents_enabled: bool = False
    images_enabled: bool = False
    links_enabled: bool = False
    ocr_enabled: bool = False
    vision_enabled: bool = False
    agent_enabled: bool = False
    agent_max_tool_calls: int = 6
    agent_max_model_calls: int = 6
    agent_max_context_chars: int = 20000
    agent_max_sources: int = 6
    agent_max_download_bytes: int = 20971520
    agent_max_evidence_bytes: int = 8388608
    agent_timeout_seconds: int = 300
    asset_cache_enabled: bool = False
    asset_max_bytes: int = 10485760
    asset_timeout_seconds: int = 120
    asset_max_chars: int = 20000
    asset_max_pages: int = 10
    asset_max_sheets: int = 3
    asset_max_rows: int = 200
    asset_max_columns: int = 50
    asset_max_archive_bytes: int = 52428800
    asset_max_image_pixels: int = 10000000
    asset_image_edge: int = 1600
    asset_max_links: int = 50
    asset_max_images: int = 3
    asset_cache_ttl_seconds: int = 3600
    asset_cache_max_bytes: int = 104857600
    ocr_max_pages: int = 3
    ocr_provider: str = "local"
    ocr_languages: str = "eng+tur"
    model_max_calls_per_read: int = 1
    model_max_calls_per_hour: int = 20
    model_max_tokens: int = 2048
    kloudeks_base_url: str = "https://mia.csp.kloudeks.com/v1"
    kloudeks_vision_model: str = "kkbhackathon2026/Qwen3.8-27B"
    kloudeks_ocr_model: str = "kkbhackathon2026/Unlimited-OCR"
    kloudeks_chat_model: str = "kkbhackathon2026/Qwen3.8-27B"
    kloudeks_api_key: str = field(default="", repr=False)

    @property
    def needs_image(self):
        return self.documents_enabled or self.images_enabled or self.agent_enabled

    def __post_init__(self):
        bounds = {
            "asset_max_bytes": (1024, 52428800), "asset_timeout_seconds": (5, 300),
            "asset_max_chars": (100, 50000), "asset_max_pages": (1, 50),
            "asset_max_sheets": (1, 20), "asset_max_rows": (1, 2000),
            "asset_max_columns": (1, 200), "asset_max_archive_bytes": (1024, 104857600),
            "asset_max_image_pixels": (10000, 40000000), "asset_image_edge": (256, 3000),
            "asset_max_links": (1, 200), "asset_max_images": (1, 5),
            "asset_cache_ttl_seconds": (1, 604800), "asset_cache_max_bytes": (1048576, 1073741824),
            "ocr_max_pages": (1, 20), "model_max_calls_per_read": (0, 5),
            "model_max_calls_per_hour": (0, 1000), "model_max_tokens": (128, 8192),
            "agent_max_tool_calls": (1, 12), "agent_max_model_calls": (1, 12),
            "agent_max_context_chars": (2000, 24000),
            "agent_max_sources": (1, 12), "agent_max_download_bytes": (1024, 209715200),
            "agent_max_evidence_bytes": (4096, 33554432), "agent_timeout_seconds": (5, 1800),
        }
        for name, (lower, upper) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"WEB_{name.upper()} must be an integer from {lower} to {upper}")
        for item in fields(self):
            if item.name.endswith("_enabled") and type(getattr(self, item.name)) is not bool:
                raise ValueError(f"WEB_{item.name.upper()} must be true or false")
        if self.ocr_provider not in {"local", "kloudeks"}:
            raise ValueError("WEB_OCR_PROVIDER must be local or kloudeks")
        if self.ocr_languages not in {"eng", "tur", "eng+tur", "tur+eng"}:
            raise ValueError("WEB_OCR_LANGUAGES must be eng, tur, or eng+tur")
        parts = urlsplit(self.kloudeks_base_url)
        if (parts.scheme != "https" or not parts.hostname
                or not parts.hostname.endswith(".kloudeks.com") or parts.port not in {None, 443}
                or parts.username is not None or parts.password is not None or parts.query or parts.fragment):
            raise ValueError("WEB_KLOUDEKS_BASE_URL must be an HTTPS Kloudeks URL without credentials")
        for name in ("kloudeks_vision_model", "kloudeks_ocr_model", "kloudeks_chat_model"):
            value = getattr(self, name)
            if not value.startswith("kkbhackathon2026/") or len(value) > 200 or any(ord(c) < 33 for c in value):
                raise ValueError(f"WEB_{name.upper()} must use the exact kkbhackathon2026/ model ID")

    @classmethod
    def from_environ(cls, environ=None):
        env = os.environ if environ is None else environ
        values = {}
        for item in fields(cls):
            name = "WEB_" + item.name.upper()
            if name not in env:
                continue
            value = env[name]
            if item.type is bool:
                value = boolean(value, name)
            elif item.type is int:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{name} must be an integer") from None
            values[item.name] = value
        if not values.get("kloudeks_api_key"):
            values["kloudeks_api_key"] = env.get("MIA_API_KEY", "")
        return cls(**values)
