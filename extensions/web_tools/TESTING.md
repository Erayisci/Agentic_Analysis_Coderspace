# Test the web tools yourself

This walkthrough starts with the existing checkout, exercises each capability, and
saves results you can inspect. You need Docker with working WSL integration,
Python 3, and internet access for builds and public sources. You do not need the
baseline `.venv`, an LLM, or an API key for steps 1–8. Step 9 is optional MIA usage.

For a fresh machine/clone, use [README.md](README.md#recommended-wsl2-setup) first.
Keep this guide open and run the numbered steps in **one Ubuntu/WSL Bash terminal**.
The commands change this terminal's settings; they do not rewrite your `.env`.

## 1. Check Docker

In your existing checkout:

```bash
cd ~/Agentic_Analysis_Coderspace
git status --short
git branch --show-current
docker version
docker compose version
```

Use your actual checkout path if it differs. Preserve existing changes; these tests
do not require switching branches. The code must include `extensions/web_tools/web-tools`.

Expected: `docker version` shows **Client and Server**, and Compose prints a version.
For Docker Desktop, start it on Windows and enable this Ubuntu distribution under
**Settings → Resources → WSL Integration**. Docker's [WSL guide](https://docs.docker.com/desktop/features/wsl/)
explains the integration; do not install a second engine for this walkthrough.

If you see socket `permission denied` and your account is already a Docker group
member, run `newgrp docker`, then repeat the two Docker checks in the resulting
shell. This shell must also be used for the exports below. See Docker's
[group setup instructions](https://docs.docker.com/engine/install/linux-postinstall/)
if membership has not been configured. If the Server is missing, resolve Docker
access before proceeding.

## 2. Enable local capabilities and start services

```bash
./extensions/web_tools/web-tools setup

export WEB_TOOLS_ENABLED=true
export WEB_LINKS_ENABLED=true
export WEB_DOCUMENTS_ENABLED=true
export WEB_IMAGES_ENABLED=true
export WEB_OCR_ENABLED=true
export WEB_OCR_PROVIDER=local
export WEB_OCR_LANGUAGES=eng+tur
export WEB_VISION_ENABLED=false
export WEB_ASSET_CACHE_ENABLED=true

# Small, explicit budgets for this walkthrough.
export WEB_ASSET_MAX_PAGES=3
export WEB_ASSET_MAX_CHARS=12000
export WEB_OCR_MAX_PAGES=3

./extensions/web_tools/web-tools start
./extensions/web_tools/web-tools check

WEB_RESULTS_DIR=$(mktemp -d /tmp/web-tools-test.XXXXXX)
export WEB_RESULTS_DIR
echo "Results will be saved in: $WEB_RESULTS_DIR"
```

Expected: all three Docker services become healthy. `check` returns `status: ok`;
crawler capabilities show documents/images/links/OCR enabled and vision disabled.
The first build can take several minutes. Dependencies are installed in Docker.

`setup` preserves existing configuration and appends missing defaults. `start`
applies flags and limits to the worker. If a later command reports `feature_disabled`,
confirm you exported the setting in this shell and reran `start`.

The output directory is temporary Linux storage, outside Git. Every new use of
`mktemp` creates a separate directory. Keep its printed path if you open another
terminal. Plain `get_tools()` in Python reads process environment; the CLI also
loads `extensions/web_tools/.env`. See [configuration precedence](ASSETS.md#2-enable-local-extraction-and-build).

## 3. Run reproducible tests

```bash
./extensions/web_tools/web-tools test
./extensions/web_tools/web-tools browser-test
./extensions/web_tools/web-tools asset-test
```

Expected on the implementation verified on 2026-09-15:

| Command | Expected result | What it exercises |
| --- | --- | --- |
| `test` | `OK (skipped=14)`; 61 passed, 75 discovered | Host contracts, disabled flags, URL rules, limits, model payload fixtures, configuration |
| `browser-test` | 5 tests, `OK` | Real Chromium, JavaScript, redirects, private destinations, and certificate rejection |
| `asset-test` | 9 tests, `OK` | Real PDF/XLSX/XLS/CSV/DOCX/image parsers, local scan OCR, limits, cache, and model batching fixtures |

The 14 skipped cases in `test` are the 5 browser and 9 asset cases you run separately.
Counts may grow with later changes; failures/errors are the important signals.
These fixture tests do not contact public websites or spend MIA quota. The image
and scanned-PDF tests assert that local OCR reads `BANK REPORT` and `12345` from
generated pixels. Passing `asset-test` proves OCR worked even if a public image
you later select contains no readable text.

## 4. Search and read an HTML page

```bash
./extensions/web_tools/web-tools search \
  'BDDK bankacılık sektörü raporu' --language tr-TR --max-results 5 \
  > "$WEB_RESULTS_DIR/search.json"

./extensions/web_tools/web-tools read \
  'https://www.bddk.org.tr/Veri/Detay/162' --max-chars 6000 \
  > "$WEB_RESULTS_DIR/page.json"

python3 -m json.tool "$WEB_RESULTS_DIR/search.json"
python3 - <<'PY'
import json, os
from pathlib import Path
page = json.loads((Path(os.environ['WEB_RESULTS_DIR']) / 'page.json').read_text())
print('Status:', page['status'], 'Error:', page.get('error'))
print('Source:', page.get('final_url'))
print(page.get('content', ''))
PY
```

Expected: search results contain `title`, `url`, and `snippet`; the reader returns
Markdown in `content`. A search can be `partial` when an engine is unavailable.
Inspect its results and `unavailable_engines` instead of assuming the whole search
failed. `--max-results 5` is a ceiling, not a promise of five hits. For HTML,
`truncated=true` can accompany `status: ok`; always inspect both fields.

Use only the plain URL in commands. Copy `'https://…'`, not `'[https://…](https://…)'`.

## 5. Find report and image links

```bash
./extensions/web_tools/web-tools assets \
  'https://www.bddk.org.tr/Veri/Detay/162' \
  > "$WEB_RESULTS_DIR/assets.json"

python3 - <<'PY'
import json, os
from pathlib import Path
result = json.loads((Path(os.environ['WEB_RESULTS_DIR']) / 'assets.json').read_text())
print('Status:', result['status'], 'Error:', result.get('error'))
for kind in ('links', 'images'):
    print('\n' + kind, 'truncated:', result.get(kind + '_truncated'))
    for item in result.get(kind) or []:
        print(item['type_hint'], item['text'].strip(), item['url'])
PY
```

Expected: file links are listed first, followed by other links within the limit.
The BDDK page returned attachment URLs such as `/Veri/EkGetir/8?ekId=625`; a PDF URL
does not need a `.pdf` suffix. `partial` plus `links_truncated=true` means discovery
hit its result limit. No linked file, image pixels, OCR, or model has been fetched
by discovery itself.

## 6. Read report pages and inspect the extracted evidence

The URL below was listed on that page during verification. It is an example
report, not a guarantee that it is the latest publication. If it changes, choose
a current attachment from step 5 and replace only the value of `WEB_REPORT_URL`.

```bash
export WEB_REPORT_URL='https://www.bddk.org.tr/Veri/EkGetir/8?ekId=625'

./extensions/web_tools/web-tools document "$WEB_REPORT_URL" \
  --start-page 5 --max-pages 2 --max-chars 8000 --no-ocr --refresh \
  > "$WEB_RESULTS_DIR/report.json"

python3 - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ['WEB_RESULTS_DIR'])
result = json.loads((root / 'report.json').read_text())
print('Status:', result['status'], 'Error:', result.get('error'))
print('Source:', result.get('final_url'))
print('Processed pages:', result.get('pages_processed'), '/', result.get('total_pages'))
print('Characters:', result.get('returned_chars'), 'Truncated:', result.get('truncated'))
print(result.get('content', ''))
for section in result.get('sections', []):
    print('Section:', section['location'], 'Method:', section['method'])
    if 'rows' in section:
        print('Table rows:', section['rows'])
(root / 'report.md').write_text(result.get('content', ''), encoding='utf-8')
PY
```

Open `report.md` in VS Code for readable text; keep `report.json` as the evidence
record with source URL, fetch time, page locations, tables, and limit warnings.
During verification this file had 47 pages; pages 5–6 returned 6,123 characters
and two detected tables. A changed upstream report can produce different numbers.
`partial` is expected because you requested only two pages, not the whole report.

PDF page selection limits extraction, but still downloads the whole file within
the byte limit. `document` also accepts public XLSX, XLS, CSV, and DOCX URLs. Page
selection applies only to PDFs; sheet/row/column limits govern spreadsheets.
The tools do not automatically import this evidence into the lakehouse.

## 7. Confirm caching

Repeat the same request without `--refresh`:

```bash
./extensions/web_tools/web-tools document "$WEB_REPORT_URL" \
  --start-page 5 --max-pages 2 --max-chars 8000 --no-ocr \
  > "$WEB_RESULTS_DIR/report-cached.json"

python3 - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ['WEB_RESULTS_DIR'])
first = json.loads((root / 'report.json').read_text())
second = json.loads((root / 'report-cached.json').read_text())
print('Cache:', second.get('cache'))
print('Same fetch timestamp:', first.get('fetched_at') == second.get('fetched_at'))
assert second.get('cache', {}).get('hit'), second.get('error') or second.get('warnings')
assert first['content'] == second['content']
PY
```

Expected: `cache.hit` is true while the entry is within its TTL and no setting or
request parameter changed. `--refresh` fetches again and replaces that entry.
MIA OCR/vision requests bypass this result cache. Local OCR results can be cached.

## 8. Try an image or scan without a model

The reproducible scan test already ran in step 3. For a public image of your choice,
copy a PNG/JPEG/WebP/TIFF URL from the image list in step 5:

```bash
read -rp 'Paste a public image URL from the list: ' WEB_IMAGE_URL
./extensions/web_tools/web-tools image "$WEB_IMAGE_URL" --ocr \
  > "$WEB_RESULTS_DIR/image-ocr.json"
python3 -m json.tool "$WEB_RESULTS_DIR/image-ocr.json"
```

Expected for readable text: `sections` with `method: local_ocr`, `model_calls: 0`,
and extracted text. A logo/photo may return `empty`; that is not a chart explanation.
Use `--no-ocr` for metadata only. For a scanned public PDF, use `document` with its
URL, `--ocr`, and a small `--max-pages` value. Native-text PDF pages normally skip OCR.
This interface accepts public URLs; local paths and localhost URLs are not supported
inputs. The dedicated fixture tests run their own controlled local harness.

## 9. Optional: ask MIA to interpret a chart

Skip this step if you do not have a MIA key. This is a **real model call** using
your account quota. Configure the key locally; the input below is hidden and
does not place the key in shell history:

```bash
read -rsp 'MIA API key: ' WEB_KLOUDEKS_API_KEY
echo
export WEB_KLOUDEKS_API_KEY
export WEB_KLOUDEKS_BASE_URL='https://mia.csp.kloudeks.com/v1'
export WEB_KLOUDEKS_VISION_MODEL='kkbhackathon2026/Qwen3.8-27B'
export WEB_VISION_ENABLED=true
export WEB_MODEL_MAX_CALLS_PER_READ=1
export WEB_MODEL_MAX_CALLS_PER_HOUR=5
export WEB_MODEL_MAX_TOKENS=2048
./extensions/web_tools/web-tools start

./extensions/web_tools/web-tools document "$WEB_REPORT_URL" \
  --start-page 5 --max-pages 1 --max-chars 12000 --no-ocr --vision \
  --question 'Explain the chart in Turkish. Preserve units and labels. Mark unreadable values.' \
  > "$WEB_RESULTS_DIR/report-vision.json"
python3 -m json.tool "$WEB_RESULTS_DIR/report-vision.json"
```

Expected if MIA is configured and available: `model_calls: 1` and a section with
`method: mia_vision`, alongside any native PDF text/tables. Compare the interpretation
with the original page. Model output can misread figures; do not treat it as
validated financial data. `partial` can indicate page or output limits.

For MIA OCR, use `WEB_OCR_PROVIDER=kloudeks`,
`WEB_KLOUDEKS_OCR_MODEL=kkbhackathon2026/Unlimited-OCR`, rerun `start`, then read a
scan with `--ocr` and without `--vision`. See the [MIA configuration details](ASSETS.md#5-enable-kloudeks-mia-vision-or-ocr).
Set `WEB_OCR_PROVIDER=local` and restart to return to Tesseract.

No real MIA key was provided during implementation, so live model behavior was
not verified then. Payload, image/token limits, and error handling were tested
with fixtures. `model_not_configured` means the service needs the key and a
restart; `model_unavailable` requires checking credentials, model access, or the
endpoint. `model_rate_limited` means MIA returned 429.

## 10. Demonstrate resource limits and the off switch

Lower the server policy, then request more than it permits:

```bash
export WEB_ASSET_MAX_PAGES=1
export WEB_ASSET_MAX_CHARS=1000
./extensions/web_tools/web-tools start

./extensions/web_tools/web-tools document "$WEB_REPORT_URL" \
  --start-page 5 --max-pages 50 --max-chars 50000 --no-ocr \
  > "$WEB_RESULTS_DIR/limited.json"

python3 - <<'PY'
import json, os
from pathlib import Path
r = json.loads((Path(os.environ['WEB_RESULTS_DIR']) / 'limited.json').read_text())
assert r.get('error') is None, r.get('error')
assert r['pages_processed'] <= 1
assert r['returned_chars'] <= 1000
print(r['status'], r['pages_processed'], r['returned_chars'], r['warnings'])
PY
```

Expected: at most one processed page and 1,000 characters, with visible limits.
Restore this walkthrough's ceilings using `WEB_ASSET_MAX_PAGES=3` and
`WEB_ASSET_MAX_CHARS=12000`, followed by `start`, when you want to continue reading.

To keep search/HTML but switch off the additional capabilities:

```bash
export WEB_DOCUMENTS_ENABLED=false
export WEB_IMAGES_ENABLED=false
export WEB_LINKS_ENABLED=false
export WEB_OCR_ENABLED=false
export WEB_VISION_ENABLED=false
export WEB_ASSET_CACHE_ENABLED=false
./extensions/web_tools/web-tools start
./extensions/web_tools/web-tools check
./extensions/web_tools/web-tools document "$WEB_REPORT_URL"
```

Expected: capabilities are false, and the last command reports `feature_disabled`
with a nonzero exit code. It should not extract the report. An individual switch
can be turned off the same way without disabling the others. Changes to flags,
limits, model provider, or credentials need a service restart through `start`.

## 11. Stop and keep the evidence

```bash
export WEB_TOOLS_ENABLED=false
./extensions/web_tools/web-tools stop
echo "Saved results: $WEB_RESULTS_DIR"
```

`stop` stops this extension's services and preserves its cache volume. It does
not stop unrelated Docker projects or delete baseline data. Exports last only in
the current shell; edit existing entries in the ignored extension `.env` if you
want these settings in future CLI sessions. Process exports take precedence.

For routine diagnosis use `web-tools check`, `web-tools config` (key redacted),
and `web-tools logs`. CLI exit codes are 0 for a returned non-error result
(including `partial` and `empty`), 1 for a tool error, and 2 for command/configuration
errors; lifecycle/test commands propagate their subprocess exit code. A zero exit
code alone does not prove that enough evidence was extracted.

To understand how this evidence reaches an AI answer, continue with
[AI_USAGE.md](AI_USAGE.md). The tool commands currently return evidence JSON;
automatic orchestration into an answer is a separate, not-yet-implemented agent layer.
