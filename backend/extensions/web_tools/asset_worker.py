"""Run untrusted file parsing/OCR in its own bounded-lifetime process group."""

import contextlib
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from .asset_common import AssetFailure, failure, normalize
from .asset_config import AssetConfig
from .security import UnsafeURL


def process(payload, proxy, config=None):
    from .asset_cache import AssetStore
    from .asset_download import download
    from .asset_extract import extract
    config = config or AssetConfig.from_environ()
    request = normalize(payload, config)
    # Model outputs are never cached: disabling a model or changing credentials
    # cannot reveal a previous model response or hide model-call accounting.
    cacheable = config.asset_cache_enabled and not request["vision"] and not (
        request["ocr"] and config.ocr_provider == "kloudeks")
    store = AssetStore(config) if cacheable else None
    key = store.key(request) if store else None
    if store and not request["refresh"]:
        cached = store.get(key)
        if cached:
            return cached
    with tempfile.TemporaryDirectory(prefix="web-asset-") as directory:
        path = Path(directory) / "download.bin"
        metadata = download(request["url"], proxy, path, request["max_bytes"], config.asset_timeout_seconds)
        result = extract(path, request, metadata, config, proxy)
    if store:
        store.put(key, result)
    return result


def run_isolated(request, proxy, timeout, popen=subprocess.Popen):
    process = popen([sys.executable, "-m", "backend.extensions.web_tools.asset_worker"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True)
    try:
        stdout, _ = process.communicate(json.dumps({"request": request, "proxy": proxy}).encode(), timeout=timeout)
        if process.returncode != 0 or len(stdout) > 2 * 1024 * 1024:
            return failure("parse_error", request["url"])
        return json.loads(stdout)
    except subprocess.TimeoutExpired:
        return failure("timeout", request["url"])
    except (ValueError, OSError):
        return failure("parse_error", request["url"])
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()


def main():
    from backend.model_clients.kloudeks import ModelFailure
    payload = json.loads(sys.stdin.buffer.read(32769))
    url = payload.get("request", {}).get("url")
    with open(os.devnull, "w") as silent, contextlib.redirect_stdout(silent), contextlib.redirect_stderr(silent):
        try:
            if payload["request"].get("operation") == "agent-model":
                from .agent_protocol import model_decision
                result = model_decision(payload["request"], payload["proxy"], AssetConfig.from_environ())
            else:
                result = process(payload["request"], payload["proxy"])
        except (AssetFailure, ModelFailure) as error:
            result = failure(error.code, url)
            if getattr(error, "http_status", None) is not None:
                result["error"]["http_status"] = error.http_status
        except UnsafeURL:
            result = failure("invalid_url", url)
        except (ModuleNotFoundError, ImportError, FileNotFoundError):
            result = failure("missing_dependency", url)
        except (TimeoutError, subprocess.TimeoutExpired):
            result = failure("timeout", url)
        except (OSError, http.client.HTTPException):
            result = failure("upstream_error", url)
        except Exception:
            result = failure("parse_error", url)
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
