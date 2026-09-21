# Test LLM web research from the website

The website's **Web araştırması** mode runs the existing bounded research agent:
the model chooses a search query, inspects search results, chooses URLs to read,
and writes an answer with source IDs. **Araştırma** shows the actual tool calls,
source excerpts, errors, and the complete saved tool outputs for each question.
In automatic mode, questions routed to `search` use the same research loop when
it is enabled; analytics questions keep using the existing analysis pipeline.

## Start the services and website

Use Python 3.10+, Node compatible with the frontend's Vite version (22.16+ works),
and Docker Engine/Compose. In WSL, enable Docker Desktop's integration for your
distro first. These commands run from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
npm --prefix frontend ci
npm --prefix frontend run build

./extensions/web_tools/web-tools setup
```

Set `WEB_KLOUDEKS_API_KEY` in the ignored `extensions/web_tools/.env` if it is
not already configured. Keep the key out of the browser and source code.
Then, in the same terminal:

```bash
export WEB_TOOLS_ENABLED=true
export WEB_AGENT_ENABLED=true
export WEB_DOCUMENTS_ENABLED=true
export WEB_LINKS_ENABLED=true

./extensions/web_tools/web-tools start
./extensions/web_tools/web-tools check
.venv/bin/python -m backend.api.serve
```

Open **http://127.0.0.1:8000**, choose **Web araştırması**, and submit:

> BDDK Türk Bankacılık Sektörü Temel Göstergeleri raporunun resmi sayfasını bul,
> oku ve raporun hangi konuları kapsadığını kaynak göstererek açıkla.

The launcher loads the web-tools CLI configuration (`.env.example`, extension
`.env`, then process environment) and also accepts the root model key. It serves
the built website and API on one origin. For persistent settings across terminals,
edit the existing flags in the extension `.env` instead of only exporting them.
The worker and API must use matching capabilities and service addresses.

For frontend development, leave the API running and use `npm --prefix frontend
run dev`; Vite proxies API calls to port 8000. `VITE_API_BASE_URL` overrides the
API address for deployments using a separate origin. `/health` reports whether
research is configured; this is configuration status, not a live service probe.
Use `web-tools check` to check the services.

## Inspect what was actually used

- In **Araştırma**, expand each tool call to inspect its arguments and full saved
  result. Search hits that the model did not select are retained too.
- Match `[S1]`, `[S2]`, etc. in the answer to **Kaynaklar** and open the original
  publication. A citation match establishes provenance, not factual correctness.
- Check **Eksik bilgiler**, errors and limitations. An unfinished run keeps its
  evidence and is labelled partial or failed; it does not claim a completed answer.
- Refresh the page, return to **Araştırma**, and select a saved question. The
  database survives API restarts and **Yeni Sohbet**. Conversation working tables
  remain in memory and are cleared by a new chat.

Native extraction, OCR, vision and model calls retain their existing resource
limits. The database stores everything returned by the tools, including extracted
tables, page sections, snippets, metadata, timestamps, truncation flags and errors.
It does not promise to download an entire website or every page of a bounded PDF.
Research can consume the configured Kloudeks quota.

## Database and API

Evidence is automatically committed **before** a tool result reaches the model
or is shortened for its context. The default is `data/research.sqlite3`, with
`research_runs` and `research_tool_results` tables. Every result links to a run,
session, question, tool and arguments; the completed response links answers and
citations back to those results. If saving fails, the request returns an error
instead of reporting a successful ingestion. Already committed results survive.

This is a persistent evidence database alongside the validated analytics database
`data/lakehouse.duckdb`. Extracted web text/tables remain source evidence and are
not automatically converted into verified financial observations. The lakehouse
build does not erase research history. **Back up the research database**; unlike
the lakehouse it cannot be rebuilt from committed inputs. Set `RESEARCH_DB_PATH`
to store it elsewhere. All website `/ask` turns, including ordinary search,
URL-reading and model-selected external-series ingestion, use the evidence store.
For series ingestion, the returned values and source/unit metadata are saved.
Standalone Python/CLI callers opt in
with `Agent(evidence_store=...)` or the research runner's `on_tool_result` callback.
The manual `/debug/ingest_external` helper still creates a session-only column.

```text
POST /ask
  {"question":"Find and read an official report","session_id":"my-session","mode":"research"}

GET /session/my-session/research
GET /session/my-session/research/<run_id>
```

The response's `ingestion` contains `status`, `run_id` and the number of committed
tool results. History endpoints scope results to the supplied session ID; this
local demo API does not provide account authentication.

To inspect a saved run directly without any network or model call:

```bash
.venv/bin/python - <<'PY'
import sqlite3
with sqlite3.connect('data/research.sqlite3') as db:
    for row in db.execute('''
        SELECT r.question, r.status, t.tool, t.arguments_json, t.output_json
        FROM research_runs r JOIN research_tool_results t ON t.run_id = r.id
        ORDER BY t.id DESC LIMIT 5
    '''):
        print(row)
PY
```

## Verification

`pytest -q tests/test_research_persistence.py` exercises the API and the real
bounded research loop with deterministic model decisions and tool responses.
It verifies restart/reset persistence, session isolation, context/evidence limits,
failed model/tool calls, failed storage, ordinary URL reads, and concurrent writes.
These tests consume no model quota. Live result quality requires the running
services and a real model; inspect official-source relevance and answer citations
using the website steps above.
