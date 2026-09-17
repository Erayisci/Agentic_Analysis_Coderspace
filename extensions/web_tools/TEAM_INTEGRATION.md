# Team integration and branch findings

For setup and a standalone Python example, start with
[DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md). For runtime bot instructions and validated
dispatch, use [AI_USAGE.md](AI_USAGE.md). This document covers the existing team
agent's adapter and the reviewed branch differences.

Reviewed on 2026-09-16, without checking out, merging or editing teammates' branches.

## What was found

- `origin/web-url-tool/ilmay` at `e7dac3d`: İlmay's `backend/tools/web_url.py`
  provides one `read_url()` interface for PDF, Excel, static HTML and text/JSON.
  Image handling explicitly raises `NotImplementedError`. Its tests use mocked
  network responses. The branch also inherits unrelated analytical/unit changes.
- The reader resolves hosts before Requests connects, but does not pin the
  validated address to that connection. It downloads entire responses and parses
  all PDF pages/sheets before limiting output. It adds parser dependencies to the
  baseline `pyproject.toml`. These are reasons not to merge it wholesale into the
  isolated optional extension.
- `origin/feat/weekly-evds-lakehouse` at `bbf2fb5`: the team's five-stage agent
  (`route → plan → execute → verify → compose`) accepts `url_reader` and
  `web_search` injections. Its document composer expects a `text` field.

This implementation adopts the unified-interface and charset-aware text ideas,
retaining the isolated downloader, bounded parsing, Chromium, OCR/vision and
network guard. No teammate branch was removed. The original stash is retained.

## Python tool registration

```python
from backend.tools import get_tools

# Plain Python reads this process's environment, not extensions/web_tools/.env.
tools = get_tools()
result = tools['read_web_url']('https://www.bddk.org.tr/Veri/EkGetir/8?ekId=625',
                               start_page=5, max_pages=1, ocr=False)
```

`read_url` in the registry remains HTML-only for compatibility. New code can use
`read_web_url` for all supported formats. `backend.tools.web_url.read_url` also
delegates to the unified reader, but returns the extension's structured-evidence
contract (`content`, `sections`, `status`), not İlmay's original dictionary shape.

For another framework, `web-tools schemas` exports JSON function schemas.
`backend.extensions.web_tools.agent_protocol.tool_schemas()` is the equivalent
Python helper. Registration does not itself run an agent or start Docker.

## Connect the team's existing agent

After the team integrates the extension files into its agent branch:

```python
from backend.agent.pipeline import Agent
from backend.extensions.web_tools.team_adapter import get_team_tools

agent = Agent(client=team_kloudeks_client, **get_team_tools())
result = agent.ask('https://www.bddk.org.tr/Veri/Detay/162 sayfasını özetle.')
```

`team_kloudeks_client` is the team's existing configured client; it is not created
by this snippet. `backend.agent` is not on `perhat`, so that import belongs in
the combined application. This change does not merge the analytical branch or
replace its planner, database, model client or conversation state.

The adapter supplies `text`, `kind`, source URL and bounded metadata; errors raise
a sanitized exception so the team's executor records a failed step. It preserves
partial text when optional interpretation fails. `get_team_tools(vision=True)`
opts that application's URL reads into allowed vision; the default is off.

Keep extraction warnings visible when adapting the team's composer: its current
document field whitelist omits the extension's warnings and processing errors.
Prefer the final redirected URL for citations. A successful adapter call proves
evidence was read, not that the final narrative verified every numeric claim.

On `perhat` alone, use `web-tools ask` or
`backend.extensions.web_tools.research.research()` for the standalone cited web
workflow. It does not require the other branch's agent packages.

## Validate tables before calculations

Extraction preserves raw cells. Every table section initially says
`validation.ready_for_calculation: false`. The trusted caller must establish
expected headers, numeric columns, locale, units and reporting period, and verify
those against the source. Do not let an LLM set `source_verified=True` on its own.

```python
from backend.tools.table_validation import validate_extracted_table

# Example contract: replace it with the headers/units/period of your actual source.
validated = validate_extracted_table(
    evidence=result, section_index=0,
    expected_columns=['Month', 'Revenue'], numeric_columns=['Revenue'],
    unit='TRY', period='2026-03', source_verified=True,
    decimal_separator=',', thousands_separator='.',
)
if not validated['ready_for_calculation']:
    raise ValueError(validated['errors'])
```

The helper rejects missing provenance, partial/truncated extraction, wrong or
duplicate headers, ragged rows, missing cells, invalid grouping and ambiguous
numeric syntax. For that explicit locale, `1.234,56` becomes the decimal string
`1234.56`. Convert normalized numeric strings to `Decimal` for exact arithmetic.
It never executes formulas, guesses units, reconciles publisher data or writes to
DuckDB. Passing means the explicit contract passed, not that the source is true.

## Limits and failure contracts

- `WEB_AGENT_ENABLED=false` disables the research model endpoint and runner.
- Defaults per question: six tool calls, six total planning/interpretation model
  calls, six distinct URL read attempts, 20 MiB file allocation, 8 MiB retained
  tool-output JSON, 300 seconds and 20,000 serialized context characters. Caller limits clamp to the
  worker's advertised limits. These are runner limits; the internal decision
  endpoint itself is not a persistent conversation manager.
- All worker model attempts also share `WEB_MODEL_MAX_CALLS_PER_HOUR`. Model
  failures are never automatically retried. A lost response counts conservatively.
- Model failure with usable file text returns `partial`, `error: null` and
  `processing_errors`; without text it remains an error. `model_calls` counts
  attempts. The hard child-process deadline can still terminate an entire read.
- Chat and vision use `chat_template_kwargs.enable_thinking=false` to avoid
  spending a small output budget solely on Qwen reasoning. OCR keeps the exact
  supplied MIA OCR prompt and settings.
- Citations verify source identity and retain locations, not entailment. Review
  the answer and numbers before relying on them.

For several sources, call `research(question, urls=[...], requirements=[...],
min_sources=2)`. `min_sources` defaults to 1 and counts distinct cited documents,
not pages or independent publishers. Preserve `coverage`, `conflicts`,
`missing_information` and `stop_reason` in the main agent's final response.
`sources` includes uncited evidence; `citations` identifies the answer's references.
See [MULTISOURCE.md](MULTISOURCE.md) for the full output/limit contract and demo.
