# KKB Agentic Data Analytics

A deterministic data pipeline that turns Turkish banking-regulator publications into a queryable DuckDB
lakehouse, built so an LLM agent never has to do arithmetic or schema reasoning itself.

Submission for the **KKB Hackathon 2026 — Lakehouse Agent Builder and Data Analytics**.

Current state: the ingestion, validation and lakehouse layers are implemented and tested, and the
agent layer runs end to end against the Kloudeks/MIA open-weight endpoint — a typed plan DSL, the
brief's tools, a verifier and a cited answer — behind a FastAPI service with a React frontend
(`backend/api/`, `frontend/`). Docker packaging and deployment are still to come.

An optional [SearXNG + Crawl4AI web-tools extension](extensions/web_tools/README.md) provides
`search_web`, HTML `read_url`, and automatic `read_web_url` callables, with separately optional
[file, image, OCR, and Kloudeks vision tools](extensions/web_tools/ASSETS.md). Each capability
has switches and resource limits. Dependencies run in separate containers; all tools are
disabled by default (`WEB_TOOLS_ENABLED`) and do not change the pipeline setup below. When enabled,
the API wires the extension's `search_web` in as the agent's web-search backend; the in-process
`backend/tools/web_url.py` remains the URL reader either way.

To try the web tools yourself, use the [Docker-to-results testing walkthrough](extensions/web_tools/TESTING.md).
For developer onboarding without changing the baseline environment, start with
the [developer guide](extensions/web_tools/DEVELOPER_GUIDE.md).
For AI integration, see the [tool contracts and usage reference](extensions/web_tools/AI_USAGE.md).
For the complete search-to-answer demo, see [DEMO.md](extensions/web_tools/DEMO.md).
For several sources, explicit coverage and conflict reporting, see the
[multi-source research guide](extensions/web_tools/MULTISOURCE.md).

---

## Quick start

Requires Python 3.10+. Nothing else — no network access, no database server.

```bash
git clone <repo-url> && cd Agentic_Analysis_Coderspace

python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

python -m backend.ingestion.bddk_bulletin --from-cache  # raw workbooks from the archived responses
python -m backend.lakehouse.build                       # parse -> validate -> parquet + duckdb
pytest -q                                               # 246 tests
```

That produces `data/lakehouse.duckdb` and `data/analytics/schema_card.md`. The whole thing runs offline
because the regulator responses are committed to the repository — see [Raw data](#raw-data).

Query it:

```bash
python -c "import duckdb; print(duckdb.connect('data/lakehouse.duckdb', read_only=True).execute('''
    SELECT period, value FROM bulletin_observations
    WHERE entity_key = 'tuketici_kredileri_konut' AND currency = 'total'
    ORDER BY period DESC LIMIT 6''').df())"
```

---

## Commands

### Build

| Command | What it does |
|---|---|
| `python -m backend.lakehouse.build` | The full build: parse → validate → Parquet + DuckDB + schema card. Aborts on any integrity failure. |
| `pytest -q` | All tests. Needs a completed build (they read `data/lakehouse.duckdb`). |
| `flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics` | The blocking lint CI runs. |

`data/` is fully derived — deleting it is always safe, the build recreates it.

### Ingestion

Ingestion **fetches raw data onto disk**; it never parses or interprets. You only need it to add new
months or to refresh a source. A fresh clone can build without it.

```bash
# Regenerate the workbooks from the committed responses. No network.
python -m backend.ingestion.bddk_bulletin --from-cache

# BDDK monthly bulletin, all 17 tables for a year
python -m backend.ingestion.bddk_bulletin --year 2026

# ...a specific window, or specific tables
python -m backend.ingestion.bddk_bulletin --year 2026 --months 1-7 --tables 4,5
python -m backend.ingestion.bddk_bulletin --list        # what the 17 tables are

# TBB Risk Merkezi sectoral distribution
python -m backend.ingestion.riskmerkezi

# TCMB EVDS: series catalogue, then data at native frequency. Needs EVDS_API_KEY
# (environment or repo-root .env, gitignored). The archive under evds/_raw_json/
# is committed, so this is only for refreshing.
python -m backend.ingestion.evds --list
python -m backend.ingestion.evds --catalog --fetch
```

Downloads skip files already on disk, so an interrupted run is resumed by re-running the same command.
Pass `--overwrite` to force.

**Adding months changes numbers the tests pin.** `tests/test_lakehouse.py` asserts exact period counts
and golden values. Update those deliberately alongside a refresh, and treat an unexpected failure as a
data problem first, not a test problem.

---

## Data sources

The brief's target corpus is **2021-01 through 2026-06**.

| Source | Status |
|---|---|
| BDDK Aylık Bülten — all 17 tables | ✅ built, 2021-01..2026-07, 135,513 observations |
| TCMB EVDS — 44 data groups, 1,515 series | ✅ built, 2021-01..2026-07, 89,680 monthly rows (+ native frequency) |
| BDDK Haftalık Bülten — all 9 tables | ✅ built, 2021-01-08..2026-09-04, 163,740 observations |
| BDDK FinTürk (İllere Göre) | ❌ not acquired |
| TBB Risk Merkezi sectoral | ✅ built, 2022-01..2026-06 — supplementary, not required by the brief |

### Raw data

Two things are committed, and the distinction matters:

- **`bddk_aylik_bulten/_raw_json/`** — the archived endpoint responses. This is the **source of truth**.
  It is what the bulletin parser reads, and it carries two fields the Excel rendering drops: `colModels`
  (column names, so measures are found by name rather than position) and `BasitFont` (the only signal
  separating the six different `a) Gerçek Kişiler` rows in the deposit tables).
- **`riskmerkezi_sectoral/*.xlsx`** — TBB publishes only Excel, so that *is* the source format.

**`bddk_aylik_bulten/<NN>_<slug>/*.xlsx` is gitignored.** Those workbooks are a rendering of the JSON,
written by the same downloader from the same HTTP response. Committing both would mean two copies of one
dataset that can silently disagree. Regenerate them any time with `--from-cache`.

---

## Architecture

```
backend/
├── core/         config, labels (shared key normalisation), errors
├── domain/       bulletin_tables (17-table registry + lifecycles), canonical (sector graph), evds_series
├── ingestion/    bddk_bulletin, riskmerkezi, evds  — runnable CLIs, fetch only
├── parsing/      bddk_sectoral, bddk_bulletin, tbb, evds — raw files -> long frames
├── validation/   identities, continuity, weekly, macro  — abort the build on failure
├── transform/    analytics, bulletin, macro        — growth, ratios, de-cumulation, monthly alignment
├── lakehouse/    build (orchestrator), schema_card
├── llm/          client — the only module that talks to a model (Kloudeks/MIA)
├── tools/        lakehouse, series, transforms, charts, anomaly, web_url — no model calls
├── agent/        state, router, planner, executor, verifier, composer, pipeline
└── eval/         scenarios.yaml + run_eval — benchmark against SQL-computed gold numbers
```

### The agent

    question -> router -> planner -> executor -> verifier -> composer -> answer

The model classifies intent, emits a typed plan, and writes prose over numbers it did not compute.
Everything between is Python: the plan is a pydantic schema the server's guided decoding constrains
generation to, the executor runs it and records a citation per series, and the verifier checks units,
coverage and lineage before anything is said. Run the benchmark with:

```bash
python -m backend.eval.run_eval --out eval_results.md    # needs KLOUDEKS_API_KEY
python -m backend.eval.run_eval --config deterministic   # no model, no network
```

Dependencies point inward only: `lakehouse` orchestrates, `transform`/`validation` operate on parsed
frames, `parsing` reads what `ingestion` fetched, and both lean on `domain` declarations and `core`
primitives. `domain` and `core` import nothing from the layers above them. `agent/`, `tools/` and `api/`
join as sibling packages in later phases.

Two BDDK paths coexist on purpose. `parsing.bddk_sectoral` is the pinned sectoral path;
`parsing.bddk_bulletin` is the generic path covering all 17 tables. A test asserts they agree exactly on
table 05, so the generic path cannot drift from the pinned one.

### Lakehouse tables

| Table | Contents |
|---|---|
| `bulletin_observations` | All 17 BDDK bulletin tables, long format, generalised entity schema |
| `observations` | BDDK sectoral + TBB sectoral, the pinned sector-grained corpus |
| `sectors`, `metrics`, `sector_crosswalk` | Dimensions and the cross-source mapping |
| `growth`, `ratios` | Month-over-month / year-over-year changes, derived ratios |
| `macro_series`, `macro_observations`, `macro_observations_native` | TCMB EVDS series index, monthly-aligned values, and the native-frequency points |
| `reconciliation_monitor` | BDDK vs TBB divergence with a mean ± 3σ band per series |
| `bulletin_entities` | The agent's search surface: 519 monthly line items with unit, semantics, parent and period span |
| `weekly_observations`, `weekly_items` | All 9 BDDK weekly tables; items carry `retired_on` and `is_informational` |
| `data_quality_report`, `bulletin_lifecycle_report`, `weekly_lifecycle_report` | Validation evidence, persisted and quotable |

`data/analytics/schema_card.md` is generated for an agent's context window, not for humans.

---

## Things that will produce silently wrong analysis

These are enforced in code and documented at length in [`CLAUDE.md`](CLAUDE.md). The short version:

- **The two sources are never merged.** BDDK `follow_up` and TBB `liquidation` are different regulatory
  concepts with a persistent ~19% gap. Cross-source questions go through `reconciliation_monitor`.
- **BDDK sector codes are a graph, not a list.** Sectors 23/24 sit inside parent 09 as well as 22, and
  sector 46 is a non-additive detail of 45. For national totals, sum only `relation='top_level'`.
- **Row position is not an identifier.** Three bulletin tables reshuffle their rows mid-history. Rows are
  keyed on a normalised label qualified by parent; a series that starts or stops must be registered in
  `domain.bulletin_tables.KNOWN_LIFECYCLES` or the build fails.
- **Temporal semantics are per series, never assumed.** BDDK balances are period-end stocks (a
  month-over-month change is a net balance change, never "new lending"), the BDDK income statement is
  year-to-date, and every EVDS series declares `stock` / `flow` / `rate` / `index` plus the rule that
  made it monthly. Read `metrics` / `macro_series` before differencing anything.

---

## Constraints from the brief

- **No third-party LLM APIs.** Only open-weight models via the Kloudeks platform.
- **Python server-side**, open-source libraries (pypdf, Pandas, DuckDB, Plotly, LanceDB).
- **A live deployed system** on demo day, exercised with unseen inputs.

---

## Documentation

| File | Purpose |
|---|---|
| [`Launch.MD`](Launch.MD) | Architecture and execution blueprint: stack, agent design, roadmap |
| [`CLAUDE.md`](CLAUDE.md) | Working notes for contributors and coding agents: invariants, traps, refresh procedure |
| [`extensions/web_tools/README.md`](extensions/web_tools/README.md) | Optional web tools overview and fresh-clone onboarding |
| [`extensions/web_tools/TESTING.md`](extensions/web_tools/TESTING.md) | Manual testing, expected results, saved output, limits, and shutdown |
| [`extensions/web_tools/AI_USAGE.md`](extensions/web_tools/AI_USAGE.md) | Python tool contracts and guidance for AI consumers |
| `data/analytics/schema_card.md` | Generated, agent-facing description of the lakehouse |
