# KKB Agentic Data Analytics

A deterministic data pipeline that turns Turkish banking-regulator publications into a queryable DuckDB
lakehouse, built so an LLM agent never has to do arithmetic or schema reasoning itself.

Submission for the **KKB Hackathon 2026 — Lakehouse Agent Builder and Data Analytics**.

Current state: the ingestion, validation and lakehouse layers are implemented and tested. The agent,
its tools and the API are designed in [`Launch.MD`](Launch.MD) but not yet written.

---

## Quick start

Requires Python 3.10+. Nothing else — no network access, no database server.

```bash
git clone <repo-url> && cd Agentic_Analysis_Coderspace

python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

python -m backend.ingestion.bddk_bulletin --from-cache  # raw workbooks from the archived responses
python -m backend.lakehouse.build                       # parse -> validate -> parquet + duckdb
pytest -q                                               # 57 tests
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
| TCMB EVDS (`evds3.tcmb.gov.tr`) | ❌ not acquired — **highest priority**, the demo scenario needs it |
| BDDK Haftalık Bülten | ❌ not acquired |
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
| `reconciliation_monitor` | BDDK vs TBB divergence with a mean ± 3σ band per series |
| `data_quality_report`, `bulletin_lifecycle_report` | Validation evidence, persisted and quotable |

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
- **Every value is a period-end stock.** A month-over-month change is a net balance change (new lending
  − repayments ± FX revaluation), never "new lending".

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
| `data/analytics/schema_card.md` | Generated, agent-facing description of the lakehouse |
