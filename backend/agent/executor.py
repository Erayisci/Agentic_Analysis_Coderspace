"""Runs a validated Plan. No model is called here.

This is where the system's central claim is enforced: every number in an answer
is produced by Python, from the lakehouse, with its unit and provenance
recorded as it is computed. The executor appends a citation for each series it
touches and an audit row for each step it runs, so the trust layer costs
nothing extra -- it is a by-product of execution rather than a later
reconstruction.

Two tolerances are deliberate. The planner's step model is flat, so a model
will sometimes fill fields an op does not use; those are ignored rather than
rejected. And a model asked to copy a key from a discovery result will
sometimes copy the whole `source/dataset/key` line; `_normalise_key` takes the
last segment rather than failing, because a plan that is 95% right should
produce an answer, not an error message.

A failing step does not end the turn. It records `ok=False` with a usable
message and execution continues, so one bad step costs a column rather than
the whole question -- the demo-day failure mode that matters most.
"""
import re
import time
from typing import Optional

from ..tools import transforms as T
from ..tools.anomaly import detect_anomalies_in_series
from ..tools.causality import granger_both_directions
from ..tools.change_detection import detect_change_points
from ..tools.charts import build_chart, chart_summary, mark_breaks
from ..tools.external_series import ingest_external_series
from ..tools.lakehouse import discover, fetch_series, footnotes
from ..tools.series import load_series
from .planner import Plan, Step
from .state import AnalysisArtifact, AuditStep, ColumnLineage, Session

MAX_URL_CHARS = 6000

LAKEHOUSE_SOURCES = ("bulletin", "weekly", "macro", "finturk")
# Sources whose fact table keys a series by (dataset, key) rather than by the
# key alone, so `dataset` has to travel with the key on every fetch.
DATASET_SOURCES = ("bulletin", "finturk")
# Sources with no currency dimension: an EVDS series is one number a month,
# and FinTurk publishes a province split instead of a TL/FX one.
NO_CURRENCY_SOURCES = ("macro", "finturk")


def _monthly_rule(semantics: Optional[str]) -> str:
    """How a weekly series collapses onto months: what its semantics imply."""
    return {"stock": "last", "rate": "avg", "flow": "sum"}.get(semantics or "", "last")


def _normalise_key(key: str) -> str:
    """'bulletin/tuketici_kredileri/tuketici_kredileri_konut' -> the key itself.

    A macro series_code contains dots but never slashes; a bulletin entity_key
    may contain one slash as a parent/child qualifier. Only strip a prefix that
    looks like a source or dataset label.
    """
    key = key.strip()
    if "/" not in key:
        return key
    head, _, tail = key.rpartition("/")
    if head.split("/")[0] in LAKEHOUSE_SOURCES:
        return tail
    return key


def _column_name(step: Step, fallback: str) -> str:
    """A safe identifier for a new column."""
    raw = step.as_name or step.column or fallback
    return re.sub(r"[^\w]+", "_", str(raw)).strip("_") or fallback


class Executor:
    """Executes plan steps against a Session, accumulating an artifact."""

    def __init__(self, session: Session, url_reader=None, web_search=None):
        self.session = session
        # Injected so a test -- and the eval harness -- never touches the network.
        self._read_url = url_reader
        self._search = web_search

    # -- entry point -------------------------------------------------------

    def run(self, plan: Plan) -> Session:
        session = self.session
        for index, step in enumerate(plan.steps, start=1):
            started = time.perf_counter()
            try:
                detail = self._dispatch(step, plan)
                ok = True
            except Exception as exc:                                  # noqa: BLE001
                # Deliberately broad: a tool raising anything must cost one step,
                # never the turn. The message reaches the composer as a fact.
                detail = f"{type(exc).__name__}: {exc}"
                ok = False
            session.audit.append(AuditStep(
                index=index, op=step.op, arguments=step.arguments(), ok=ok,
                detail=str(detail)[:500], seconds=time.perf_counter() - started))

        if plan.start or plan.end:
            T.window(session.artifact, plan.start, plan.end)
        session.facts["table"] = {
            "n_rows": int(len(session.artifact.frame)),
            "columns": session.artifact.column_names(),
            "units": session.artifact.units(),
            "summary": session.artifact.summary(),
        }
        session.facts["failed_steps"] = [a.to_dict() for a in session.audit if not a.ok]
        return session

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self, step: Step, plan: Plan) -> str:
        handler = {
            "discover": self._discover, "fetch_series": self._fetch, "transform": self._transform,
            "analyze": self._analyze, "find_periods": self._find_periods,
            "read_url": self._read_url_step, "search": self._search_step, "chart": self._chart,
            "ingest_external": self._ingest_external, "clear_table": self._clear_table,
            "footnotes": self._footnotes,
        }[step.op]
        return handler(step, plan)

    def _footnotes(self, step: Step, plan: Plan) -> str:
        """BDDK's methodology notes for the named table, or for every
        bulletin table the current columns come from."""
        datasets = [step.dataset] if step.dataset else sorted({
            (line.citation.get("filters") or {}).get("dataset")
            for line in self.session.artifact.lineage.values()
            if line.source == "bulletin" and (line.citation.get("filters") or {}).get("dataset")})
        if not datasets:
            raise ValueError("footnotes needs a dataset, or a table with a bulletin column in it")
        found = []
        for dataset in datasets:
            result = footnotes(dataset)
            found.append(result)
            self.session.cite(result["citation"])
        self.session.facts.setdefault("footnotes", []).extend(found)
        return ", ".join(f"{r['dataset']}: {r['n_notes']} note(s)" for r in found)

    # -- steps -------------------------------------------------------------

    def _discover(self, step: Step, plan: Plan) -> str:
        result = discover(step.query, source=step.source)
        self.session.facts.setdefault("discovery", []).append(result)
        top = result["candidates"][:3]
        return f"{result['n_candidates']} candidate(s): " + ", ".join(
            f"{c['source']}:{c['key']}" for c in top) if top else "no candidates"

    def _fetch(self, step: Step, plan: Plan) -> str:
        key = _normalise_key(step.key)
        source = step.source or ("macro" if key.upper().startswith(("TP.", "DERIVED.")) else "bulletin")
        currency = step.currency if step.currency is not None else (
            "total" if source not in NO_CURRENCY_SOURCES else None)
        resolved_by = ""
        try:
            series = fetch_series(key, source=source, dataset=step.dataset, currency=currency,
                                  metric=step.metric, start=plan.start, end=plan.end,
                                  province=step.province)
        except (ValueError, KeyError) as exc:
            # A key the model invented is the most common plan defect -- it wrote
            # TP.TUFE where the corpus publishes TP.GENENDEKS.T1. Discovery already
            # knows the real key, so resolve it here rather than losing the column.
            candidates = discover(step.as_name or key, source=source, limit=1)["candidates"] \
                or discover(step.as_name or key, limit=1)["candidates"]
            if not candidates:
                raise ValueError(f"{exc}; discovery found no alternative for {key!r}") from exc
            best = candidates[0]
            series = fetch_series(best["key"], source=best["source"],
                                  dataset=best["dataset"] if best["source"] in DATASET_SOURCES else None,
                                  currency=best.get("currency") or (
                                      "total" if best["source"] not in NO_CURRENCY_SOURCES else None),
                                  start=plan.start, end=plan.end,
                                  province=step.province if best["source"] == "finturk" else None)
            resolved_by = f" (key {key!r} not found; resolved to {best['key']!r} by discovery)"

        # The weekly bulletin is observed on Fridays. Joining it into a
        # month-indexed table raw does not add a column -- it adds 296 new index
        # entries and turns a 60-row answer into a 308-row one. Aggregate to the
        # monthly grain using the rule the series' own semantics imply.
        values, transform = series.values, None
        if source == "weekly":
            rule = _monthly_rule(series.temporal_semantics)
            values = T.resample_to_monthly(series.values, rule)
            transform = f"resample_to_monthly({rule})"

        name = self.session.touch_column(_column_name(step, re.sub(r"[^\w]+", "_", key)))
        # Three slices of one line share a name; the label says which slice
        # this is, or the composer cannot tell the FX column from the TL one.
        label = series.name + {"FX": " (YP)", "TL": " (TL)"}.get(currency or "", "")
        self.session.artifact.add_column(name, values, ColumnLineage(
            transform=transform,
            column=name, label=label, source=series.source, unit=series.unit,
            temporal_semantics=series.temporal_semantics, key=series.key,
            citation=series.citation()))
        self.session.cite(series.citation())
        return (f"{name}: {len(values)} points, {series.unit}, "
                f"{series.temporal_semantics}, {series.period_start}..{series.period_end}{resolved_by}")

    def _transform(self, step: Step, plan: Plan) -> str:
        artifact = self.session.artifact
        column = self._resolve_column(step.column)
        if step.operation == "index_to_base":
            name = T.index_to_base(artifact, column, step.base_period or plan.start, step.as_name)
        elif step.operation == "deflate":
            deflator = self._resolve_column(step.other_column, required="deflate needs other_column")
            name = T.deflate(artifact, column, deflator, step.base_period or plan.start, step.as_name)
        elif step.operation == "change":
            name = T.change(artifact, column, step.periods or 1, step.as_name)
        elif step.operation == "ratio":
            denominator = self._resolve_column(step.other_column, required="ratio needs other_column")
            name = T.ratio(artifact, column, denominator, step.as_name)
        elif step.operation == "in_usd":
            rate = self._resolve_column(step.other_column, required="in_usd needs other_column (USD/TRY)")
            name = T.in_usd(artifact, column, rate, step.as_name)
        else:
            raise ValueError(f"unknown transform {step.operation!r}")
        self.session.touch_column(name)
        return f"{name} = {artifact.lineage[name].transform}"

    def _full_history(self, column: str):
        """(series, describe, citation) for an analysis that needs a baseline.

        A lakehouse-backed column is re-read in full, unwindowed, with the
        same currency/metric filters the column was fetched with -- the
        artifact's own copy may be windowed to 60 months, and a rolling
        baseline wants more than what happens to be on screen. A weekly
        series is put on the monthly grain first, with the same rule `_fetch`
        used, so `window=12` means twelve months and no two observations
        share a "%Y-%m" label. A derived or external column has no lakehouse
        row to go back to; its artifact values are the only copy.
        """
        lineage = self.session.artifact.lineage[column]
        filters = lineage.citation.get("filters") or {}
        if lineage.source in LAKEHOUSE_SOURCES and lineage.key:
            # A FinTurk column's citation names its province, or none for the
            # national sum; the re-read keeps the same slice. (Quarterly, so
            # a `window=12` there is twelve quarters -- stated by the tool's
            # own frequency inference, not silently treated as months.)
            loaded = load_series(lineage.key, source=lineage.source, dataset=filters.get("dataset"),
                                 currency=filters.get("currency"), metric=filters.get("metric"),
                                 province=filters.get("province"))
            values = loaded.values
            if lineage.source == "weekly":
                values = T.resample_to_monthly(values, _monthly_rule(loaded.temporal_semantics))
            return values, loaded.describe(), loaded.citation()
        describe = {"source": lineage.source, "key": lineage.key, "name": lineage.label,
                    "unit": lineage.unit, "temporal_semantics": lineage.temporal_semantics,
                    "value_column": column}
        return self.session.artifact.frame[column].dropna(), describe, lineage.citation

    def _second_column(self, step: Step, column: str, prefer: Optional[str] = None) -> tuple:
        """The `against` column, or a deterministic stand-in when the plan
        named none: a plan that is 95% right should still produce an answer,
        and the result says the choice was automatic."""
        named = step.against or step.other_column
        if named:
            try:
                return self._resolve_column(named), False
            except ValueError:
                pass
        artifact = self.session.artifact
        others = [c for c in artifact.column_names() if c != column]
        if prefer == "index":
            others = [c for c in others if artifact.lineage[c].temporal_semantics == "index"] or others
        if not others:
            raise ValueError(f"{step.method} needs a second column beside {column!r}; the table has none")
        return self.session.touch_column(others[0]), True

    def _analyze(self, step: Step, plan: Plan) -> str:
        column = self._resolve_column(step.column)
        artifact = self.session.artifact
        against, auto = None, False
        if step.method == "anomaly":
            series, describe, citation = self._full_history(column)
            result = detect_anomalies_in_series(series, describe, citation, window=step.window or 12)
            result["inputs"] = [column]
        elif step.method == "changepoint":
            # Same history policy as the anomaly tool: the column's full
            # lakehouse history, so a break just before the window's start is
            # not mistaken for the level the window opens at. The plan may
            # name `kind` (volatility for a stability question) and
            # `sensitivity`; unset, the tool picks level for rates/ratios and
            # trend for balances from the column's own semantics.
            series, describe, citation = self._full_history(column)
            series = series.rename(column)
            result = detect_change_points(
                series, kind=step.kind or "auto", sensitivity=step.sensitivity or "medium",
                temporal_semantics=describe.get("temporal_semantics"), unit=describe.get("unit"),
                name=describe.get("name") or column)
            result = {**describe, "citation": citation, **result, "inputs": [column]}
        elif step.method == "causality":
            against, auto = self._second_column(step, column)
            describe = {c: {"name": artifact.lineage[c].label, "unit": artifact.lineage[c].unit,
                            "temporal_semantics": artifact.lineage[c].temporal_semantics}
                        for c in (column, against)}
            result = granger_both_directions(artifact.frame, column, against, describe=describe)
        elif step.method == "decompose":
            against, auto = self._second_column(step, column, prefer="index")
            result = T.decompose_growth(artifact, column, against)
        else:
            raise ValueError(f"unknown analysis method {step.method!r}")
        if auto:
            result["against_auto"] = True
        # The second column is part of the key so two analyses of one target
        # against different partners do not overwrite each other.
        key = f"{step.method}:{column}" + (f"~{against}" if against else "")
        self.session.facts.setdefault("analysis", {})[key] = result
        headline = result.get("n_anomalies", result.get("n_breakpoints", result.get("verdict", "done")))
        return (f"{step.method} on {column}" + (f" vs {against}" if against else "")
                + (" (against chosen automatically)" if auto else "") + f": {headline}")

    def _find_periods(self, step: Step, plan: Plan) -> str:
        result = T.find_periods(
            self.session.artifact, self._resolve_column(step.column),
            direction=step.direction or "down",
            against=self._resolve_column(step.against) if step.against else None,
            against_direction=step.against_direction or "up")
        self.session.facts.setdefault("find_periods", []).append(result)
        return f"{result['n_periods']} matching period(s)"

    def _read_url_step(self, step: Step, plan: Plan) -> str:
        if self._read_url is None:
            raise RuntimeError("no URL reader configured for this executor")
        result = self._read_url(step.url)
        if isinstance(result, dict) and isinstance(result.get("text"), str):
            result = {**result, "text": result["text"][:MAX_URL_CHARS]}
        self.session.facts.setdefault("documents", []).append(result)
        self.session.cite({"source": "url", "url": step.url, "kind": result.get("kind")})
        return f"read {step.url} ({result.get('kind')})"

    def _ingest_external(self, step: Step, plan: Plan) -> str:
        """Add one column of an external Excel/CSV file to THIS SESSION'S
        table only -- see tools.external_series' module docstring for why
        this never touches data/lakehouse.duckdb."""
        series = ingest_external_series(
            step.url, step.value_column, period_column=step.period_column,
            sheet=step.sheet, unit=step.unit, monthly_rule=step.monthly_rule or "last")
        name = self.session.touch_column(_column_name(step, re.sub(r"[^\w]+", "_", step.value_column)))
        self.session.artifact.add_column(name, series.values, ColumnLineage(
            column=name, label=series.value_column, source=series.source, unit=series.unit,
            temporal_semantics=series.temporal_semantics, key=series.key,
            transform=f"ingest_external(period_column={series.period_column!r}, monthly_rule={series.monthly_rule!r})",
            citation=series.citation()))
        self.session.cite(series.citation())
        return (f"{name}: {len(series.values)} points from {step.url} "
                f"(value_column={series.value_column!r}, period_column={series.period_column!r}, "
                f"unit={series.unit!r} unverified"
                + (f", {series.n_dropped_rows} row(s) dropped" if series.n_dropped_rows else "") + ")")

    def _search_step(self, step: Step, plan: Plan) -> str:
        if self._search is None:
            raise RuntimeError("no web search backend configured for this executor")
        result = self._search(step.query)
        self.session.facts.setdefault("search", []).append(result)
        for hit in (result.get("results") or [])[:5]:
            self.session.cite({"source": "web", "url": hit.get("url"), "title": hit.get("title")})
        return f"{len(result.get('results') or [])} result(s)"

    def _chart(self, step: Step, plan: Plan) -> str:
        # A chart step runs last, so the columns this turn touched are already
        # known: a plan that names none draws the turn's own series rather
        # than every column the conversation has ever accumulated. The same
        # rule the table follows (`Session.focus`), one step earlier.
        artifact = self.session.artifact
        if step.columns:
            columns = [self._resolve_column(c) for c in step.columns]
        else:
            scope = set(self.session.turn_columns)
            if self.session.facts.get("is_followup"):
                scope |= set(self.session.visible_columns)          # "ayni grafige ekle"
            columns = [c for c in artifact.column_names() if c in scope] or None
        try:
            figure = build_chart(artifact, columns, step.title)
        except ValueError as exc:
            # Too many units for one chart: fall back to the columns that share
            # the two most common ones rather than returning no chart at all.
            if "different units" not in str(exc):
                raise
            groups = sorted(T.columns_sharing_unit(artifact), key=len, reverse=True)[:2]
            columns = [c for group in groups for c in group]
            figure = build_chart(artifact, columns, step.title)
        # If change detection already ran on a charted column, draw its breaks
        # on the chart so the reader sees where the regimes change.
        charted = columns or artifact.column_names()
        analysis = self.session.facts.get("analysis", {})
        breaks_by_column = {c: analysis[f"changepoint:{c}"]["breaks"]
                            for c in charted if f"changepoint:{c}" in analysis
                            and analysis[f"changepoint:{c}"].get("breaks")}
        if breaks_by_column:
            figure = mark_breaks(figure, breaks_by_column)
        self.session.facts["chart"] = chart_summary(artifact, columns)
        self.session.facts["figure"] = figure
        marks = sum(len(b) for b in breaks_by_column.values())
        return f"chart with {len(figure['data'])} trace(s)" + (f", {marks} break marker(s)" if marks else "")

    def _clear_table(self, step: Step, plan: Plan) -> str:
        """Empty this session's working table -- and only this session's.

        Replaces the in-memory AnalysisArtifact with a fresh, empty one and
        drops the citations that described its (now gone) columns. This
        touches nothing under data/: the lakehouse has exactly one writer
        (backend.lakehouse.build, run offline) and every reader here -- this
        included -- only ever opens it read_only. There is no code path from
        this op, or any other, to a write against lakehouse.duckdb.
        """
        n_columns = len(self.session.artifact.column_names())
        self.session.artifact = AnalysisArtifact()
        self.session.citations = []
        self.session.turn_columns = []
        self.session.visible_columns = []
        return f"cleared {n_columns} column(s); table is now empty"

    # -- helpers -----------------------------------------------------------

    def _resolve_column(self, name: Optional[str], required: Optional[str] = None) -> str:
        """Map a model-supplied column name onto a real one.

        The planner sees the table's column names, but may return a key, a
        label, or a near-miss. Exact match, then case-insensitive, then a
        containment match -- and only then an error naming what exists.
        """
        if name is None:
            raise ValueError(required or "this step needs a column")
        columns = self.session.artifact.column_names()
        # A resolved column is one this turn read, so it belongs in the
        # turn's scope beside the columns the turn produced -- a deflated
        # series with its deflator out of the table explains nothing.
        touch = self.session.touch_column
        if name in columns:
            return touch(name)
        lowered = {c.lower(): c for c in columns}
        if name.lower() in lowered:
            return touch(lowered[name.lower()])
        cleaned = re.sub(r"[^\w]+", "_", name).strip("_").lower()
        if cleaned in lowered:
            return touch(lowered[cleaned])
        partial = [c for c in columns if cleaned and (cleaned in c.lower() or c.lower() in cleaned)]
        if len(partial) == 1:
            return touch(partial[0])
        raise ValueError(f"column {name!r} is not in the table; have {columns}")
