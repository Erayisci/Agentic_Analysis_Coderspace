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
from typing import Any, Dict, Optional

from ..tools import transforms as T
from ..tools.anomaly import detect_anomalies, detect_anomalies_in_series
from ..tools.causality import analyze_causality
from ..tools.charts import build_chart, chart_summary
from ..tools.external_series import ingest_external_series
from ..tools.lakehouse import discover, fetch_series
from .planner import Plan, Step
from .state import AnalysisArtifact, AuditStep, ColumnLineage, Session

MAX_URL_CHARS = 6000


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
    if head.split("/")[0] in ("bulletin", "weekly", "macro", "finturk"):
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
        }[step.op]
        return handler(step, plan)

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
            "total" if source not in ("macro", "finturk") else None)
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
                                  dataset=best["dataset"] if best["source"] in ("bulletin", "finturk") else None,
                                  currency="total" if best["source"] not in ("macro", "finturk") else None,
                                  start=plan.start, end=plan.end)
            resolved_by = f" (key {key!r} not found; resolved to {best['key']!r} by discovery)"

        # The weekly bulletin is observed on Fridays. Joining it into a
        # month-indexed table raw does not add a column -- it adds 296 new index
        # entries and turns a 60-row answer into a 308-row one. Aggregate to the
        # monthly grain using the rule the series' own semantics imply.
        values, transform = series.values, None
        if source == "weekly":
            rule = {"stock": "last", "rate": "avg", "flow": "sum"}.get(series.temporal_semantics, "last")
            values = T.resample_to_monthly(series.values, rule)
            transform = f"resample_to_monthly({rule})"

        name = _column_name(step, re.sub(r"[^\w]+", "_", key))
        self.session.artifact.add_column(name, values, ColumnLineage(
            transform=transform,
            column=name, label=series.name, source=series.source, unit=series.unit,
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
        else:
            raise ValueError(f"unknown transform {step.operation!r}")
        return f"{name} = {artifact.lineage[name].transform}"

    def _analyze(self, step: Step, plan: Plan) -> str:
        column = self._resolve_column(step.column)
        lineage = self.session.artifact.lineage[column]
        if step.method == "anomaly":
            if lineage.source in ("bulletin", "weekly", "macro") and lineage.key:
                # Re-fetch the full, unwindowed history from the lakehouse
                # rather than the artifact's own (possibly plan.start/end
                # windowed, or too-short) column, so the rolling baseline has
                # more than what happens to be on screen to compare against.
                result = detect_anomalies(lineage.key, source=lineage.source,
                                          dataset=(lineage.citation.get("filters") or {}).get("dataset"))
            else:
                # A `transform`-derived or `ingest_external` column has no
                # lakehouse row to go back to -- the artifact's own values
                # are the only copy that exists, so score those directly.
                series = self.session.artifact.frame[column].dropna()
                result = detect_anomalies_in_series(
                    series,
                    describe={"source": lineage.source, "key": lineage.key, "name": lineage.label,
                             "unit": lineage.unit, "temporal_semantics": lineage.temporal_semantics,
                             "value_column": column},
                    citation=lineage.citation)
        elif step.method == "changepoint":
            result = self._changepoint(column)
        elif step.method == "causality":
            other = self._resolve_column(step.against or step.other_column,
                                         required="causality needs a second column")
            result = analyze_causality(
                cause=self.session.artifact.frame[other],
                effect=self.session.artifact.frame[column],
                cause_name=other, effect_name=column)
        else:
            raise ValueError(f"unknown analysis method {step.method!r}")
        self.session.facts.setdefault("analysis", {})[f"{step.method}:{column}"] = result
        return f"{step.method} on {column}: " + str(
            result.get("n_anomalies", result.get("n_breakpoints", result.get("verdict", "done"))))

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
        name = _column_name(step, re.sub(r"[^\w]+", "_", step.value_column))
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
        columns = [self._resolve_column(c) for c in step.columns] if step.columns else None
        artifact = self.session.artifact
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
        self.session.facts["chart"] = chart_summary(artifact, columns)
        self.session.facts["figure"] = figure
        return f"chart with {len(figure['data'])} trace(s)"

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
        if name in columns:
            return name
        lowered = {c.lower(): c for c in columns}
        if name.lower() in lowered:
            return lowered[name.lower()]
        cleaned = re.sub(r"[^\w]+", "_", name).strip("_").lower()
        if cleaned in lowered:
            return lowered[cleaned]
        partial = [c for c in columns if cleaned and (cleaned in c.lower() or c.lower() in cleaned)]
        if len(partial) == 1:
            return partial[0]
        raise ValueError(f"column {name!r} is not in the table; have {columns}")

    def _changepoint(self, column: str) -> Dict[str, Any]:
        """PELT change points on the column's own values."""
        import numpy as np
        import ruptures

        series = self.session.artifact.frame[column].dropna()
        if len(series) < 10:
            raise ValueError(f"{column!r} has {len(series)} points; need at least 10 for change detection")
        values = series.to_numpy(dtype=float).reshape(-1, 1)
        indices = ruptures.Pelt(model="rbf", min_size=3).fit(values).predict(pen=5.0)
        breaks = [i for i in indices if 0 < i < len(series)]
        segments = []
        previous = 0
        for cut in breaks + [len(series)]:
            block = series.iloc[previous:cut]
            segments.append({"from": block.index[0].strftime("%Y-%m"),
                             "to": block.index[-1].strftime("%Y-%m"),
                             "mean": round(float(np.mean(block)), 4), "n": int(len(block))})
            previous = cut
        return {"column": column, "unit": self.session.artifact.lineage[column].unit,
                "method": "PELT (rbf, pen=5)", "n_breakpoints": len(breaks),
                "breakpoints": [series.index[i].strftime("%Y-%m") for i in breaks],
                "segments": segments}
