# Run the complete web pipeline

Use the existing checkout on `perhat`. Keep these commands in one Ubuntu/WSL
terminal. Start Docker Desktop and its Ubuntu WSL integration first. If Docker
reports socket permission denied and you already belong to the Docker group,
run `newgrp docker` **before** the exports below.

For installation from a fresh clone, start with [README.md](README.md).
The MIA key belongs in the ignored `extensions/web_tools/.env`, never in Git.

## 1. Enable bounded capabilities and start Docker

```bash
cd ~/Agentic_Analysis_Coderspace
docker version
docker compose version
./extensions/web_tools/web-tools setup

export WEB_TOOLS_ENABLED=true
export WEB_DOCUMENTS_ENABLED=true
export WEB_IMAGES_ENABLED=true
export WEB_LINKS_ENABLED=true
export WEB_OCR_ENABLED=true
export WEB_OCR_PROVIDER=local
export WEB_VISION_ENABLED=true
export WEB_AGENT_ENABLED=true
export WEB_ASSET_CACHE_ENABLED=true
export WEB_ASSET_MAX_PAGES=3
export WEB_ASSET_MAX_CHARS=12000
export WEB_MODEL_MAX_CALLS_PER_READ=1
export WEB_AGENT_MAX_TOOL_CALLS=6
export WEB_AGENT_MAX_MODEL_CALLS=6
export WEB_AGENT_MAX_CONTEXT_CHARS=20000
export WEB_MODEL_MAX_TOKENS=2048

export WEB_RESULTS_DIR="$(mktemp -d /tmp/web-tools-demo.XXXXXX)"
export WEB_REPORT_URL='https://www.bddk.org.tr/Veri/EkGetir/8?ekId=625'
echo "Saved results: $WEB_RESULTS_DIR"

./extensions/web_tools/web-tools start
./extensions/web_tools/web-tools check
```

Expected: healthy services and `agent`, `documents`, `images`, `links`, `ocr` and
`vision` enabled. Flags exported here do not rewrite `.env`. To keep them across
terminals, edit the corresponding existing entries in the ignored `.env` and
restart. Do not add duplicate keys. The hourly model-attempt budget persists in
the Docker volume; restarting does not reset it.

`ask` and `--vision` use your MIA quota. Search, HTML, native file reading and
local OCR do not. Do not repeat the key prompt if it is already configured.

## 2. Run deterministic tests

```bash
./extensions/web_tools/web-tools test
./extensions/web_tools/web-tools asset-test
./extensions/web_tools/web-tools browser-test
```

All must finish with `OK`. Host tests skip the Docker cases, which the next two
commands run. Tests use fixtures and consume no MIA quota. They cover real
PDF/Excel/CSV/DOCX parsers, scan OCR, JavaScript, limits, disabled features,
source citations, and preservation of native PDF text during a model failure.

## 3. Search, read the page, and find attachments

```bash
./extensions/web_tools/web-tools search \
  'BDDK bankacılık sektörü raporu' --language tr-TR --max-results 3 \
  > "$WEB_RESULTS_DIR/search.json"

./extensions/web_tools/web-tools url 'https://www.bddk.org.tr/Veri/Detay/162' \
  --max-chars 3000 > "$WEB_RESULTS_DIR/page.json"

./extensions/web_tools/web-tools assets 'https://www.bddk.org.tr/Veri/Detay/162' \
  > "$WEB_RESULTS_DIR/links.json"

python3 -m json.tool "$WEB_RESULTS_DIR/search.json"
python3 -m json.tool "$WEB_RESULTS_DIR/links.json"
```

Search returns candidate URLs. Page reading returns evidence. Discovery returns
links without downloading every linked file. `WEB_REPORT_URL` is a known test
report, not a promise that it is the latest; replace it with a relevant discovered
attachment if the publisher changes the site.

## 4. Read files through the same URL tool

```bash
./extensions/web_tools/web-tools url "$WEB_REPORT_URL" \
  --start-page 5 --max-pages 1 --max-chars 6000 --no-ocr \
  > "$WEB_RESULTS_DIR/pdf.json"

./extensions/web_tools/web-tools url 'https://www.rfc-editor.org/rfc/rfc9110.txt' \
  --max-chars 1500 --no-ocr > "$WEB_RESULTS_DIR/text.json"
```

The same `url` command handles HTML, PDF, XLSX/XLS, CSV, DOCX, TXT/Markdown/JSON,
and supported images. It checks responses/file signatures, so a PDF URL need
not end in `.pdf`. Existing `read` remains the HTML-only command; `document`
and `image` remain available for explicit requests.

To try a public Excel file of your own:

```bash
read -rp 'Public XLSX/XLS URL: ' WEB_EXCEL_URL
./extensions/web_tools/web-tools url "$WEB_EXCEL_URL" --max-chars 6000 --no-ocr \
  > "$WEB_RESULTS_DIR/excel.json"
```

## 5. Interpret a PDF chart using MIA

```bash
./extensions/web_tools/web-tools url "$WEB_REPORT_URL" \
  --start-page 5 --max-pages 1 --max-chars 12000 --no-ocr --vision \
  --question 'Grafik başlıklarını ve banka gruplarının paylarını Türkçe açıkla. Birimleri koru; okunamayan sayıları tahmin etme.' \
  > "$WEB_RESULTS_DIR/vision.json"
```

Look for a `mia_vision` section and `model_calls: 1`. `partial` can mean only
the requested page range was read. Always inspect `processing_errors`: if MIA
fails, native PDF text survives as partial evidence, with a sanitized diagnostic.
If nothing readable was obtained, the operation remains an error.

For a public image, use `url IMAGE_URL --ocr` for local text extraction, or
`url IMAGE_URL --no-ocr --vision --question 'Describe the chart'` for MIA vision.
Only supported public image URLs are accepted, not local file paths.

## 6. Ask a natural-language question: tools → evidence → cited answer

```bash
./extensions/web_tools/web-tools ask \
  'BDDK Türk Bankacılık Sektörü Temel Göstergeleri raporunun resmi sayfasını bul, sayfayı oku ve raporun hangi konuları kapsadığını kaynak göstererek kısaca açıkla.' \
  --max-tool-calls 4 > "$WEB_RESULTS_DIR/answer.json"

python3 - <<'PY'
import json, os
from pathlib import Path
r = json.loads((Path(os.environ['WEB_RESULTS_DIR']) / 'answer.json').read_text())
print('Status:', r['status'], 'Error:', r.get('error'))
print(r['answer'])
for source in r['sources']:
    print(f"[{source['id']}] {source['url']} — {source['location']}")
print('Usage:', r['usage'])
print('Tools:', [step['tool'] for step in r['trace']])
PY
```

The model selects only allowed tools. Source IDs such as `[S1]` refer to entries
in `sources` with URL, fetch time, location and excerpt. Unknown citations,
unknown tools and excessive calls are rejected. Model outputs still need review;
checking a citation exists is not a proof that every statement follows from it.

Supply `--url 'https://...'` to read a known source before planning. Repeat it for
several sources; add repeated `--require 'question to cover'` arguments and
`--min-sources 2` for a comparison. Follow [MULTISOURCE.md](MULTISOURCE.md) for a
complete two-source demo and the coverage/conflict output contract. `sources`
contains all retained excerpts; filter with `citations` for answer references.
Images and
PDF visual interpretation remain off per question unless `--allow-vision` is
provided. `ask` is a single-question web runner; the team's conversational
analytics integration uses [TEAM_INTEGRATION.md](TEAM_INTEGRATION.md).

## 7. Inspect saved content without another network/model request

```bash
python3 - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ['WEB_RESULTS_DIR'])
for name in ('pdf', 'text', 'vision'):
    result = json.loads((root / f'{name}.json').read_text())
    (root / f'{name}.md').write_text(result.get('content', ''), encoding='utf-8')
    print(name, result['status'], result.get('error'), result.get('processing_errors', []))
print('Open these files in VS Code:', root)
PY
```

Raw tables carry `ready_for_calculation: false`. Use the explicit schema/unit/
period validation helper before numeric analysis; see the integration guide.
No automatic database import occurs.

## Troubleshooting

- `/report-vision.json: Permission denied`: `WEB_RESULTS_DIR` was empty. Rerun
  its `mktemp` export; do not use sudo to write into the filesystem root.
- `feature_disabled`: enable the flag in this terminal and run `start` again.
- `model_access_denied`: inspect the sanitized HTTP status (401/403); check the
  worker's private key/model access. Never print the key or a provider error body.
- `model_output_limit`: the model returned no usable answer within its output
  budget. Chat/vision disable optional Qwen thinking; OCR retains its supplied
  protocol. Increasing limits is an operator decision, not an automatic retry.
- `model_timeout` / `model_unavailable`: inspect preserved source evidence. There
  are no automatic model retries, to avoid repeating billable requests.
- `model_limit`: the per-read, per-question or persisted UTC-hour budget was
  reached. Check usage/configuration; do not restart repeatedly to bypass it.
- `partial`: inspect warnings, truncation and processing errors. An intentionally
  bounded report read and a failed optional model call have different meanings.

Stop only these optional services when finished:

```bash
./extensions/web_tools/web-tools stop
```
