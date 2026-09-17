# Files, images, OCR, and vision

For a complete self-test with expected output and saved results, start with
[TESTING.md](TESTING.md). For tool contracts and AI integration, use
[AI_USAGE.md](AI_USAGE.md). This guide is the detailed configuration reference.

These capabilities extend the existing SearXNG/Crawl4AI tools. Every new capability
defaults to **off**. PDF, spreadsheet, DOCX, image, and OCR dependencies are installed
only in the optional Docker image; the baseline Python environment and lakehouse
commands do not change. Search, HTML reading, file extraction, and local OCR need
no model API key. MIA vision and MIA OCR require your Kloudeks credentials and quota.
Plain TXT, Markdown and JSON documents are also supported under the document
switch. Their declared charset or Unicode BOM is honored; otherwise UTF-8 is used.
Undecodable text fails explicitly. `url` automatically routes supported formats;
see [DEMO.md](DEMO.md) for the optional `ask` research runner and its limits.

## 1. Prepare Docker and this checkout

For a fresh clone, follow the [onboarding guide](README.md) first. In an existing
checkout, keep using its WSL terminal and run from the repository root:

```bash
docker version
docker compose version
./extensions/web_tools/web-tools setup
```

`docker version` must show both Client and Server. For Docker Desktop, start it on
Windows and enable integration for this Ubuntu distribution. If the socket reports
permission denied and your account already belongs to the Docker group, run
`newgrp docker` and retry in that shell. The onboarding guide covers first-time
installation and group setup. Installing Ubuntu alone does not install a working
Docker daemon.

`setup` preserves your existing ignored `extensions/web_tools/.env`, project name,
secret, and settings; it appends missing settings with their default values.

## 2. Enable local extraction and build

Run these exports in the same terminal used for subsequent commands:

```bash
export WEB_TOOLS_ENABLED=true
export WEB_LINKS_ENABLED=true
export WEB_DOCUMENTS_ENABLED=true
export WEB_IMAGES_ENABLED=true
export WEB_OCR_ENABLED=true
export WEB_OCR_PROVIDER=local
export WEB_VISION_ENABLED=false
export WEB_ASSET_CACHE_ENABLED=true

./extensions/web_tools/web-tools start
./extensions/web_tools/web-tools check
```

The first build downloads the locked parser packages and installs Tesseract with
English and Turkish language data. Later code-only builds reuse those layers.
The CLI selects `compose.assets.yaml` when documents or images are enabled; the
same three extension services and localhost ports are used. `check` should report
documents, images, links, and OCR as enabled.

For persistent settings, edit the existing entries in `extensions/web_tools/.env`
instead of exporting them. Do not append duplicate keys. Process variables override
that file, so clear or update old exports when changing configuration. Run `start`
after changing flags, limits, providers, or credentials: the worker enforces the
settings it received at startup, even if a caller asks for a higher limit.

## 3. Discover and read a report

```bash
./extensions/web_tools/web-tools search \
  'BDDK bankacılık sektörü raporu' --language tr-TR --max-results 5

./extensions/web_tools/web-tools assets \
  'https://www.bddk.org.tr/Veri/Detay/162' > /tmp/bddk-assets.json

python3 - <<'PY'
import json
result = json.load(open('/tmp/bddk-assets.json'))
print('Status:', result['status'])
for link in result.get('links') or []:
    print(link['type_hint'], link['text'].strip(), link['url'])
PY
```

Discovery returns bounded `links` and `images` lists. It prioritizes recognizable
file/download links before navigation links. It does **not** download each linked
file or call OCR/vision. `type_hint` is only a hint; a URL can serve a PDF without
ending in `.pdf`. The document reader checks the downloaded bytes. Check
`links_truncated` and `images_truncated` for discovery limits; limited discovery
returns `status: partial`.

Choose a returned report URL. For example, this BDDK attachment was listed during
development (public websites can change):

```bash
./extensions/web_tools/web-tools document \
  'https://www.bddk.org.tr/Veri/EkGetir/8?ekId=625' \
  --start-page 1 --max-pages 2 --max-chars 8000 --no-ocr \
  > /tmp/bddk-report.json

python3 - <<'PY'
import json
result = json.load(open('/tmp/bddk-report.json'))
print('Status:', result['status'], 'Error:', result.get('error'))
print('Source:', result.get('final_url'))
print('Pages:', result.get('pages_processed'), '/', result.get('total_pages'))
print(result.get('content', ''))
for section in result.get('sections', []):
    if 'rows' in section:
        print('Table at', section['location'], section['rows'])
PY
```

Use plain URLs in shell commands, such as `'https://example.org/report.pdf'`.
Do not copy Markdown syntax such as `'[https://...](https://...)'`.
`read` continues to accept HTML pages; `document` accepts PDF, XLSX, XLS, CSV, and
DOCX. It does not automatically follow every document link on an HTML page.

For a later PDF section, repeat with `--start-page 3 --max-pages 2`. A page range
still downloads the bounded whole PDF, but only extracts the selected pages.
For XLSX/XLS/CSV/DOCX, use `document 'THE_RETURNED_FILE_URL'`; page options apply
only to PDFs. Spreadsheet limits are configured below.

## 4. Read a scan or image

Select a public PNG/JPEG/WebP/TIFF URL from the `images` list:

```bash
./extensions/web_tools/web-tools image 'THE_RETURNED_IMAGE_URL' --ocr
./extensions/web_tools/web-tools document 'THE_SCANNED_PDF_URL' --ocr --max-pages 3
```

Replace these uppercase placeholders with actual URLs. With the settings in step
2, OCR runs locally through Tesseract. PDFs use OCR for pages without extractable
text. `--no-ocr` disables OCR for that call. An image read without OCR or vision
returns dimensions/format and an explicit metadata-only warning; it cannot explain
a chart. Multi-frame images process only their first frame and report that limit.

For a reproducible test without finding a public scan:

```bash
./extensions/web_tools/web-tools test
./extensions/web_tools/web-tools asset-test
./extensions/web_tools/web-tools browser-test
```

`asset-test` runs real parsers and local OCR inside Docker with generated PDF,
spreadsheet, DOCX, and image fixtures. It tests text/table extraction, page/row/
pixel limits, caching, and model budgets. MIA responses are fixtures, so it does
not spend model quota. These tests do not require a publicly hosted test file.

## 5. Enable Kloudeks MIA vision or OCR

The implementation uses the endpoint/model IDs from the supplied MIA hackathon
guide, through `backend.model_clients.kloudeks.KloudeksClient`:

| Purpose | Exact setting |
| --- | --- |
| Base URL | `WEB_KLOUDEKS_BASE_URL=https://mia.csp.kloudeks.com/v1` |
| Vision | `WEB_KLOUDEKS_VISION_MODEL=kkbhackathon2026/Qwen3.8-27B` |
| OCR | `WEB_KLOUDEKS_OCR_MODEL=kkbhackathon2026/Unlimited-OCR` |

Enter your key locally without putting it in shell history or chat:

```bash
read -rsp 'MIA API key: ' WEB_KLOUDEKS_API_KEY
echo
export WEB_KLOUDEKS_API_KEY
export WEB_VISION_ENABLED=true
export WEB_MODEL_MAX_CALLS_PER_READ=1
export WEB_MODEL_MAX_CALLS_PER_HOUR=20
export WEB_MODEL_MAX_TOKENS=2048
./extensions/web_tools/web-tools start

./extensions/web_tools/web-tools image 'THE_CHART_IMAGE_URL' --no-ocr --vision \
  --question 'Explain this chart in Turkish. Preserve the labels and units; mark unreadable values.'
```

You can also request vision on selected PDF pages:

```bash
./extensions/web_tools/web-tools document 'THE_REPORT_PDF_URL' \
  --start-page 5 --max-pages 2 --no-ocr --vision \
  --question 'Describe the charts on these pages and identify their units.'
```

`WEB_VISION_ENABLED=true` permits vision; each call must also request `--vision`.
Turning on this flag alone does not send images to MIA. `MIA_API_KEY` is accepted
as a fallback when `WEB_KLOUDEKS_API_KEY` is empty. The key can instead be stored
in the ignored extension `.env`. `web-tools config` redacts it. Docker administrators
can access container environment variables; do not share raw `docker inspect` or
`docker compose config` output containing configured credentials.

To use MIA OCR instead of Tesseract:

```bash
export WEB_OCR_ENABLED=true
export WEB_OCR_PROVIDER=kloudeks
./extensions/web_tools/web-tools start
./extensions/web_tools/web-tools document 'THE_SCANNED_PDF_URL' --ocr --max-pages 3
```

The client sends base64 PNG blocks to `/v1/chat/completions`, never external image
URLs. OCR preserves the supplied `<image>\ndocument parsing` prompt,
`skip_special_tokens=false`, and `vllm_xargs` settings: `ngram_size=35`,
`window_size=128` for one image or `1024` for multiple images. OCR batches up to
3 scanned pages per model call; vision sends at most 5 images per call, further
restricted by the configured image limit. There are no automatic model retries.

OCR and vision share the per-read call budget. Using both MIA OCR and vision on
one file usually needs `WEB_MODEL_MAX_CALLS_PER_READ=2`; otherwise the result
reports that the budget prevented the later operation. Local OCR uses no model
calls. No embedding/vector-index feature is introduced here.

## Limits and disabling capabilities

Every setting below is in `.env.example`. Values outside the allowed ranges fail
configuration validation. Request-level limits can lower these ceilings, never
raise them. Values are per file/read unless stated otherwise.

| Setting | Default | Allowed range / effect |
| --- | --- | --- |
| `WEB_ASSET_MAX_BYTES` | `10485760` (10 MiB) | 1 KiB–50 MiB downloaded bytes |
| `WEB_ASSET_TIMEOUT_SECONDS` | `120` | 5–300 seconds for download, parsing, OCR, and model work together |
| `WEB_ASSET_MAX_CHARS` | `20000` | 100–50,000 returned content characters |
| `WEB_ASSET_MAX_PAGES` | `10` | 1–50 PDF pages |
| `WEB_ASSET_MAX_SHEETS` | `3` | 1–20 sheets |
| `WEB_ASSET_MAX_ROWS` | `200` | 1–2,000 rows per sheet/table |
| `WEB_ASSET_MAX_COLUMNS` | `50` | 1–200 columns per sheet/table |
| `WEB_ASSET_MAX_ARCHIVE_BYTES` | `52428800` | 1 KiB–100 MiB uncompressed ZIP members |
| `WEB_ASSET_MAX_IMAGE_PIXELS` | `10000000` | 10,000–40,000,000 pixels; oversized source images rejected, PDF renders scaled to fit |
| `WEB_ASSET_IMAGE_EDGE` | `1600` | 256–3,000 pixels on the longest prepared image edge |
| `WEB_ASSET_MAX_LINKS` | `50` | 1–200 returned links and separately images; inspect at most 1,000 DOM candidates each |
| `WEB_ASSET_MAX_IMAGES` | `3` | 1–5 PDF page images for vision |
| `WEB_OCR_MAX_PAGES` | `3` | 1–20 scanned pages for OCR |
| `WEB_OCR_LANGUAGES` | `eng+tur` | Local OCR: `eng`, `tur`, `eng+tur`, or `tur+eng` |
| `WEB_MODEL_MAX_CALLS_PER_READ` | `1` | 0–5; zero prevents model calls |
| `WEB_MODEL_MAX_CALLS_PER_HOUR` | `20` | 0–1,000 attempts shared by this service, per UTC clock hour; zero prevents calls |
| `WEB_MODEL_MAX_TOKENS` | `2048` | 128–8,192 output tokens per model request |
| `WEB_ASSET_CACHE_TTL_SECONDS` | `3600` | 1–604,800 seconds |
| `WEB_ASSET_CACHE_MAX_BYTES` | `104857600` | 1 MiB–1 GiB of cached JSON, plus SQLite overhead |

Additional fixed limits include one simultaneous file extraction per service,
2,000 ZIP entries, ZIP expansion ratio 200, 512 characters per table cell, 4 MiB
per prepared PNG, and 2,000 characters in a vision question. Downloads use the
existing public-address proxy; redirects cannot bypass it. Its 32 MiB tunnel
limit and 60-second tunnel lifetime can be stricter than a raised file limit.
The parent kills the extraction process group at its deadline. Container memory
and process limits still apply.

To disable only model vision, or OCR entirely:

```bash
export WEB_VISION_ENABLED=false
export WEB_OCR_ENABLED=false
./extensions/web_tools/web-tools start
```

To keep OCR but make it local again, use `WEB_OCR_ENABLED=true` and
`WEB_OCR_PROVIDER=local`, then run `start`. To return to HTML/search services only:

```bash
export WEB_DOCUMENTS_ENABLED=false
export WEB_IMAGES_ENABLED=false
export WEB_LINKS_ENABLED=false
export WEB_OCR_ENABLED=false
export WEB_VISION_ENABLED=false
export WEB_ASSET_CACHE_ENABLED=false
export WEB_AGENT_ENABLED=false
./extensions/web_tools/web-tools start
```

This selects the original crawler image without the additional file/OCR packages
or asset volume mount. Existing cache data remains in the extension's Docker
volume. `WEB_ASSET_CACHE_ENABLED=false` prevents cache reads/writes; it does not
erase saved content or reset model counters. Set `WEB_TOOLS_ENABLED=false` to
remove all tools from host registration. Run `web-tools stop` to stop this
extension's services as well. Persist desired flags in `.env` for new terminals.

## Results, caching, and AI use

Successful results contain `content` plus `sections` with a `location`, `method`,
and text; detected tables also include `rows` when they fit the output budget.
Methods distinguish plain text, HTML, PDF text, tables, DOCX text, local OCR, MIA OCR, and MIA vision.
Keep `final_url`, `fetched_at`, and the page/sheet/block location with any excerpt
passed to an agent so it can cite its evidence.

Check `status`, `error`, `processing_errors`, `warnings`, and `truncated` before reasoning. `partial`
means a limit omitted something or an optional model operation failed while usable
text was preserved. `processing_errors` distinguishes the latter. `empty` is
not an interpretation of an image. `original_chars` counts extracted sections
visited during this bounded read; it is not the length of the entire original
file. PDF table extraction is heuristic. Spreadsheet formulas/macros are never
executed; cached formula values can be missing or stale. Password-protected files,
old binary DOC files, presentations, and arbitrary archive extraction are unsupported.

With caching enabled, repeating the same request and policy can return the stored
text/table/local-OCR result without downloading or parsing again. Check `cache.hit`,
`cache.age_seconds`, and the original `fetched_at`. Use `--refresh` for a new fetch.
Requests using MIA OCR or vision bypass result caching. Model-call attempts are
counted persistently in the Docker volume before sending, including failed calls.
The hourly limit survives restarts and is shared across clients of this service;
it is not a global account quota across other installations.

From Python, enable the same capabilities in the service first, then:

```python
from backend.tools import get_tools

tools = get_tools({
    "WEB_TOOLS_ENABLED": "true",
    "WEB_LINKS_ENABLED": "true",
    "WEB_DOCUMENTS_ENABLED": "true",
    "WEB_IMAGES_ENABLED": "true",
    "WEB_CRAWLER_URL": "http://127.0.0.1:8932",
})
assets = tools["get_page_assets"]("https://www.bddk.org.tr/Veri/Detay/162")
report = tools["read_document"](
    "https://www.bddk.org.tr/Veri/EkGetir/8?ekId=625",
    max_pages=2, max_chars=8000, ocr=False,
)
```

`get_tools()` reads the supplied mapping or process environment, not `.env`.
It returns synchronous callables. The optional `ask` runner can search, discover,
select relevant files, read a small
page range, and request more pages or vision only when necessary. Feed only
relevant sections into its context, preserve citations, and perform calculations
with deterministic analytics tools. File/image content and model interpretations
remain untrusted evidence and must never become system instructions.

Raw table sections carry `validation.ready_for_calculation: false`. Use the
explicit schema/unit/period validation helper described in
[TEAM_INTEGRATION.md](TEAM_INTEGRATION.md) before calculations. It does not
automatically normalize or import data into the lakehouse.

## Troubleshooting

| Result | Action |
| --- | --- |
| `feature_disabled` | Enable the specific capability in the CLI and service, then rerun `start`. OCR and vision have independent switches. |
| `missing_dependency` | Start with documents/images enabled so the CLI builds the optional image. |
| `asset_too_large` | Lower the workload or deliberately raise the relevant server limit within its allowed range; PDF page selection does not reduce download bytes. |
| `timeout` / `busy` | Use fewer pages, lower OCR/vision work, or wait for the active extraction to finish. |
| `model_not_configured` | Set the key locally, then rerun `start`; never paste the key into chat. |
| `model_limit` | Review per-read and hourly budgets; a later UTC hour resets the hourly window. |
| `model_rate_limited` | MIA returned 429; wait or check the provider quota. |
| `model_access_denied` | MIA returned 401/403; check the private worker key and model access. |
| `model_timeout` / `model_output_limit` | Time or output budget was exhausted. Use preserved native text, a smaller request, or explicitly revise the budget. |
| `model_unavailable` | Check endpoint, exact model ID, credentials, and model access. Provider error bodies are not exposed. |
| `certificate_error` | The source certificate could not be verified. Missing intermediates can be recovered from a verified Chromium connection; roots and verification remain unchanged. |
| `parse_error` / `unsupported_content_type` | Check whether the URL returns a supported, unencrypted file rather than an HTML/login/error page. |

See [VERIFICATION.md](VERIFICATION.md) for actual executed checks and the live MIA
verification status.
