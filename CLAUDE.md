# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The virtualenv lives at the repo root: `.venv` (Python 3.10), gitignored.

```bash
.venv/bin/pip install -e ".[dev]"           # once; makes `backend` importable
.venv/bin/python -m backend.ingestion.bddk_bulletin --from-cache  # render workbooks, no network
.venv/bin/python -m backend.lakehouse.build # full build: parse -> validate -> parquet + duckdb
.venv/bin/pytest -q                         # all tests, incl. extensions/web_tools/tests (build must have run first)
.venv/bin/pytest tests/test_bulletin.py -q  # parser/label invariants only — needs no build
.venv/bin/pytest tests/test_lakehouse.py::test_period_coverage -q
.venv/bin/pytest -q -k risk_weight          # one test by name, across files
.venv/bin/flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics   # blocking lint in CI
.venv/bin/python scripts/lakehouse_query.py            # the manual check plan, expectations printed
.venv/bin/python scripts/lakehouse_query.py --sql "SELECT * FROM macro_series LIMIT 5"
.venv/bin/python scripts/lakehouse_query.py --repl     # one statement per line, empty line quits
.venv/bin/python -m backend.ingestion.bddk_bulletin --list          # the 17 bulletin tables
.venv/bin/python -m backend.ingestion.bddk_weekly --list            # the 9 weekly tables
.venv/bin/python -m backend.ingestion.bddk_weekly --catalog --fetch # refresh the weekly archive
.venv/bin/python -m backend.ingestion.bddk_finturk --list            # the 7 il-bazli tables
.venv/bin/python -m backend.ingestion.bddk_finturk --fetch           # refresh the FinTurk archive (2021-Q1..2026-Q2)
.venv/bin/pytest tests/test_finturk.py -q  # parser/label invariants only — needs no build
.venv/bin/pytest tests/test_weekly.py -q    # weekly parser/validation; 3 lakehouse tests skip without a build
.venv/bin/python -m backend.ingestion.bddk_bulletin --year 2021     # all 17 tables for a year
.venv/bin/python -m backend.ingestion.bddk_bulletin --year 2026 --months 1-7 --tables 4,5
.venv/bin/python -m backend.ingestion.riskmerkezi                   # refresh TBB Risk Merkezi files
.venv/bin/python -m backend.ingestion.evds --list                   # the registered EVDS data groups
.venv/bin/python -m backend.ingestion.evds --catalog --fetch        # refresh the EVDS archive (needs key)
.venv/bin/python -m backend.ingestion.evds --fetch --tier 0 --overwrite   # re-pull the demo inputs only
```

EVDS calls need `EVDS_API_KEY`, read from the environment or the gitignored repo-root `.env`
(`backend.core.config.evds_api_key`). Only ingestion needs it: the parser and the build read the
committed archive under `evds/_raw_json/`, so a clone builds and tests with no key.

The downloader skips files already on disk, so a failed run is resumed by re-running it.

`.flake8` excludes `.venv` and `data` so the blocking lint command does not walk site-packages.

`pyproject.toml` makes this an editable install, so no `PYTHONPATH` juggling: run pytest and the modules
directly (it also installs a `kkb-build` console script for `backend.lakehouse.build:main`). CI
(`.github/workflows/python-app.yml`) runs lint → `--from-cache` render → build → pytest in that order.
`requirements.txt` only forwards to `pyproject.toml` (`-e .[dev]`) — add dependencies in `pyproject.toml`.

`pytest` also collects `extensions/web_tools/tests`, the web-tools extension's unittest suite (15 of
its tests skip unless `WEB_TOOLS_TEST_ASSETS` / `WEB_TOOLS_TEST_BROWSER` point at running containers),
in `--import-mode=importlib` because both directories hold a `test_tools.py`. The extension itself is
off unless `WEB_TOOLS_ENABLED=true`; when on, `backend/api/main.py` wires its `search_web` in as the
agent's web-search backend. The in-process `backend/tools/web_url.py` stays the URL reader either way,
and `KLOUDEKS_API_KEY` serves both the team client and the extension's own client (a second Kloudeks
client under `backend/model_clients/` — unifying the two is open work).

These test files differ in what they need, and the difference is in the fixtures:

| File | Needs a build? |
|---|---|
| `tests/test_bulletin.py` | No — parses `bddk_aylik_bulten/_raw_json/` directly, runs on a bare clone |
| `tests/test_weekly.py` | Mostly no — parses `bddk_haftalik_bulten/_raw/`; its 3 `connection` tests **skip** without `data/lakehouse.duckdb` |
| `tests/test_finturk.py` | Mostly no — parses `bddk_finturk/_raw_json/`; its 2 `connection` tests **skip** without the database |
| `tests/test_evds.py` | Mostly no — parses `evds/_raw_json/`; its 6 `connection` tests **skip** without the database |
| `tests/test_lakehouse.py` | **Yes, hard** — its `connection` fixture calls `pytest.fail`, not `skip` |

A skipped test is not a passing one: after changing anything the build writes, run the full suite with a
completed build, or the lakehouse assertions quietly do not run.

`data/` is gitignored and fully derived — deleting it is always safe. Raw inputs are never modified.

**`bddk_haftalik_bulten/_raw/` is the source of truth for the weekly tables, and it is committed** (~11 MB,
nine tables plus the item picker). The weekly endpoint returns HTML rather than JSON, so the envelope
stores the report table verbatim together with the request that produced it; see the weekly section below
for why the request matters.

**`bddk_aylik_bulten/_raw_json/` is the source of truth for the bulletin tables, and it is committed.**
The endpoint response carries two fields the Excel rendering drops, and the parser cannot work without
either: `colModels` (column names, so measures are found by name rather than position) and `BasitFont`
(the only signal that separates the six different `a) Gerçek Kişiler` rows in the deposit tables).

**`bddk_finturk/_raw_json/` is the source of truth for the FinTurk tables, and it is committed**
(154 files, 7 tables × 22 quarters). Every cell already arrives as a JSON number, so unlike the
bulletin archive there is no string-to-number coercion for the parser to depend on getting right.

**`bddk_aylik_bulten/<NN>_<slug>/*.xlsx` is gitignored** — those workbooks are a rendering the downloader
writes from the same HTTP response as the JSON, so committing both would mean two copies of one dataset
that can drift apart. `backend.parsing.bddk_sectoral` still reads them, so a fresh clone must render them
first with `--from-cache`; the parser's FileNotFoundError says exactly that. `riskmerkezi_sectoral/` is
committed as Excel because TBB publishes nothing else.

Consequence worth keeping: a clone builds and tests with **no network at all**, and CI never hits the
regulators' servers.

A DuckDB lock held by an editor extension will fail the build's write step — close the database in the
IDE before running the build. Everything that only reads (`scripts/lakehouse_query.py`, every test
fixture) opens the file `read_only=True`, so those never take the lock.

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
├── domain/       bulletin_tables (17-table registry + lifecycles), weekly_tables (9-table registry,
│                 lifecycles, formula overrides, weekly↔monthly pairs), canonical (sector graph),
│                 evds_series (EVDS data-group registry + temporal declarations)
├── ingestion/    bddk_bulletin, bddk_weekly, riskmerkezi, evds  — runnable CLIs, fetch only
├── parsing/      bddk_sectoral, bddk_bulletin, bddk_weekly, tbb, evds — raw files -> long frames
├── validation/   identities, continuity, weekly, macro  — abort the build on failure
├── transform/    analytics, bulletin, macro        — growth, ratios, reconciliation, de-cumulation,
│                 the bulletin metric catalogue, monthly alignment
├── lakehouse/    build (orchestrator), schema_card
├── llm/          client — the ONLY module that talks to a model (Kloudeks/MIA, httpx, no SDK)
├── tools/        lakehouse (discover/fetch_series/run_sql), series (the shared loader),
│                 transforms, charts, anomaly, web_url — pure functions, no model calls
├── agent/        state (AnalysisArtifact + lineage), router, planner (the plan DSL),
│                 executor, verifier, composer, pipeline (wires the five stages)
└── eval/         scenarios.yaml + run_eval — benchmark the model against SQL-computed golds

scripts/lakehouse_query.py — read-only DuckDB console + a manual check plan with expected values
```

Dependencies point inward only: `lakehouse` orchestrates, `transform`/`validation` operate on parsed
frames, `parsing` reads what `ingestion` fetched, and both lean on `domain` declarations and `core`
primitives. `domain` and `core` import nothing from the layers above them. `agent/`, `tools/` and `api/`
join as sibling packages in later phases.

```
evds/_raw_json/ -------------parsing.evds---------> transform.macro --> validation.macro --\
   (catalogue + 44 groups)                          (monthly align)     (coverage)          \
bddk_aylik_bulten/05_*/*.xlsx --parsing.bddk_sectoral-\
                                              validation --> domain --> transform --> data/processed/
riskmerkezi_sectoral/*.xlsx --parsing.tbb----/ (fail-fast) (crosswalk)            --> data/analytics/
                                                                                  --> data/lakehouse.duckdb
bddk_aylik_bulten/_raw_json/ --parsing.bddk_bulletin--> validation.continuity ---/   --> schema_card.md
   (all 17 tables)                              |       (fail-fast)                 /
                                                |    transform.bulletin (de-cumulate)
                                                |                                   /
bddk_haftalik_bulten/_raw/ ---parsing.bddk_weekly---> validation.weekly ------------/
   (9 tables, whole history)                          (fail-fast; also cross-checks the
                                                       weekly figures against the monthly
                                                       ones on the 10 month-end Fridays)
```

Two BDDK paths coexist deliberately. `parsing.bddk_sectoral` is the pinned sectoral path feeding
`observations`, `sectors`, the crosswalk and the reconciliation monitor. `parsing.bddk_bulletin` is the
generic path that reads all 17 tables into `bulletin_observations` with a generalised entity schema. A
test asserts the two agree exactly on table 05, so the generic path cannot drift from the pinned one.

`backend/lakehouse/build.py:main` is the single orchestrator; every other module is a pure transformation
it calls. It ends with one `tables` dict, and that dict *is* the lakehouse — each entry is written both as
Parquet (under `data/processed/` for the base tables, `data/analytics/` for the derived ones) and as a
DuckDB table of the same name. Adding a table means adding one entry there:

| Table | Contents |
|---|---|
| `bulletin_observations` | all 17 BDDK monthly tables, generalised entity schema; `value` as published plus `value_flow` where the series is cumulative |
| `bulletin_entities` | the agent's search surface: one row per (dataset, entity_key) for all 519 monthly line items, with unit, semantics, parent, the currencies/metrics it publishes and its period span |
| `bulletin_metrics` | the bulletin half of the series index: one row per (dataset, metric, currency, unit), carrying `temporal_semantics` and `metric_kind` |
| `bulletin_footnotes` | `Json.uyari` methodology notes, one row per distinct text with the period span it covers |
| `weekly_observations`, `weekly_items` | all 9 BDDK weekly tables; items carry `retired_on`, `is_informational`, `parent_key` |
| `observations` | pinned sector-grained corpus: BDDK sectoral + TBB |
| `sectors`, `metrics`, `sector_crosswalk` | dimensions and the cross-source mapping |
| `growth`, `ratios` | MoM/YoY changes and derived ratios |
| `reconciliation_monitor` | BDDK vs TBB divergence, mean ± 3σ band per series |
| `macro_series` | series index for TCMB EVDS: names, unit, native frequency, `temporal_semantics`, `monthly_rule` |
| `macro_observations` | EVDS at the monthly grain: `value` per rule plus `value_avg` / `value_last`, `n_native_obs` |
| `macro_observations_native` | the same series at their own daily/weekly/monthly/quarterly frequency |
| `data_quality_report`, `bulletin_lifecycle_report`, `weekly_lifecycle_report` | validation evidence, persisted so the agent can quote it |

Numeric thresholds are not scattered through the transforms: identity tolerances and the reconciliation
hard bounds all live in `backend/core/config.py` alongside the paths.
Each parser emits **long format** (`period, source, sector_code, sector_name, metric, value`) and asserts
the exact expected row structure, so a layout change in a new monthly file raises rather than silently
producing wrong numbers.

`validate.run_all_validations` raises `ValidationError` and aborts the build if any arithmetic identity
fails. The passing report is itself persisted as the `data_quality_report` table.

`schema_card.md` is generated for the agent's context window, not for humans — keep it token-efficient and
keep the query rules in `lakehouse/schema_card.py` in sync with anything you change in `domain/canonical.py`.

Its `## Query patterns` section holds seven worked SQL examples, and the card tells the model they run as
written. `test_every_schema_card_query_runs_and_returns_rows` parses them back out of the generated card and
executes each one, so that claim is enforced rather than asserted: a renamed column or a retired entity_key
fails the build's tests instead of leaving the agent copying SQL that errors. Add an example and update the
expected count in `test_schema_card_documents_the_query_patterns`.

**Discovery is a table, not a `SELECT DISTINCT`.** `macro_series` and `weekly_items` already let a question
find its series without reading the fact table; `bulletin_entities` closes the same gap for the 519 monthly
line items. `entity_key` is ASCII-slugified Turkish, so `ILIKE` on the key is case-safe where `ILIKE` on
`entity_name` is not — `'İ'.lower()` is not `'i'`, and a Turkish character in a key would silently break the
search the card documents. `test_entity_keys_are_ascii_so_turkish_search_is_case_safe` pins that.

## Data sources

The brief's required corpus is **2021-01 through 2026-06**, and it is wider than what is currently built.

| Source | Status |
|---|---|
| BDDK Aylık Bülten — all 17 tables | **built** (2021-01..2026-07), 135,513 observations |
| TCMB EVDS — 44 data groups, 1,515 series | **built** (2021-01..2026-07), 158,179 native / 89,680 monthly rows |
| BDDK Haftalık Bülten — all 9 tables | **built** (2021-01-08..2026-09-04), 163,740 observations |
| BDDK FinTürk (İllere Göre) | **built** (2021-Q1..2026-Q2, 22 quarters), 905,620 observations |
| TBB Risk Merkezi sectoral | built (2022-01..2026-06) — **supplementary, not required by the brief** |

### BDDK FinTürk (il-bazlı / geographic distribution)

A third BDDK release, separate from both bulletins: **quarterly** (Mar/Jun/Sep/Dec, not monthly
or weekly) and broken down by **province** (81 iller + `YURT DIŞI`), not by balance-sheet line.
Fetched from `POST https://www.bddk.org.tr/BultenFinturk/tr/Home/VeriGetir`, which returns the
same JSON envelope shape as the monthly bulletin (`Json.colModels`, `colNames`,
`data.rows[].cell`) but needs no session/CSRF handshake, unlike the weekly bulletin's
`__RequestVerificationToken` dance. Measured live: `tarafList`/`sehirList` accept every value in
one POST (repeated form keys, ASP.NET's ordinary `List<T>` binding), so the whole corpus is
`7 tables × 22 quarters` = 154 requests, not that times 7 taraf groups times 82 provinces.

7 tables (`krediler`, `mevduat`, `bireysel_bankacilik`, `sektorel_krediler`, `oranlar`,
`subeler_ve_nufus`, `altin`), landing in `finturk_observations`:
`period, source, dataset, province, taraf_code, taraf_name, metric, metric_name, value, unit`.

**No column states an arithmetic formula** (unlike both bulletins' row labels), so there is no
`formula`/`parent_key` here and nothing resembling `WEEKLY_FORMULA_OVERRIDES` — see
`domain.finturk_tables` for why that is a fact about the source, not a gap in the parser.
`taraf_code` 10001 (SEKTÖR) is the whole sector; 10002..10007 break it down by ownership
(Mevduat, Kalkınma ve Yatırım, Katılım, Yabancı, Kamu, Yerli Özel) — summing all seven
double-counts, since 10001 already IS their sum. **There is no published national-total row**;
a Türkiye-wide figure is `SUM(value) GROUP BY period` over every province.

The endpoint's PascalCase field ids (`colModels[i]['name']`, e.g. `AltinDepoGercek`) do not
derive predictably from the Turkish label BDDK shows for the same column (`colNames[i]`,
"Altın Mevduatı-Gerçek Kişi") — measured across all seven tables, so `domain.finturk_tables`
does not hardcode either column list. The parser reads both straight out of each archived
response and keys `metric` on `core.labels.slugify(colNames[i])`.

`finturk_metrics` (76 rows, one per `(dataset, metric)`) is the discovery index, mirroring
`bulletin_metrics` — without it an agent would have to `SELECT DISTINCT` over 905k fact rows to
learn what FinTurk publishes. It is in `tools.lakehouse.discover`'s default search set alongside
the bulletin/weekly/macro indexes, and both `finturk_observations` and `finturk_metrics` are in
`ALLOWED_TABLES` for `run_sql`. `fetch_series`/`tools.series.load_series` carry the `province`
dimension the fact table needs: naming an il (`Step.province`) filters to it, and leaving it out
sums every province (there is no published Türkiye-wide row in this product) — `taraf_code` is
pinned to 10001 (SEKTÖR, the whole sector) the same way the monthly bulletin pins its own `taraf`,
and is not a caller-facing filter.

### TCMB EVDS

`evds3.tcmb.gov.tr/tumSeriler` is a React shell; the data comes from the backend at
`https://evds3.tcmb.gov.tr/igmevdsms-dis/` (the old `evds2.../service/evds/` URLs redirect and are
dead). `categories/withDatagroups/type=json` is public — 154 categories, 678 live data groups — while
`serieList/` and the data endpoint need the `key` request header. "All series" is not literally
feasible, so the selection rule is written into `domain.evds_series.GROUPS` in three tiers: 0 = the demo
scenario cannot run without it (housing-loan rate `TP.KTF12`, TÜFE 2003=100, KFE, total and mortgaged
house sales for Türkiye + 81 provinces, TCMB funding cost), 1 = banking core that pairs with BDDK
tables, 2 = macro context for the tools. **EVDS3 has no dedicated policy-rate group**; `TP.APIFON4`
(weighted average funding cost) is the proxy, `TP.BISPOLFAIZ.TUR` the monthly BIS view.

Series names and frequencies are never hand-typed: they come from the archived `serieList`
responses, so the registry cannot drift from what TCMB publishes. What the registry *does* declare per
group is `semantics` (`stock` / `flow` / `rate` / `index`) and `monthly_rule` (`last` / `avg` / `sum`),
with per-series `overrides` for mixed groups (`bie_apifon` holds funding amounts and a rate). Data are
pulled at **native frequency** and aligned to months in `transform.macro.align_monthly`; the rule is a
catalogue column, not a URL parameter. Quarterly series land on Mar/Jun/Sep/Dec rows only.

Response facts the parser depends on (all verified against live calls): numbers are strings and missing
values `null`; `Tarih` is `dd-mm-yyyy` for daily/weekly, `yyyy-m` unpadded for monthly, `yyyy-Qn` for
quarterly; a series column is absent when nothing came back, which is why each archived file is an
envelope recording the series requested; a weekly calendar-year request also returns the first Friday of
the next year, so year files overlap by one observation and the parser asserts the overlap agrees.

**The unit is the one piece of metadata TCMB does not publish per series.** `BIRIMI` is a free-text
field on the DATA GROUP, and measured across the 44 registered groups it fails three ways: it is
*compound* (`bie_apifon` says `milyon TL ve yüzde` over eleven TL amounts and one percentage; the gold
group says `TL/kg, USD/ons, Euro/ons, TL/gr` over four differently quoted series), it is *not a unit*
(92 interest-rate series say `Ağırlıklı ortalama`, a method; the card group says `İşlem`), and it
*varies in spelling* (`%` / `Yüzde` / `% Değişim`, `endeks` / `Endeks` / `2003=100`). So the published
string is kept verbatim in `macro_series.unit_source` and `unit` is resolved per series by
`parsing.evds.resolve_unit`: the group's `BIRIMI` when it names exactly one unit, else the series' own
name, else the declared semantics. A series the registry **overrides** is excluded from the group's
unit by construction — an override says it does not behave like its group, so the group's unit does not
describe it either. That is what separates `TP.APIFON4` (%) from the funding amounts published beside
it, and the three labour-force ratios from the headcounts in `bie_yisgucu2`.
`validation.macro.check_unit_resolution` aborts the build if any series reaches the end of that chain
unresolved, and records how each unit was reached in `data_quality_report`.

Coverage is validated in two regimes (`validation.macro`): national tier-0 series must have every month
of 2021-01..2026-06 (build fails otherwise); everything else is checked against its own published
start/end and gaps are *reported* in `data_quality_report`, because small provinces publish no row for a
month with zero mortgaged sales. The derived series `DERIVED.IPOTEKLI_PAY.<region>` (mortgaged / total
sales, %) is computed at build so the agent never divides.

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

**Not every row is covered by a stated formula.** Table 12's sixteen risk-weight buckets carry none,
and their parent's formula addresses rows from the other direction, so nothing looked at them.
`validation.continuity.check_risk_weight_identity` closes that hole with a rule still read from the
labels: a child saying `Risk Ağırlığı %75 Olan Kalemler Toplamı` states its own weight, so
`parent == Σ(weight_i × child_i) + Σ(children stating no weight)`. Measured, it holds to within 0.073%
in all 67 months — which is also the evidence that `KDA Riskine Esas Tutar` sits *inside* item 13 and
that the parent's own formula therefore double-counts it.

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

**The unit is not the same across the bulletin, and it is not bin TL.** Each response states its
own unit in `Json.caption` (`Bilanço (milyon TL), Dönem:2026/6`). Measured across all 67 months:
**fourteen tables publish in milyon TL and only `05_sektorel_kredi_dagilimi` in bin TL** (15/16/17
publish ratios and counters, which state no monetary unit). `parsing.bddk_bulletin.caption_unit`
reads it per response and raises if it contradicts `Table.unit`, so a rebasing by BDDK fails the
build instead of rescaling every figure by 1000 in silence. Cross-checked: table 03's
`toplam_krediler` × 1000 equals table 05's `toplam/nakdi` in 64 of 67 months to within rounding.

Consequence for queries: `observations` (sectoral path, table 05) is bin TL while almost all of
`bulletin_observations` is milyon TL. **The same quantity read from the two tables differs by 1000x** —
never sum or compare them without rescaling, and always select `unit` alongside `value`.

**The response also publishes methodology notes (`Json.uyari`), and some of them change what a sum
means.** They land in `bulletin_footnotes` (one row per distinct text with the period span it covers).
Three are load-bearing: table 05's `Bankalara Kullandırılan Krediler` and table 08's repo-subject
securities are published *outside* their own tables' totals, and table 06 counts a customer using
several loan types once in the total column, so its counts are not additive across rows. No arithmetic
check can catch these — the published totals reconcile without those rows. The text is period-dependent:
table 12's note changed at 2021-11, the same month its capital-adequacy formula changed.

### The BDDK weekly bulletin

A different release from the monthly bulletin, not a finer grain of it: 9 tables, 201 line items,
observed on **Fridays** (the last business day when Friday is a holiday). It leads the monthly bulletin
by weeks, which is the whole reason to carry it.

**It answers a date range, so the archive is nine files, not nine-times-296.** `Temel Gösterim` renders
one table for one week — 2,664 requests for this corpus. `Gelişmiş Gösterim`
(`/BultenHaftalik/tr/Gelismis/GelismisRaporGetir`) takes `BaslangicTarihi`/`BitisTarihi` plus a list of
`Kalemler` ids and returns every week in the range at once, at full precision where the basic page
rounds to whole millions. So `bddk_haftalik_bulten/_raw/<tabloId>.json` is one table's entire history,
and a refresh re-fetches all nine. The session protocol is GET the advanced page → read
`__RequestVerificationToken` **from the report form specifically** (the page carries several and they
are not interchangeable) → POST with that token and its cookie.

**The envelope archives the question beside the answer, and that is load-bearing.** Table 297 carries
ten retired items whose labels are *identical* to the ten that replaced them, so a column cannot be
identified by its header — only by its position, which is the order `request.items` was sent in. The
parser asserts the two line up and refuses to guess otherwise. The archived fragment is the report
table only: the response is ~1 MB of which ~90% is navigation chrome.

**BDDK publishes the lifecycle here instead of leaving it to be measured.** The picker prefixes a
retired item with its old row code and suffixes it with `Sonlandırılma Tarihi= 10-09-2022`. 22 of the
201 items are retired and two tables were re-issued wholesale (291 at 2022-09-10, 297 at 2023-01-09).
Measured: not one retired item publishes a value after its stated date, so the check on that is hard,
never registerable. Introductions are *not* published and are measured into `KNOWN_WEEKLY_LIFECYCLES`
(KKM at 2022-02-18, the two overdraft memo lines at 2022-09-23).

**A retired item is a superseded definition, not a series that stopped — so retired and current rows
duplicate each other before the retirement date.** `validation.weekly.check_superseded_series` proves
the pairing every build by value rather than by label (table 291's replacement rows are worded
differently; table 297's are worded identically), and all 19 retired items that hold data agree with a
current item on every week both publish. They are kept rather than dropped because the superseded
definition is the only publisher of 2022-01-07 in table 297 — the replacements are backfilled to 2021
but miss that one week, which is registered in `KNOWN_WEEKLY_GAPS`. **Filter `retired_on IS NULL` for
any aggregate.**

**`entity_key` is BDDK's item id, not the label** — the one place the weekly path is sturdier than the
monthly one, which has nothing better than the label to key on.

**31 items say `(Bilgi)` in their own label**, meaning published for information and outside the
table's totals. The monthly bulletin buries the same fact in `bulletin_footnotes` where no arithmetic
check can reach it; here it is the `is_informational` column.

**Identities and parentage come from the labels, as in the monthly path.** `Toplam Krediler (2+10)`
addresses rows 2 and 10 counting active items down the picker; 59 of the 60 stated formulas hold
exactly in all 296 weeks and all three currency columns. The 60th is `Bankalardan Alacaklar (5+6)`,
which points at itself because BDDK inserted a row above it without renumbering — the arithmetic it
means, `(6+7)`, holds in all 296 weeks and is registered in `WEEKLY_FORMULA_OVERRIDES`. The published
text is never edited away; it stays verbatim in `entity_name`. Additive formulas also give
`parent_key`; subtraction (`(2-3)`, a net FX position) does not.

**Cross-validated against the monthly bulletin on the only dates where that is possible.** A weekly
observation lands on a month end 10 times in 296 weeks. On those dates five registered pairs
(`WEEKLY_MONTHLY_PAIRS`) agree to within 0.19% — the weekly figure is a flash estimate the monthly one
revises, so they agree closely without agreeing exactly, which is what makes the check sharp: an
ingestion error moves a figure by orders of magnitude. The ceiling is 0.5%. A sixth pair was measured
and **deliberately excluded**: weekly `Takipteki Alacaklar / a) Konut` against monthly
`Takipteki Konut Kredileri` diverges by a median 1.19% and a maximum 3.90% in every one of the 10
months, which is a scope difference between two definitions, not a revision. Admitting it would have
meant a tolerance wide enough to blind the check.

`currency` uses the **same vocabulary as `bulletin_observations`** — `TL` / `FX` / `total`, not
`tp` / `yp` — for the reason given under Domain invariants: a synonym is a filter that silently
returns nothing.

Most tables carry a **currency split** (`Tp` / `Yp` / `Toplam`), which `parsing.bddk_bulletin.split_measure`
peels off the measure column name into a separate `currency` dimension: `NakdiKrediToplam` becomes
metric `nakdi_kredi` with currency `total`. `taraf=10001` (whole banking sector) is fixed; that parameter
also offers a bank-group breakdown, not taken.

TBB Risk Merkezi is a source the team added; the brief does not ask for it. Keep it as an extra
cross-validation signal, but do not let it drive schema or roadmap decisions, and be prepared to drop it.

## The agent layer

    question -> router -> planner -> executor -> verifier -> composer -> answer

The model appears at exactly three points: classifying intent, emitting a typed plan, and writing
prose over numbers it did not compute. Everything else is Python. `agent/pipeline.py:run_turn` is
the single entry point and returns the API payload; `Agent` holds one `Session` per conversation.

**The plan DSL is the only language the model speaks.** Ten ops (`discover`, `fetch_series`,
`transform`, `analyze`, `find_periods`, `read_url`, `search`, `chart`, `ingest_external`,
`clear_table`) over a flat pydantic `Step`. Flat rather than a discriminated union on purpose:
guided-decoding backends vary in `$ref`/`anyOf` support, and a schema a deployment silently
mishandles fails with no error message.

**`clear_table` empties the session's `AnalysisArtifact`, never the lakehouse.** It replaces the
in-memory table with a fresh empty one and drops its citations -- there is no op, in this DSL or
anywhere else in the codebase, that can reach `data/lakehouse.duckdb` with anything but a
`read_only=True` connection, so a user asking to "clear" or "start over" can never touch the shared
database. The planner is told to reach for this only on an explicit ask ("temizle", "sil", "baştan
başla") -- never as a side effect of a normal follow-up.

**`ingest_external` adds a column; `read_url` only ever reads.** `read_url` extracts a document's
text/preview into `session.facts["documents"]` for the composer to read and cite -- it cannot become
a series, so nothing downstream (`transform`, `analyze`, `chart`) can touch it. `ingest_external`
resolves one column of an external Excel/CSV URL into a real, unit-labelled column on the current
turn's `AnalysisArtifact`, through `tools.external_series` (which shares `tools.web_url`'s fetch and
SSRF guard rather than forking a second one). It is **session-scoped by construction**: nothing here
writes to `data/lakehouse.duckdb`, which has exactly one writer (`backend.lakehouse.build`) and every
other reader open `read_only=True` -- a live turn writing an unseen, demo-day file into the shared
database on every question would risk corrupting or locking it for every other session. The column
disappears when the session does, exactly like a `transform`-derived one. Its unit and temporal
semantics are best-effort (an external file publishes neither the way the lakehouse's own sources do)
and the citation says so with `unit_verified: false`.

**MIA supports `response_format: json_schema`, and that changes the design.** Generation is
constrained to the schema token by token, so a syntactically invalid plan is unreachable — plan
validity stops being a prompting problem. Measured on this endpoint: 0.5s and schema-conformant.

**Qwen3 is a reasoning model, and reasoning tokens are billed against `max_tokens`.** A trivial
prompt costs 63 completion tokens with thinking and 2 without; the reference demo question consumed
all 1400 on reasoning and returned an *empty string*, which looks like a model failure and is a
budget error. `KloudeksClient` therefore defaults `think=False` and quadruples the budget when
thinking is on. Benchmarked, thinking is ~60x slower for no score gain — see `eval_results.md`.

**Discovery is the tool everything depends on**, and its ranking is pinned by tests. Lessons that
cost real debugging, each now a comment in `tools/lakehouse.py`:

- The candidate pool must be *every* matching row. An unordered `LIMIT 60` in SQL meant the correct
  series was often never scored, which made the ranking look mysteriously unstable.
- Aliases match on word boundaries, longest phrase wins, and a self-map (`"faiz" -> "faiz"`) is
  dropped — it adds no vocabulary and only triples the weight of a word the question already used.
- A long question is several short ones: `discover_concepts` splits on clause boundaries and
  searches each, because no weighting rescues one content word among thirty filler ones.
- Every corpus is guaranteed seats in the result. A rate-heavy question otherwise fills all of them
  with EVDS series and the planner never sees the BDDK row it was asked about.

**The artifact is state, not chat history.** The demo's turns 2 and 3 say "bozmadan" — later turns
extend the table rather than recompute it. `add_column` outer-joins so a new column can never
shorten the table, and a follow-up inherits the existing window (without that, adding the house
price index stretched a 60-row answer to 67 and disturbed exactly what the question protected).
A weekly series is resampled to the monthly grain on the way in, or it adds 296 index entries
instead of a column.

**Citations and the audit trail are by-products of execution, not a later reconstruction.** Each
`fetch_series` appends `{table, filters, value_column, unit, semantics, period range}`; each step
appends an `AuditStep`. A failing step records `ok=False` and execution continues, so one bad step
costs a column rather than the turn.

**The composer never sees the table** — only `verifier.quotable_numbers()`, a dict of figures the
tools already computed. Afterwards `unsupported_numbers()` re-reads the prose and flags any figure
matching nothing computed. (Turkish groups thousands with `.`, so "678.970" means 678970; reading
it as 678.97 made every correctly-quoted large figure look unsupported.)

`backend/eval/run_eval.py` benchmarks configurations of the one chat model MIA exposes against
SQL-computed gold numbers. Every `expect_values` entry in `scenarios.yaml` carries the `gold_sql`
that produced it — **never** the kick-off deck's chart, whose four labelled end-values do not
co-occur at any month in the published data and which is a shape to match, not a numeric target.

## Domain invariants (violating these produces silently wrong analysis)

**Temporal semantics are per-metric, not global.** The BDDK sectoral file is period-end outstanding
balances in bin TL (thousands) — a month-over-month change there is a net balance change (new lending −
repayments ± FX revaluation/write-offs), never "new lending". But the brief warns explicitly that other
BDDK releases are cumulative, some since inception and some year-to-date. Every metric must declare its
kind in the `metrics.temporal_semantics` column (`stock` / `flow` / `cumulative_ytd` / `cumulative_all`),
de-cumulation is a pipeline step, and no metric may reach the agent without that label set.

Measured across the bulletin, **exactly one table is cumulative: 02 `kar_zarar`**, the income statement,
which is year-to-date and resets every January. `transform.bulletin.decumulate` therefore adds
`value_flow` — the month's own contribution (the published figure in January, the first difference after
that) — and leaves `value` as published, because a year-to-date total is the right answer to "bu yılın
karı" and the wrong answer to "bu ayın karı"; the agent needs both, labelled. `value_flow` is NULL for
stocks and ratios on purpose: differencing a stock is a net balance change, a different concept already
served by the `growth` table. **Never difference `kar_zarar.value` — read `value_flow`**, and
`check_decumulation` fails the build if a cumulative series is missing one.

**`metric` is not always a measure.** Table 09 splits deposits by size and tables 03, 10 and 11 by
maturity, so in those four datasets the `metric` column carries bracket names rather than measures (the
`balance` / `toplam` columns excepted). `bulletin_metrics.metric_kind` says which is which — `measure`
/ `size_bucket` / `maturity_bucket` — and it is the column to read before summing across `metric`
values or presenting one as a quantity.

**Semantics and units are one vocabulary across all three series indexes.** `metrics`,
`bulletin_metrics` and `macro_series` are the agent's only way to learn what a column means, and a
synonym in one of them is a filter that silently returns nothing — `metrics` said `period_end_stock`
where the other two said `stock`, and `observations` said `bin_TL` where `bulletin_observations` said
`bin TL`, so a query for either word reached one table and not the other. Two tests now pin it:
`test_the_three_series_indexes_share_one_semantics_vocabulary` and
`test_the_two_bddk_paths_spell_their_shared_unit_the_same_way`. A new word in either dimension belongs
in the schema card at the same time it enters a table.

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

Adding raw periods changes numbers the tests pin, in three places — update them deliberately alongside a
refresh, and treat an unexpected failure as a data problem first, not a test problem:

- `tests/test_lakehouse.py` — golden fixtures and exact period counts (BDDK 67 months for
  2021-01..2026-07, TBB 54 for 2022-01..2026-06), plus a 54-month TBB continuity check.
- `tests/test_weekly.py::test_corpus_shape` — `EXPECTED_WEEKS` and the corpus edges
  (2021-01-08..2026-09-04); `test_only_ten_weeks_of_the_corpus_land_on_a_month_end` moves with it.
- `tests/test_evds.py::test_demo_series_cover_the_whole_window` — 66 months per tier-0 demo series, and
  `validation.macro`'s strict regime fails the build outright if a national tier-0 series gains a gap.

## Project stage

The lakehouse and schema card are implemented and validated for the full BDDK monthly bulletin (17
tables, 2021-01..2026-07), the BDDK weekly bulletin (9 tables, 2021-01-08..2026-09-04), the BDDK
FinTürk il-bazlı corpus (7 tables, 2021-Q1..2026-Q2), the TCMB EVDS macro corpus (44 groups,
2021-01..2026-07) and the TBB sectoral corpus — 21 lakehouse tables, 436 tests (15 of the
web-tools extension's skip without its containers).

The agent layer is implemented end to end against Kloudeks/MIA: `llm/client`, the plan DSL, the five
pipeline stages, and all six of the brief's tools (Lakehouse, Anomaly, Change Detection, Causality,
Web URL — text/PDF/Excel/CSV/HTML and, via `KloudeksClient.ocr` (the `Unlimited-OCR` model), images
— plus charts). **Web search** is still a stub the executor routes to but nothing answers without
`WEB_TOOLS_ENABLED=true` and the SearXNG/crawler containers running. A ninth op, `ingest_external`,
adds a column from an external Excel/CSV URL to the current session's table only (see "The agent
layer" above) — a team-added capability, not one of the brief's six named tools.
`backend/api/main.py` (FastAPI) and `frontend/` (React) exist. Not yet written: Docker packaging and
deployment — `README.md` says so directly, and the brief's "a live deployed system" requirement is
the largest open item.
Two tests pin the reference scenario from opposite ends:
`tests/test_evds.py::test_reference_scenario_table_is_producible_in_sql` proves the demo table is
producible from the lakehouse in SQL alone, and
`tests/test_agent.py::test_the_reference_scenario_runs_from_a_hand_written_plan` proves the execution
layer produces it with no model involved. If the second passes and a live turn still fails, the defect
is in the prompt or the plan, not in the data or the tools — which is the point of having both.

