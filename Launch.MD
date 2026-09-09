# KKB Lakehouse Agent Builder — Architecture & Execution Blueprint

## 1. Objective

An end-to-end agentic data analytics platform for the KKB Lakehouse Hackathon. It ingests Turkish
banking and macroeconomic data for **2021-01 → 2026-06**, normalises structural inconsistencies, plans
and executes multi-step analysis, verifies results against external sources, and returns a decision-support
artifact (report, chart, or table) with citations.

The distinguishing property is not retrieval. It is that the agent **plans, selects tools, computes
programmatically, and proves its numbers** — the LLM synthesises and explains, and never does arithmetic.

## 2. Binding constraints

| Constraint | Consequence for this design |
|---|---|
| No third-party LLM APIs; open-weight models via **Kloudeks** only | One `llm_client` abstraction over an OpenAI-compatible endpoint; no vendor SDK in tool code. Model choice among Qwen / Mistral / gpt-oss / Kimi. |
| Open-source libraries expected: pypdf, Pandas, DuckDB, Plotly, LanceDB | LanceDB for vectors. Plotly for every chart. No hosted search/RAG service — the web-search tool needs an open backend (SearXNG or DuckDuckGo). |
| Python server-side; frontend free | FastAPI backend, any frontend framework. |
| Live deployed system on demo day, unseen inputs | Deployment is a phase, not an afterthought. Docker + a reachable URL. |
| Private GitHub repo shared with KKB, plus diagrams, docs, DB definitions | `schema_card.md` is agent-facing; a separate human-readable data dictionary is a deliverable. |

Smaller open-weight models are markedly weaker at free-form tool calling than frontier models. Design
for that: narrow tool schemas, explicit routing edges, validation on every tool argument, and a
deterministic fallback path rather than an open ReAct loop that can wander.

## 3. Stack

| Layer | Technology | Responsibility |
|---|---|---|
| Storage & lakehouse | DuckDB / Parquet, LanceDB | Analytical querying of monthly and higher-frequency series; vector store for document retrieval. |
| Ingestion & preprocessing | Pandas, openpyxl, pypdf | Schema alignment, frequency harmonisation, de-cumulation, unit standardisation. |
| Agent orchestration | LangGraph | Stateful cyclic workflows, conditional routing, persistent analysis state across turns. |
| Model access | Kloudeks (OpenAI-compatible), open-weight LLM + VLM | Intent parsing, tool selection, synthesis. A vision-capable model is required for the image path of the URL tool. |
| Execution & analysis | Text-to-SQL, sandboxed Python, statsmodels, ruptures | Aggregation, anomaly detection, causality testing, change-point detection. |
| Trust & delivery | FastAPI, Plotly, Docker | Cited outputs, audit trail, deployed endpoints and UI. |

## 4. Data pipeline

### 4.1 Sources

Required by the brief: **BDDK** Aylık Bülten, Haftalık Bülten, FinTürk (İllere Göre); **TCMB EVDS**
(all series). TBB Risk Merkezi is a supplementary source the team added — useful for cross-validation,
not required, and droppable.

All 17 monthly-bulletin tables are parsed into the lakehouse for 2021-01..2026-07 -- 135,513
observations in `bulletin_observations` -- keyed on normalised row labels qualified by parent, with a
continuity check that aborts the build when a series appears or disappears without being registered.
Housing loans live in Tüketici Kredileri, not in the sectoral table.

EVDS is the critical path: mortgage and policy rates, TÜFE, the house price index and mortgaged-sale
share all live there, and the reference demo scenario is unanswerable without them.

### 4.2 Structural preprocessing

- **Frequency alignment.** EVDS and the BDDK weekly bulletin arrive daily/weekly; the analytical grain is
  monthly. Aggregation rule is per-metric — period-end for stocks, mean for rates, sum for flows — and
  recorded, never inferred at query time.
- **De-cumulation.** The brief warns that some BDDK series are cumulative since inception and others
  year-to-date. Each metric carries `temporal_semantics` (`stock` / `flow` / `cumulative_ytd` /
  `cumulative_all`); cumulative series are differenced into flows at build time. An unlabelled metric is
  a build failure, not a default.
- **Unit normalisation.** Detect and scale monetary multipliers (thousands vs millions of TL) across
  releases; carry the unit in the catalog.
- **Entity generalisation.** Done for the bulletin path: `bulletin_observations` carries
  `entity_type` / `entity_key` / `entity_name` / `parent_key` plus a `currency` dimension
  (`Tp`/`Yp`/`Toplam`), one table for all 17 sources. Migrating the sectoral path onto the same schema
  is still open and touches `domain/canonical.py`, the crosswalk and the reconciliation monitor.
- **Geographic keys.** FinTürk adds a province dimension; it needs its own canonical key, mirroring how
  TBB sector names are slugified rather than trusting an unstable numeric code.

### 4.3 Validation

Validation aborts the build rather than warning:

- **Arithmetic identities.** Aggregates equal the sum of their sub-categories, respecting the BDDK
  sector graph (see `CLAUDE.md` for the double-counting traps). Most bulletin tables state their own
  identities inside the row labels — `Tüketici Kredileri (2+3+4)` — so these rules are parsed from the
  source rather than hand-curated.
- **Statistical bounds.** Z-score and IQR checks flag structural breaks and entry errors for review.
- **Cross-source bands.** `reconciliation_monitor` tracks each cross-source pair against a mean ± 3σ
  band, so a divergence regime change is distinguishable from a known methodology gap.

The passing report persists as the `data_quality_report` table and is quotable as evidence.

## 5. Agentic engine

A LangGraph state machine, not a free-running loop:

1. **Intent analysis** — decompose the request into sub-tasks and identify the target series.
2. **Tool routing** — dispatch to the tools below, with validated arguments.
3. **Verification** — cross-check computed results against external evidence; suppress unsupported claims.
4. **Output generation** — executive summary plus the requested artifact, with sources and audit trail.

### 5.1 Required tools

The brief names six. All six are in scope:

| Tool | Function |
|---|---|
| **Lakehouse** | Natural-language discovery, query, join and analysis over the DuckDB lakehouse. |
| **Web Search** | Enrich analysis with external information via an open search backend. |
| **Web URL Agent** | Read a URL given in the prompt and extract meaning from **PDF, Excel, image or text**. The image path requires a VLM. |
| **Anomaly** | Detect outliers and deviations from expected behaviour. |
| **Causality** | Test whether an observed relationship carries causal direction (Granger, lead-lag). |
| **Change Detection** | Locate trend, level and regime breaks in time series (PELT / CUSUM via `ruptures`). |

Demo day guarantees URL-driven questions. The Borsa İstanbul precious-metals PDF and the XTUMY index
page are named in the brief — both belong in the test fixtures now, not on demo day.

### 5.2 Conversational analysis state

The reference scenario is three chained turns: build a table, then *"without disturbing the table"*
deflate it by inflation, then *"without disturbing it at all"* add the house price index as a new column.

This is a state design requirement, not a chat-history requirement. The produced table is a **persistent
analysis artifact**: later turns extend it column-wise and re-render it, rather than recomputing a new
table that happens to look similar. Inflation adjustment (TÜFE real terms) and indexing (2021-01 = 100)
are first-class transformations over that artifact.

## 6. Risks and mitigations

- **LLM arithmetic.** Delegate every calculation to SQL or sandboxed Python; the model only explains.
- **Weak tool calling in open-weight models.** Narrow schemas, deterministic routing, argument validation,
  fallback paths. Evaluate candidate models on tool-call reliability early — the choice is not cosmetic.
- **Context drift in multi-step queries.** Explicit LangGraph state and conditional edges; persist the
  analysis artifact outside the prompt.
- **False anomaly alerts.** Turkish macro data contains genuine structural breaks. Anomaly output is
  contextualised against historical windows and, where possible, external confirmation before being
  reported as an anomaly.
- **Demo-day fragility.** Unseen inputs against a live system: cache aggressively, degrade gracefully,
  and make every tool return a usable error instead of failing the turn.

## 7. Roadmap

```text
Phase 1  BDDK sectoral corpus, validation, DuckDB lakehouse                  [done]
Phase 1b All 17 bulletin tables parsed and validated, 2021-01..2026-07        [done]
Phase 2  EVDS ingest with frequency alignment, de-cumulation and unit
         normalisation; migrate the sectoral path onto the entity schema
Phase 3  Kloudeks LLM abstraction, model selection, LangGraph skeleton
Phase 4  Tools: Lakehouse, Web URL, Anomaly  ->  end-to-end reference scenario
Phase 5  Remaining BDDK feeds (weekly bulletin, FinTürk); Causality, Change Detection
Phase 6  Trust layer, FastAPI + frontend, Docker, live deployment
```

Milestones: online build period ends **20 September**; the top-10 selection is announced **5 October**
and is judged on what exists at that point; the final demo is **17 October**. Phases 2–4 and a deployable
slice of Phase 6 are what the first deadline actually selects on.
