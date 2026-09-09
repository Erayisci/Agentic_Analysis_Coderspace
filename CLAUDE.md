# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The virtualenv lives at the repo root: `.venv` (Python 3.10), gitignored.

```bash
.venv/bin/pip install -e ".[dev]"           # once; makes `backend` importable
.venv/bin/python -m backend.ingestion.bddk_bulletin --from-cache  # render workbooks, no network
.venv/bin/python -m backend.lakehouse.build # full build: parse -> validate -> parquet + duckdb
.venv/bin/pytest -q                         # all tests (build must have run first)
.venv/bin/pytest tests/test_lakehouse.py::test_period_coverage -q
.venv/bin/flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics   # blocking lint in CI
.venv/bin/python -m backend.ingestion.bddk_bulletin --list          # the 17 bulletin tables
.venv/bin/python -m backend.ingestion.bddk_bulletin --year 2021     # all 17 tables for a year
.venv/bin/python -m backend.ingestion.bddk_bulletin --year 2026 --months 1-7 --tables 4,5
.venv/bin/python -m backend.ingestion.riskmerkezi                   # refresh TBB Risk Merkezi files
```

The downloader skips files already on disk, so a failed run is resumed by re-running it.

`.flake8` excludes `.venv` and `data` so the blocking lint command does not walk site-packages.

`pyproject.toml` makes this an editable install, so no `PYTHONPATH` juggling: run pytest and the modules
directly. CI (`.github/workflows/python-app.yml`) runs lint → build → pytest in that order; the tests read
`data/lakehouse.duckdb`, so a build is a hard prerequisite.

`data/` is gitignored and fully derived — deleting it is always safe. Raw inputs are never modified.

**`bddk_aylik_bulten/_raw_json/` is the source of truth for the bulletin tables, and it is committed.**
The endpoint response carries two fields the Excel rendering drops, and the parser cannot work without
either: `colModels` (column names, so measures are found by name rather than position) and `BasitFont`
(the only signal that separates the six different `a) Gerçek Kişiler` rows in the deposit tables).

**`bddk_aylik_bulten/<NN>_<slug>/*.xlsx` is gitignored** — those workbooks are a rendering the downloader
writes from the same HTTP response as the JSON, so committing both would mean two copies of one dataset
that can drift apart. `backend.parsing.bddk_sectoral` still reads them, so a fresh clone must render them
first with `--from-cache`; the parser's FileNotFoundError says exactly that. `riskmerkezi_sectoral/` is
committed as Excel because TBB publishes nothing else.

Consequence worth keeping: a clone builds and tests with **no network at all**, and CI never hits the
regulators' servers.

A DuckDB lock held by an editor extension will fail the build's write step — close the database in the
IDE before running the build.

## Hard constraints (from the KKB hackathon brief)

These are binding and override ordinary engineering preference:

- **No third-party LLM APIs.** Only open-weight models reachable through the **Kloudeks** platform
  (Qwen, Mistral, gpt-oss, Kimi class). All model access goes behind one client abstraction; never
  import a vendor SDK directly in tool or agent code. This also rules out hosted search/RAG services
  such as Tavily — the web-search tool must use an open backend.
- **Python for everything server-side.** Frontend framework is free choice.
- **Open-source libraries only**, and the brief names the expected set: pypdf, Pandas, DuckDB,
  Plotly, LanceDB. Use LanceDB for vector storage — not Qdrant or Chroma.
- **Deliverable is a live deployed system**, exercised on demo day with inputs the team has not seen,
  plus architecture diagrams, docs, DB definitions, and a private GitHub repo shared with KKB.

## Architecture

A deterministic ETL that turns regulator Excel corpora into a DuckDB lakehouse, built so an LLM agent
(see `Launch.MD`) never has to do arithmetic or schema reasoning itself.

```
backend/
├── core/         config, labels (shared key normalisation), errors
├── domain/       bulletin_tables (17-table registry + lifecycles), canonical (sector graph)
├── ingestion/    bddk_bulletin, riskmerkezi        — runnable CLIs, fetch only
├── parsing/      bddk_sectoral, bddk_bulletin, tbb — raw files -> long frames
├── validation/   identities, continuity            — abort the build on failure
├── transform/    analytics                         — growth, ratios, reconciliation
└── lakehouse/    build (orchestrator), schema_card
```

Dependencies point inward only: `lakehouse` orchestrates, `transform`/`validation` operate on parsed
frames, `parsing` reads what `ingestion` fetched, and both lean on `domain` declarations and `core`
primitives. `domain` and `core` import nothing from the layers above them. `agent/`, `tools/` and `api/`
join as sibling packages in later phases.

```
bddk_aylik_bulten/05_*/*.xlsx --parsing.bddk_sectoral-\
                                              validation --> domain --> transform --> data/processed/
riskmerkezi_sectoral/*.xlsx --parsing.tbb----/ (fail-fast) (crosswalk)            --> data/analytics/
                                                                                  --> data/lakehouse.duckdb
bddk_aylik_bulten/_raw_json/ --parsing.bddk_bulletin--> validation.continuity ---/   --> schema_card.md
   (all 17 tables)                                      (fail-fast)
```

Two BDDK paths coexist deliberately. `parsing.bddk_sectoral` is the pinned sectoral path feeding
`observations`, `sectors`, the crosswalk and the reconciliation monitor. `parsing.bddk_bulletin` is the
generic path that reads all 17 tables into `bulletin_observations` with a generalised entity schema. A
test asserts the two agree exactly on table 05, so the generic path cannot drift from the pinned one.

`backend/lakehouse/build.py:main` is the single orchestrator; every other module is a pure transformation
it calls.
Each parser emits **long format** (`period, source, sector_code, sector_name, metric, value`) and asserts
the exact expected row structure, so a layout change in a new monthly file raises rather than silently
producing wrong numbers.

`validate.run_all_validations` raises `ValidationError` and aborts the build if any arithmetic identity
fails. The passing report is itself persisted as the `data_quality_report` table.

`schema_card.md` is generated for the agent's context window, not for humans — keep it token-efficient and
keep the query rules in `lakehouse/schema_card.py` in sync with anything you change in `domain/canonical.py`.

## Data sources

The brief's required corpus is **2021-01 through 2026-06**, and it is wider than what is currently built.

| Source | Status |
|---|---|
| BDDK Aylık Bülten — all 17 tables | **built** (2021-01..2026-07), 135,513 observations |
| TCMB EVDS (`evds3.tcmb.gov.tr/tumSeriler`) | **not acquired — highest priority** |
| BDDK Haftalık Bülten | not acquired |
| BDDK FinTürk (İllere Göre) | not acquired |
| TBB Risk Merkezi sectoral | built (2022-01..2026-06) — **supplementary, not required by the brief** |

EVDS carries the macro series the demo scenario depends on (policy and mortgage interest rates, TÜFE,
house price index, mortgaged-sale share). Nothing in the reference scenario can be answered without it.

### The BDDK bulletin tables

All 17 share one shape — a row-label column plus measure columns — so one generic parser
(`parsing.bddk_bulletin`) covers them all and they land in a single `bulletin_observations` table.

`04_tuketici_kredileri` is the one the reference demo scenario runs on: it holds
"Tüketici Kredileri - Konut" and "Takipteki Konut Kredileri". Table 05 does **not** contain housing
loans; it breaks credit down by the borrower's activity sector, not by loan product.

Two things the tables give for free. Row labels encode their own arithmetic identities
(`Tüketici Kredileri (2+3+4)`, `Takipteki Tüketici Krd. (14+15+16)`), so validation rules are
machine-derivable rather than hand-curated. And table 15 publishes official ratios, which cross-check
ratios the agent computes itself.

**Row position is not an identifier, and neither is the raw label.** Three tables reshuffle their rows
mid-history — `03_krediler` at 2022-01 (a line dropped, four added, everything from row 15 shifts),
`08_menkul_kiymetler` at 2022-09, `12_sermaye_yeterliligi` three times — so `BasitSira` splices
unrelated line items into one series. But the label drifts too, in two cosmetic ways that
`core.labels.canonical_key` strips and nothing else:

- a footnote marker appears: `Ortaklık Finansmanı` → `Ortaklık Finansmanı*`
- the row states its own aggregation, and the indices shift as rows are inserted above it:
  `Menkul Değerler (2 den 24'e)` → `(2 den 26'ya)`

Strip only those two. A genuine rename must still read as a new series so the continuity check catches it.

**A label is only unique within its parent.** Tables 09, 10 and 11 repeat labels — `a) Gerçek Kişiler`
appears once per deposit type, `Türev İşlemler` once on each side of the liquidity table — so keys are
qualified as `parent/child`. The parent comes from `BasitFont`, which is a presentation attribute and
therefore was measured per table rather than assumed: sixteen tables read `leading` (a row belongs to the
most recent non-italic row above it) and table 11 reads `trailing` (its bold rows are section totals, not
headers). See `Table.hierarchy`. The parser asserts key uniqueness within a period regardless.

**Identity formulas are period-dependent.** `Kredi Riskine Esas Tutar (11+12+13+27+28)` became
`(11+12+13)` in 2021-06. The formula is parsed out of the label, stored per period in its own column, and
never used as part of the key — a formula pinned once and reused across history validates the wrong rows.

**A series that stops must be registered.** Every entity either spans all 67 months or appears in
`domain.bulletin_tables.KNOWN_LIFECYCLES` with its introduction/retirement period; an unregistered gap aborts
the build. The 11 current entries were found by measuring the corpus, so each is a fact about published
data, not a guess. Adding months is when this earns its keep.

Tier-1 tables are the stable ones: `04_tuketici_kredileri` holds 41 rows and `05_sektorel_kredi_dagilimi`
70 rows across every month. `12_sermaye_yeterliligi` is the least stable and should stay out of any
near-term tier.

One encoding to watch: the single italic row in table 05 is sector 46, which `domain.canonical.BDDK_DETAIL_OF`
hand-codes as a non-additive detail of 45. The generic parser derives the same relationship from
`BasitFont`, and a test asserts they agree — if it fails, the two encodings have diverged.

Most tables carry a **currency split** (`Tp` / `Yp` / `Toplam`), which `parsing.bddk_bulletin.split_measure`
peels off the measure column name into a separate `currency` dimension: `NakdiKrediToplam` becomes
metric `nakdi_kredi` with currency `total`. `taraf=10001` (whole banking sector) is fixed; that parameter
also offers a bank-group breakdown, not taken.

TBB Risk Merkezi is a source the team added; the brief does not ask for it. Keep it as an extra
cross-validation signal, but do not let it drive schema or roadmap decisions, and be prepared to drop it.

## Domain invariants (violating these produces silently wrong analysis)

**Temporal semantics are per-metric, not global.** The BDDK sectoral file is period-end outstanding
balances in bin TL (thousands) — a month-over-month change there is a net balance change (new lending −
repayments ± FX revaluation/write-offs), never "new lending". But the brief warns explicitly that other
BDDK releases are cumulative, some since inception and some year-to-date. Every metric must declare its
kind in the `metrics.temporal_semantics` column (`stock` / `flow` / `cumulative_ytd` / `cumulative_all`),
de-cumulation is a pipeline step, and no metric may reach the agent without that label set.

**The entity dimension is not "sector".** `observations` is sector-grained (`sector_code`,
`sector_name`), which fits table 05 alone — a balance-sheet line, a loan product, a maturity bucket and
a ratio are none of them sectors. `bulletin_observations` is the generalised fact table all 17 tables
land in: `period, source, dataset, entity_type, entity_key, entity_name, parent_key, metric, currency,
value, unit, formula, footnote`. One schema for a small open-weight model to learn, not seventeen.
Migrating the sectoral path onto it is still open — it would touch `domain/canonical.py`, the crosswalk and the
reconciliation monitor, so it is a deliberate step, not a side effect.

**The two sources are never merged.** BDDK `bddk_follow_up` and TBB `tbb_liquidation` are different
regulatory concepts with a persistent ~19% gap. Metric ids are source-prefixed on purpose, and a test
asserts no generic `%npl%` metric ever appears in the catalog. Cross-source questions go through
`reconciliation_monitor`, which carries a mean ± 3σ band per series; when `out_of_band` is true the
standard "documented methodology difference" explanation no longer applies.

**BDDK sector codes are a graph, not a flat list** (`domain.canonical.BDDK_CHILDREN` / `BDDK_DETAIL_OF`).
Two encoded traps: sectors 23/24 are children of 22 and already inside parent 09 (summing them with
their siblings double-counts), and sector 46 is a *non-additive detail* of 45 that must stay out of
parent 44's sum. National aggregates: sum only `relation='top_level'`, or use the TOPLAM row (code 70).

**TBB sector numbers are a monthly size rank, not an identifier.** All TBB keying is on a slugified
sector name (`core.labels.slugify`); the curated slugs in `canonical.py` are checked against
the parsed data at build time by `validate_crosswalk_against_data`, which fails loudly on drift.

## Data refresh

Adding raw months changes numbers the tests pin. `tests/test_lakehouse.py` holds golden fixtures and
asserts exact period counts (BDDK 67 months for 2021-01..2026-07, TBB 54 for 2022-01..2026-06) plus a
54-month TBB continuity check — update these deliberately alongside a refresh, and treat an unexpected
failure as a data problem first, not a test problem.

## Project stage

The lakehouse and schema card are implemented and validated for the full BDDK monthly bulletin (17
tables, 2021-01..2026-07) and the TBB sectoral corpus. Everything in `Launch.MD` past the ingestion layer
— EVDS, the remaining BDDK feeds, the Kloudeks-backed agent, the six required tools, and the deployed
trust layer — is designed but not yet written.
