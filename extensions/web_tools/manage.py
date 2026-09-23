"""Explicit lifecycle and smoke commands for this extension's Compose project."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ENV_PATH = HERE / ".env"
sys.path.insert(0, str(ROOT))


def read_env(path):
    """Read literal KEY=value entries; never evaluate shell code or expansions."""
    values = {}
    if not path.exists():
        return values
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"{path.name}:{number}: expected a literal KEY=value entry")
        if key in values:
            raise ValueError(f"{path.name}:{number}: duplicate {key}")
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"{path.name}:{number}: unmatched quote")
            value = value[1:-1]
        values[key] = value
    return values


def environment():
    values = read_env(HERE / ".env.example")
    values.update(read_env(ENV_PATH))
    values.update(os.environ)
    for port in ("WEB_SEARCH_PORT", "WEB_CRAWLER_PORT"):
        if not values[port].isdigit() or not 1 <= int(values[port]) <= 65535:
            raise ValueError(f"{port} must be an integer from 1 to 65535")
    if values["WEB_SEARCH_PORT"] == values["WEB_CRAWLER_PORT"]:
        raise ValueError("WEB_SEARCH_PORT and WEB_CRAWLER_PORT must differ")
    if not values.get("WEB_SEARXNG_URL"):
        values["WEB_SEARXNG_URL"] = "http://127.0.0.1:" + values["WEB_SEARCH_PORT"]
    if not values.get("WEB_CRAWLER_URL"):
        values["WEB_CRAWLER_URL"] = "http://127.0.0.1:" + values["WEB_CRAWLER_PORT"]
    return values


def setup():
    defaults = read_env(HERE / ".env.example")
    existing = read_env(ENV_PATH)
    content = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else (HERE / ".env.example").read_text(encoding="utf-8")
    present = existing if ENV_PATH.exists() else defaults.copy()
    generated = {
        "WEB_COMPOSE_PROJECT": "kkb-web-" + secrets.token_hex(6),
        "SEARXNG_SECRET": secrets.token_hex(32),
    }
    for key, default in defaults.items():
        if key not in present:
            content = content.rstrip("\n") + "\n" + key + "=" + generated.get(key, default) + "\n"
        elif key in generated and not present[key]:
            content = re.sub(r"^[ \t]*" + key + r"\s*=.*$", key + "=" + generated[key], content, flags=re.MULTILINE)
    if not ENV_PATH.exists() or content != ENV_PATH.read_text(encoding="utf-8"):
        if ENV_PATH.is_symlink():
            raise ValueError("Refusing to replace a symlink at extensions/web_tools/.env")
        # This file is private and local. Existing user values are preserved.
        descriptor = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
        ENV_PATH.chmod(0o600)
    print("Prepared extensions/web_tools/.env; existing values preserved. Tools remain opt-in.")
    issue = docker_issue(environment())
    if issue:
        print("Prerequisite missing: " + issue)
    else:
        print("Docker Engine and Compose are available. Run web-tools start when ready.")
    return 0


def docker_issue(env):
    if not shutil.which("docker", path=env.get("PATH")):
        return "Docker is not installed or on PATH. Follow extensions/web_tools/README.md, Docker prerequisite."
    for args, message in (
        (["docker", "compose", "version"], "Docker Compose v2 is unavailable."),
        (["docker", "info", "--format", "{{.ServerVersion}}"], "Docker Engine is stopped or inaccessible to this WSL user."),
    ):
        try:
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return message + " See extensions/web_tools/README.md, Docker prerequisite."
        if result.returncode:
            return message + " See extensions/web_tools/README.md, Docker prerequisite."
    return None


def compose(env, args):
    from backend.extensions.web_tools.asset_config import AssetConfig
    if not ENV_PATH.exists():
        raise ValueError("Run ./extensions/web_tools/web-tools setup first")
    project = env.get("WEB_COMPOSE_PROJECT", "")
    if not re.fullmatch(r"kkb-web-[a-z0-9][a-z0-9_-]{0,48}", project):
        raise ValueError("WEB_COMPOSE_PROJECT must begin with kkb-web-; run setup to generate a private project name")
    if not env.get("SEARXNG_SECRET"):
        raise ValueError("Missing SEARXNG_SECRET; run setup")
    issue = docker_issue(env)
    if issue:
        raise ValueError(issue)
    command = ["docker", "compose", "--project-name", project, "--env-file", str(ENV_PATH), "--file", str(HERE / "compose.yaml")]
    if AssetConfig.from_environ(env).needs_image:
        command += ["--file", str(HERE / "compose.assets.yaml")]
    return subprocess.run(command + args, cwd=ROOT, env=env).returncode


def emit(result):
    print(json.dumps(result, indent=2, ensure_ascii=False))


def tool_mapping(env):
    from backend.tools import get_tools

    tools = get_tools(env)
    if not tools:
        raise ValueError("feature_disabled: set WEB_TOOLS_ENABLED=true for this command or in extensions/web_tools/.env")
    return tools


def check(env):
    from backend.extensions.web_tools.client import ToolFailure, _request_json
    from backend.extensions.web_tools.config import WebToolsConfig
    from backend.extensions.web_tools.asset_config import AssetConfig

    config = WebToolsConfig.from_environ(env)
    # Readiness is independent of host registration and sends no public query.
    checks = {}
    for service, url, expected in (
        ("crawler", config.crawler_url + "/health", "status"),
        ("searxng", config.searxng_url + "/config", "engines"),
    ):
        try:
            payload = _request_json(url, timeout=8)
            if expected not in payload:
                raise ValueError("Unexpected readiness response")
            if service == "crawler" and payload["status"] != "ok":
                raise ValueError("Crawler is not ready")
            if service == "searxng" and not isinstance(payload["engines"], list):
                raise ValueError("Unexpected search configuration response")
            checks[service] = {"status": "ok"}
            if service == "crawler":
                requested = AssetConfig.from_environ(env)
                capabilities = payload.get("capabilities", {})
                checks[service]["capabilities"] = capabilities
                for name in ("documents", "images", "links", "ocr", "vision", "agent"):
                    if getattr(requested, name + "_enabled") and not capabilities.get(name):
                        raise ValueError(f"{name} is disabled in the running service; run web-tools start with the same settings")
        except (ToolFailure, ValueError) as error:
            checks[service] = {"status": "error", "message": str(error)}
    success = all(item["status"] == "ok" for item in checks.values())
    emit({"status": "ok" if success else "error", "checks": checks,
          "note": "Readiness only. Run smoke to verify JSON search and a real page read."})
    return 0 if success else 1


def smoke(env, query):
    tools = tool_mapping(env)
    search = tools["search_web"](query, max_results=3)
    reads = []
    for result in search.get("results", [])[:3]:
        read = tools["read_url"](result["url"], max_chars=2000)
        reads.append(read)
        if read.get("status") in {"ok", "partial"} and read.get("content"):
            break
    success = any(read.get("status") in {"ok", "partial"} and read.get("content") for read in reads)
    partial = search.get("status") == "partial" or any(read.get("status") != "ok" for read in reads)
    emit({"status": ("partial" if partial else "ok") if success else "error", "search": search, "reads": reads,
          "note": "Real network results; upstream blocks, empty results and failed reads are reported as received."})
    return 0 if success else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "This product includes software developed by UncleCode (https://x.com/unclecode) "
        "as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai)."
    ))
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "start", "check", "logs", "stop", "test", "browser-test", "asset-test", "config", "schemas"):
        sub.add_parser(name)
    search_parser = sub.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--max-results", type=int, default=5)
    search_parser.add_argument("--language")
    search_parser.add_argument("--time-range", choices=("day", "month", "year"))
    search_parser.add_argument("--domain", action="append", dest="domains")
    read_parser = sub.add_parser("read")
    read_parser.add_argument("url")
    read_parser.add_argument("--max-chars", type=int)
    assets_parser = sub.add_parser("assets", help="Discover public document/image links without downloading them")
    assets_parser.add_argument("url")
    for name in ("document", "image", "url"):
        parser_asset = sub.add_parser(name)
        parser_asset.add_argument("url")
        parser_asset.add_argument("--max-chars", type=int)
        parser_asset.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=None)
        parser_asset.add_argument("--vision", action="store_true")
        parser_asset.add_argument("--question", default="")
        parser_asset.add_argument("--refresh", action="store_true")
        if name in {"document", "url"}:
            parser_asset.add_argument("--max-pages", type=int)
            parser_asset.add_argument("--start-page", type=int, default=1)
    smoke_parser = sub.add_parser("smoke")
    smoke_parser.add_argument("query", nargs="?", default="SearXNG documentation")
    ask_parser = sub.add_parser("ask", help="Bounded optional MIA web research with source citations")
    ask_parser.add_argument("question")
    ask_parser.add_argument("--url", action="append", default=[], help="Starting URL; repeat to read several sources")
    ask_parser.add_argument("--require", action="append", dest="requirements", help="Required question to cover; repeat as needed")
    ask_parser.add_argument("--min-sources", type=int, default=1, help="Minimum distinct cited documents (not proof of independence)")
    ask_parser.add_argument("--allow-vision", action="store_true")
    ask_parser.add_argument("--max-tool-calls", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            return setup()
        if args.command == "test":
            return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(HERE / "tests"), "-p", "test_*.py", "-v"], cwd=ROOT).returncode
        env = environment()
        from backend.extensions.web_tools.asset_config import AssetConfig
        assets_config = AssetConfig.from_environ(env)
        if args.command == "config":
            # Never print the SearXNG secret or unrelated environment/credentials.
            from backend.extensions.web_tools.config import WebToolsConfig
            from backend.tools import get_tools
            WebToolsConfig.from_environ(env)
            enabled = bool(get_tools(env))
            secrets = {"SEARXNG_SECRET", "WEB_KLOUDEKS_API_KEY"}
            emit({key: value for key, value in env.items() if key in read_env(HERE / ".env.example") and key not in secrets}
                 | {"SEARXNG_SECRET": "[set]" if env.get("SEARXNG_SECRET") else "[missing]",
                    "WEB_KLOUDEKS_API_KEY": "[set]" if assets_config.kloudeks_api_key else "[missing]", "enabled": enabled})
            return 0
        if args.command in {"start", "stop", "logs", "browser-test", "asset-test"}:
            from backend.extensions.web_tools.config import WebToolsConfig
            WebToolsConfig.from_environ(env)
            commands = {"start": ["up", "--build", "--detach", "--wait", "--wait-timeout", "180"],
                        "stop": ["stop"], "logs": ["logs", "--tail", "100"],
                        "browser-test": ["exec", "-T", "--env", "WEB_TOOLS_TEST_BROWSER=1", "crawler",
                                         "python", "-m", "unittest", "discover", "-s", "/opt/web-tools/tests",
                                         "-p", "test_browser.py", "-v"]}
            commands["asset-test"] = ["exec", "-T", "--env", "WEB_TOOLS_TEST_ASSETS=1", "crawler",
                                      "python", "-m", "unittest", "discover", "-s", "/opt/web-tools/tests",
                                      "-p", "test_assets_integration.py", "-v"]
            if args.command == "asset-test" and not assets_config.needs_image:
                raise ValueError("Enable WEB_DOCUMENTS_ENABLED or WEB_IMAGES_ENABLED, then run start before asset-test")
            return compose(env, commands[args.command])
        if args.command == "check":
            return check(env)
        if args.command == "smoke":
            return smoke(env, args.query)
        tools = tool_mapping(env)
        if args.command == "schemas":
            from backend.extensions.web_tools.agent_protocol import tool_schemas
            emit(tool_schemas(assets_config, allow_vision=assets_config.vision_enabled))
            return 0
        if args.command == "ask":
            from backend.extensions.web_tools.research import research
            result = research(args.question, environ=env, urls=args.url, allow_vision=args.allow_vision,
                              max_tool_calls=args.max_tool_calls, requirements=args.requirements, min_sources=args.min_sources)
        elif args.command == "search":
            result = tools["search_web"](args.query, args.max_results, args.language, args.time_range, args.domains)
        elif args.command == "read":
            result = tools["read_url"](args.url, args.max_chars)
        else:
            tool = {"document": "read_document", "image": "read_image", "url": "read_web_url", "assets": "get_page_assets"}[args.command]
            if tool not in tools:
                raise ValueError(f"feature_disabled: enable the corresponding WEB_DOCUMENTS_ENABLED, WEB_IMAGES_ENABLED, or WEB_LINKS_ENABLED flag for {args.command}")
            kwargs = {} if args.command == "assets" else {
                "max_chars": args.max_chars, "ocr": args.ocr, "vision": args.vision,
                "question": args.question, "refresh": args.refresh}
            if args.command in {"document", "url"}:
                kwargs.update(max_pages=args.max_pages, start_page=args.start_page)
            result = tools[tool](args.url, **kwargs)
        emit(result)
        return 1 if result.get("status") == "error" or result.get("error") else 0
    except (OSError, ValueError) as error:
        print("web-tools: " + str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
