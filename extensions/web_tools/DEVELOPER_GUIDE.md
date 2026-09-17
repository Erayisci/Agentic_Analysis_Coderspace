# Developer quick start: optional web tools

Start here to run the tools or connect them to your application. For AI tool
selection and evidence handling, read [AI_USAGE.md](AI_USAGE.md).

The extension provides web search and a unified URL reader for HTML, PDF,
Excel/CSV, DOCX, text and supported images. OCR, vision and multi-source research
are separately enabled capabilities. All tools are disabled by default.

## 1. Get the code and prerequisites

Use your existing checkout. The latest implementation was developed on `perhat`;
teammates need the commits containing these changes published to a shared branch
before pulling them. A branch name alone does not include uncommitted work.
Do not overwrite local changes or switch another developer's branch automatically.

Only if you do not already have a checkout, after the changes are published:

```bash
git clone --branch perhat https://github.com/Erayisci/Agentic_Analysis_Coderspace.git
cd Agentic_Analysis_Coderspace
```

Run the following commands from the repository root in Ubuntu/WSL Bash. You need
Python 3.10+ (the documented WSL setup uses 3.12), Git and a working Docker Engine
with Compose supporting `up --wait`:

```bash
python3 --version
docker version
docker compose version
```

`docker version` must show a working server as well as a client. If Docker is
missing or inaccessible, follow the [Docker prerequisites](README.md#wsl-bash-docker-prerequisite).
Internet access is needed for the initial image builds, searches and public URLs.

Keep your existing baseline `.venv` and startup commands. No baseline build,
database migration, `pip install`, browser installation or model key is required
just to try search and native document extraction using the CLI.

## 2. Configure and start the extension

```bash
./extensions/web_tools/web-tools setup

export WEB_TOOLS_ENABLED=true
export WEB_DOCUMENTS_ENABLED=true
export WEB_LINKS_ENABLED=true
export WEB_IMAGES_ENABLED=false
export WEB_OCR_ENABLED=false
export WEB_VISION_ENABLED=false
export WEB_AGENT_ENABLED=false

./extensions/web_tools/web-tools start
./extensions/web_tools/web-tools check
```

`setup` creates the ignored `extensions/web_tools/.env`, generates an extension
Compose project name and search secret, and preserves existing settings. `start`
builds the required image and starts three extension services: SearXNG, the crawler
worker and its egress proxy. The first build downloads dependencies and Chromium;
later builds reuse Docker's cache. `check` should return `status: ok`.

The exports above apply only to this shell. For persistent CLI settings, edit
the existing entries in `extensions/web_tools/.env`; do not create duplicate keys.
Run `start` again after changing service capabilities or limits. Exported process
values override `.env`, so update/unset them if changing the file has no effect.

| Boundary | How it stays separate from the baseline |
| --- | --- |
| Dependencies | Crawling, browser, file parsers and OCR are installed in the extension Docker images |
| Configuration | Extension-specific `.env` and `WEB_*` settings; no automatic loading by the baseline |
| Lifecycle | Separate Compose project; explicit `start` and `stop` |
| Import | `get_tools()` starts no service; with the master flag off, it returns `{}` before loading extension modules |
| Data | Tool results are returned to the caller; no automatic lakehouse writes or indexing |

Default local service addresses are `http://127.0.0.1:8888` (search) and
`http://127.0.0.1:8932` (crawler gateway). If those ports are occupied, change
`WEB_SEARCH_PORT` / `WEB_CRAWLER_PORT` in the extension configuration and restart.
Python callers must then use matching `WEB_SEARXNG_URL` / `WEB_CRAWLER_URL` values.
These local listeners are not a remotely deployed or authenticated team API.

## 3. Verify search and URL reading

```bash
./extensions/web_tools/web-tools search \
  'BDDK bankacılık sektörü raporu' --language tr-TR --max-results 3

./extensions/web_tools/web-tools url \
  'https://www.bddk.org.tr/Veri/Detay/162' --max-chars 4000

./extensions/web_tools/web-tools url \
  'https://www.rfc-editor.org/rfc/rfc9110.txt' --max-chars 2000
```

Search returns candidate URLs and snippets. `url` reads actual evidence and
automatically chooses the supported reader. Use plain URL strings, not Markdown
links such as `[label](https://...)`. Inspect `status`, `error`, `warnings`,
`truncated` and `processing_errors`; a bounded read may legitimately be partial.

To save output without relying on a variable from another terminal:

```bash
umask 077
export WEB_RESULTS_DIR="$(mktemp -d "${TMPDIR:-/tmp}/web-tools.XXXXXX")"
./extensions/web_tools/web-tools url \
  'https://www.bddk.org.tr/Veri/Detay/162' --max-chars 4000 \
  > "$WEB_RESULTS_DIR/page.json"
python3 -m json.tool "$WEB_RESULTS_DIR/page.json"
```

## 4. Connect a Python application

Use your existing application environment. This example runs from the repository
root; another working directory needs the project already importable, as with
the baseline's existing editable installation.

```python
from backend.tools import get_tools

web_config = {
    "WEB_TOOLS_ENABLED": "true",
    "WEB_DOCUMENTS_ENABLED": "true",
    "WEB_LINKS_ENABLED": "true",
    "WEB_SEARXNG_URL": "http://127.0.0.1:8888",
    "WEB_CRAWLER_URL": "http://127.0.0.1:8932",
}
tools = get_tools(web_config)  # Keep this mapping for subsequent calls.

search = tools["search_web"](
    "BDDK bankacılık sektörü raporu", max_results=3, language="tr-TR"
)
page = tools["read_web_url"](
    "https://www.bddk.org.tr/Veri/Detay/162", max_chars=4000, ocr=False
)

if page.get("error"):
    print("Read failed:", page["error"]["code"])
elif page.get("content"):
    print(page["content"])
    print("Source:", page["final_url"], "Fetched:", page.get("fetched_at"))
    print("Warnings:", page.get("warnings", []))
else:
    print("No readable content was returned.")
```

**CLI versus Python configuration:** the CLI loads `.env.example`, then its private
`.env`, then process variables. `get_tools()` and `research()` read only an explicit
mapping or the process environment. They never automatically load the extension
`.env`. Caller and worker must both allow the requested capability.

The callables are synchronous and return JSON-compatible dictionaries. In an
async application, execute them through your application's bounded thread/executor
facility. The host wrapper uses the standard library and does not import the
Docker parser/browser packages.

Choose one integration approach:

| Application owns | Use | Application still handles |
| --- | --- | --- |
| Its own planner and model loop | Register `search_web`, `read_web_url` and optionally link/file/image tools from `get_tools()` | Tool selection, dispatch, total budgets, synthesis and citations |
| A focused question needing web research | Call `research()` | Deciding when to research, conversational state, and combining results with analytics |

See the validated dispatch example in [AI_USAGE.md](AI_USAGE.md). For the team's
existing `Agent(client, url_reader=..., web_search=...)`, use
`get_team_tools(web_config)` from `backend.extensions.web_tools.team_adapter` as
documented in [TEAM_INTEGRATION.md](TEAM_INTEGRATION.md). That agent branch must
be integrated separately; importing the adapter does not merge or start it.

## 5. Enable only the additional capabilities you need

| Capability | Configuration beyond the master flag | Model key needed? |
| --- | --- | --- |
| PDF, Excel/CSV, DOCX, text | `WEB_DOCUMENTS_ENABLED=true` | No |
| Discover attachment/image URLs | `WEB_LINKS_ENABLED=true` | No |
| Image metadata | `WEB_IMAGES_ENABLED=true` | No |
| Local OCR for scans/images | Documents/images as applicable; `WEB_OCR_ENABLED=true`, `WEB_OCR_PROVIDER=local` | No |
| MIA vision for image/PDF interpretation | Images/documents as applicable; `WEB_VISION_ENABLED=true`; opt in on each read | Yes |
| MIA OCR | OCR enabled with `WEB_OCR_PROVIDER=kloudeks` | Yes |
| Question → multi-source research → cited answer | `WEB_AGENT_ENABLED=true` plus the content capabilities needed | Yes |

For MIA features, place `WEB_KLOUDEKS_API_KEY` in the private extension `.env`
and restart. Keep it out of code, prompts, logs and commits. Model calls stay
behind the Kloudeks client abstraction. See [ASSETS.md](ASSETS.md) for exact
model settings, supported formats and per-read limits.

For example, after configuring the worker key:

```bash
export WEB_AGENT_ENABLED=true
./extensions/web_tools/web-tools start
./extensions/web_tools/web-tools ask \
  'Compare the scope of RFC 9110 and RFC 9111, citing both documents.' \
  --url 'https://www.rfc-editor.org/rfc/rfc9110.txt' \
  --url 'https://www.rfc-editor.org/rfc/rfc9111.txt' \
  --require 'Scope of RFC 9110' --require 'Scope of RFC 9111' \
  --min-sources 2 --max-tool-calls 4
```

`research()` returns `answer`, `citations`, retained `evidence`, `coverage`,
`conflicts`, `missing_information`, `usage` and `stop_reason`. Display limitations
alongside the answer. The default minimum is one document; several pages of the
same file do not satisfy a two-document requirement. Read [MULTISOURCE.md](MULTISOURCE.md)
for the Python equivalent, output fields and question-level limits.

## 6. Stop, troubleshoot and check readiness

```bash
./extensions/web_tools/web-tools stop
```

This stops the extension services. The baseline keeps its original startup.
Setting `WEB_TOOLS_ENABLED=false` disables tool registration; it does not stop
already-running containers. Optional capabilities can be disabled individually.

| Symptom | Action |
| --- | --- |
| Docker permission denied or server missing | Follow the Docker prerequisite section; verify `docker version` as this user |
| `feature_disabled` | Check caller flags and worker flags; restart after changes |
| CLI works but `get_tools()` is empty | Pass a Python config mapping or export `WEB_TOOLS_ENABLED=true` in the app process |
| Service unavailable | Start the extension and check service addresses/ports |
| Old worker rejects new research options | Rebuild/restart with this checkout's `web-tools start` |
| `partial` / `truncated` | Inspect warnings and scope; fetch only the additional evidence needed |
| `model_access_denied` | Check the private MIA key and model access without printing the key |
| `model_limit` or another budget stop | Review `usage`, `limits` and `stop_reason`; keep retained evidence, avoid automatic retry loops |

Before team handoff, publish all required code/docs on the agreed branch, run
`web-tools test`, run `asset-test` and `browser-test` with the matching Docker
capabilities, and exercise the live research example if delivering that feature.
The latest recorded local run passed 96 extension tests and 57 baseline tests;
15 Docker cases and the enhanced live MIA flow still need rerunning. See
[VERIFICATION.md](VERIFICATION.md) for dated evidence and current open checks.

## Short team explanation / Kısa ekip açıklaması

**English:** The web tools are an optional Docker extension that keeps the
baseline Python environment and startup separate. Developers start the services
and register the Python callables with their agent. Search, URL reading, OCR,
vision and multi-source research have separate settings and resource limits.
Publishing the latest changes and live Docker/MIA verification remain pending.

**Türkçe:** Web araçları, mevcut Python ortamını ve başlangıç akışını değiştirmeyen,
isteğe bağlı bir Docker eklentisidir. Geliştiriciler servisleri başlatıp Python
fonksiyonlarını kendi ajanlarına bağlayabilir. Arama, URL okuma, OCR, görsel
yorumlama ve çok kaynaklı araştırma ayrı ayarlar ve kaynak limitleriyle çalışır.
Son değişikliklerin paylaşılması ve canlı Docker/MIA doğrulaması henüz tamamlanmadı.
