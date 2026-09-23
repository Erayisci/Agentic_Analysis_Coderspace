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
from typing import Any, Dict, FrozenSet, List, Optional

import pandas as pd

from ..core.labels import slugify
from ..tools import transforms as T
from ..tools.anomaly import detect_anomalies_in_series
from ..tools.causality import granger_both_directions
from ..tools.change_detection import detect_change_points
from ..tools.charts import PieError, build_chart, chart_summary, mark_breaks, rebase_for_chart, resolve_pie

BAR_CHART_WORDS = re.compile(r"(çubuk|cubuk|s[üu]tun\s+grafi|bar\s*(chart|graph|grafi)|\bbar\b)", re.I)
# The chart step already exists (the router decided a chart was asked for),
# so here "pasta" alone is enough. Not "pastasindan": `\b` after the optional
# possessive rejects the idiom's longer suffix.
PIE_CHART_WORDS = re.compile(r"(\bpasta(s[ıi]|y[ıi]|n[ıi])?\b|\bpie\b)", re.I)
from ..ingestion.external import ingest_url
from ..tools.external_series import ingest_external_series
from ..tools.lakehouse import candidate_grain, discover, fetch_series, footnotes
from ..tools.series import SeriesResult, load_series
from .planner import Plan, Step
from .state import AnalysisArtifact, AuditStep, ColumnLineage, Session

MAX_URL_CHARS = 6000

# "external" is the lakehouse's external zone (backend.ingestion.external): a
# source landed at runtime has a real row behind it in the external_* views,
# so it is re-readable like any other lakehouse series.
LAKEHOUSE_SOURCES = ("bulletin", "weekly", "macro", "finturk", "external")
# Sources whose fact table keys a series by (dataset, key) rather than by the
# key alone, so `dataset` has to travel with the key on every fetch.
DATASET_SOURCES = ("bulletin", "finturk")
# Sources with no currency dimension: an EVDS series is one number a month,
# FinTurk publishes a province split instead of a TL/FX one, and an external
# file publishes whatever it publishes -- one column, one series.
NO_CURRENCY_SOURCES = ("macro", "finturk", "external")

# `Step` fields that only mean something for a subset of sources, because they
# select a dimension that source's own fact table actually publishes. Naming
# one is a real narrowing request, not an optional hint -- so a `source` guess
# incompatible with a filter the step set is a plan defect to correct or
# reject, never a filter to quietly drop. `province` is the one entry today
# (only `finturk_observations` carries a province column). Measured live: a
# model that named province="ANKARA" but wrote source="bulletin" for a key
# that also happens to be a valid *national* bulletin entity_key raised no
# error at all -- `load_series` for source="bulletin" just ignores `province`
# -- so nothing ever ran the fallback below, and the Turkiye-wide bulletin
# total reached the composer captioned as the Ankara answer. If a future field
# turns out to have the same shape, it belongs here too, so the one
# compatibility check below covers it rather than another one-off branch.
FIELD_SOURCES: Dict[str, FrozenSet[str]] = {
    "province": frozenset({"finturk"}),
}


def _compatible_sources(step: Step) -> Optional[FrozenSet[str]]:
    """Sources compatible with every narrowing field the step set.

    `None` means no narrowing field was set -- every source is still valid.
    An empty frozenset means the step named filters no single source supports
    together (unreachable with one entry in FIELD_SOURCES today, but kept so
    a second entry fails safe rather than picking one arbitrarily).
    """
    compatible: Optional[FrozenSet[str]] = None
    for field, sources in FIELD_SOURCES.items():
        if getattr(step, field, None):
            compatible = sources if compatible is None else (compatible & sources)
    return compatible


def _resolve_source(step: Step, default_source: str) -> str:
    """The source to query: the model's guess, corrected only when a filter
    the step actually set proves that guess cannot be right.

    An explicit, already-compatible `source` is left untouched -- a model
    that wrote source="finturk" with province="ANKARA" is already correct.
    It is overridden only when a narrowing field is incompatible with it, and
    only when the compatible set names a single alternative; otherwise this
    raises rather than guessing, matching "fail explicit, never degrade".
    """
    compatible = _compatible_sources(step)
    source = step.source or default_source
    if compatible is None:
        return source
    if not compatible:
        named = {f: getattr(step, f) for f in FIELD_SOURCES if getattr(step, f, None)}
        raise ValueError(f"step names filters no single source supports together: {named}")
    if source in compatible:
        return source
    if len(compatible) == 1:
        return next(iter(compatible))
    raise ValueError(f"source={source!r} does not support "
                     f"{[f for f in FIELD_SOURCES if getattr(step, f, None)]}; "
                     f"compatible sources: {sorted(compatible)}")


def _validate_series_matches_request(step: Step, series: SeriesResult) -> None:
    """The resolved series must carry every narrowing filter the step named,
    not merely have been fetched without raising.

    This is the backstop for the whole mechanism above: even if a source swap
    or a fallback-discovery pick goes wrong in some way this file did not
    anticipate, a broader aggregate must never reach the artifact labelled as
    the answer to a narrower request -- e.g. a Turkiye-wide bulletin or
    finturk sum standing in for one province.
    """
    if step.province:
        if series.source != "finturk" or not series.province:
            raise ValueError(
                f"requested province={step.province!r} but the resolved series "
                f"{series.source}:{series.key!r} is not province-level "
                f"(province={series.province!r}); refusing to return a broader aggregate "
                "as a province-level answer")
        if slugify(series.province) != slugify(step.province):
            raise ValueError(
                f"requested province={step.province!r} but the resolved series covers "
                f"{series.province!r}; refusing to return a mismatched province")


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
    if key.split("/")[0] == "external":
        # An external series_key is '<source_id>/<location>/<name>' and keeps
        # its slashes; only a copied 'external/' prefix is removed.
        return key.split("/", 1)[1]
    head, _, tail = key.rpartition("/")
    if head.split("/")[0] in LAKEHOUSE_SOURCES:
        return tail
    return key


def _column_name(step: Step, fallback: str) -> str:
    """A safe identifier for a new column."""
    raw = step.as_name or step.column or fallback
    return re.sub(r"[^\w]+", "_", str(raw)).strip("_") or fallback


def match_column(name: str, columns: List[str]) -> Optional[str]:
    """The column in `columns` a model-supplied name means, or None.

    Exact match, then case-insensitive, then the name slugified the way
    `_column_name` slugifies it, then a containment match when it is unique.
    The same rule at plan time (`pipeline.apply_scope`) and at run time
    (`Executor._resolve_column`), so a reference the plan checks is the one
    the executor resolves.
    """
    if name in columns:
        return name
    lowered = {c.lower(): c for c in columns}
    if name.lower() in lowered:
        return lowered[name.lower()]
    cleaned = re.sub(r"[^\w]+", "_", name).strip("_").lower()
    if cleaned in lowered:
        return lowered[cleaned]
    partial = [c for c in columns if cleaned and (cleaned in c.lower() or c.lower() in cleaned)]
    return partial[0] if len(partial) == 1 else None


def _chart_title(artifact, columns: List[str], with_period: bool = True) -> str:
    """'Tuketici Kredileri - Konut ve Konut Kredisi (TL, Akim, %) | 2021-01 .. 2025-12':
    the series drawn and the months covered. Three labels at most, the rest
    counted, so a wide table does not become a title nobody can read."""
    labels = [artifact.lineage[c].label for c in columns if c in artifact.lineage]
    if len(labels) > 3:
        head = ", ".join(labels[:3]) + f" (+{len(labels) - 3})"
    elif len(labels) > 1:
        head = ", ".join(labels[:-1]) + " ve " + labels[-1]
    else:
        head = labels[0] if labels else artifact.title
    if with_period and artifact.periods():
        return f"{head} | {artifact.periods()[0][:7]} .. {artifact.periods()[-1][:7]}"
    return head


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
        # Difference only AFTER fetching the lag history, including inputs of
        # sums. The displayed plan/window stays unchanged. If the archive has
        # no preceding month, net_change correctly retains a missing value.
        lag = max((s.periods or 1 for s in plan.steps if s.op == "transform"
                   and s.operation in ("net_change", "change")), default=0)
        fetch_plan = plan
        if lag and plan.start:
            fetch_plan = plan.model_copy(update={
                "start": (pd.Timestamp(plan.start) - pd.DateOffset(months=lag)).strftime("%Y-%m-%d")})
        for index, step in enumerate(plan.steps, start=1):
            started = time.perf_counter()
            try:
                if step.op == "chart" and lag:
                    T.window(session.artifact, plan.start, plan.end)
                detail = self._dispatch(step, fetch_plan if step.op == "fetch_series" else plan)
                ok = True
            except Exception as exc:                                  # noqa: BLE001
                # Deliberately broad: a tool raising anything must cost one step,
                # never the turn. The message reaches the composer as a fact.
                detail = f"{type(exc).__name__}: {exc}"
                ok = False
            session.audit.append(AuditStep(
                index=index, op=step.op, arguments=step.arguments(), ok=ok,
                detail=str(detail)[:500], seconds=time.perf_counter() - started))

        # The window is applied to the whole artifact, so it must belong to
        # a turn that put something in it: a NEW question naming "2024" that
        # then found no series used to cut the previous question's 60-row
        # table down to that year's 12 rows on its way out.
        if (plan.start or plan.end) and session.turn_columns:
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
            "ingest_source": self._ingest_source, "ingest_external": self._ingest_external,
            "clear_table": self._clear_table, "footnotes": self._footnotes,
        }[step.op]
        return handler(step, plan)

    def _ingest_source(self, step: Step, plan: Plan) -> str:
        """Land a URL in the lakehouse's external zone (see
        backend.ingestion.external). Writes Parquet under data/external/, never
        touches lakehouse.duckdb; the series then fetch with source='external'.
        The explicit op runs heuristics plus the lakehouse cross-check; the
        model labelling happens on the pre-planning path, which has the client."""
        question = self.session.turns[-1]["question"] if self.session.turns else ""
        result = ingest_url(step.url, hint=step.hint or step.query or question)
        # `facts["sources"]` is the citation legend (`verifier.source_map`);
        # what the URLs produced lives beside it under its own name.
        self.session.facts.setdefault("landed_sources", []).append(result.to_dict())
        if result.status != "error":
            self.session.cite({"source": "external", "table": "external_sources", "source_id": result.source_id,
                               "url": result.url, "status": result.status, "n_series": result.n_series})
        keys = result.all_series_keys()
        return (f"{result.status}: {len(keys)} series landed from {step.url}"
                + (" (cache hit)" if result.cache_hit else "")
                + (": " + ", ".join(keys[:5]) if keys else ""))

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
        default_source = "macro" if key.upper().startswith(("TP.", "DERIVED.")) else "bulletin"
        # Treat the model's own source/dataset/filter fields as hints, not a
        # routing decision: `_resolve_source` corrects `source` only when a
        # field like `province` proves it cannot be right (see FIELD_SOURCES).
        source = _resolve_source(step, default_source)
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
            # `source` was already corrected by `_resolve_source` above to satisfy
            # any narrowing field (province today) the step set, so the primary
            # search below uses it directly rather than searching unrestricted and
            # filtering after: this discover() has no per-source seat floor (see
            # its own docstring), so an unrestricted search for a query like
            # "toplam_mevduat" is dominated by bulletin's exact-key matches and
            # never surfaces the finturk candidate at all. The unrestricted
            # fallback is tried only when nothing narrowed the source to begin
            # with -- reaching for it after a province was named would silently
            # reopen the exact bug this closes.
            compatible = _compatible_sources(step)
            pool = discover(step.as_name or key, source=source, limit=1)["candidates"]
            if not pool and compatible is None:
                pool = discover(step.as_name or key, limit=1)["candidates"]
            if not pool:
                extra = f" compatible with {sorted(compatible)}" if compatible else ""
                raise ValueError(f"{exc}; discovery found no alternative{extra} for {key!r}") from exc
            best = pool[0]
            series = fetch_series(best["key"], source=best["source"],
                                  dataset=best["dataset"] if best["source"] in DATASET_SOURCES else None,
                                  currency=best.get("currency") or (
                                      "total" if best["source"] not in NO_CURRENCY_SOURCES else None),
                                  start=plan.start, end=plan.end,
                                  province=step.province if best["source"] == "finturk" else None)
            resolved_by = f" (key {key!r} not found; resolved to {best['source']}:{best['key']!r} by discovery)"

        # Post-fetch backstop: whatever path produced `series`, it must still
        # satisfy every narrowing filter the step named -- never a broader
        # aggregate silently standing in for a narrower request.
        _validate_series_matches_request(step, series)

        # The weekly bulletin is observed on Fridays. Joining it into a
        # month-indexed table raw does not add a column -- it adds 296 new index
        # entries and turns a 60-row answer into a 308-row one. Aggregate to the
        # monthly grain using the rule the series' own semantics imply. Uses the
        # series' own resolved source, not the pre-fetch guess, since a fallback
        # above may have resolved to a different source than first attempted.
        values, transform = series.values, None
        if series.source == "weekly":
            rule = _monthly_rule(series.temporal_semantics)
            values = T.resample_to_monthly(series.values, rule)
            transform = f"resample_to_monthly({rule})"

        citation = series.citation()
        # An external series_key is '<source_id>/<location>/<name>'; the name
        # segment is the readable column, the rest is its address.
        default = key.rsplit("/", 1)[-1] if series.source == "external" else key
        name = _column_name(step, re.sub(r"[^\w]+", "_", default))
        # `AnalysisArtifact.add_column` replaces a same-named column outright
        # (see its own docstring) -- correct when a plan deliberately names a
        # column (`as_name`, e.g. `pipeline.apply_scope` repairing a stale
        # reference onto a fresh fetch), but silent data loss when TWO steps
        # in the SAME plan both fall back to the bare key with no `as_name`.
        # Measured live: "Ankara ve İstanbul'daki tasarruf mevduatını
        # karşılaştır" planned two fetch_series steps for one finturk key with
        # no `as_name`, both auto-named "tasarruf_mevduati" -- the second
        # (Ankara) silently overwrote the first (İstanbul) within this same
        # turn, and the composer, seeing only one column left, told the user
        # İstanbul had no data at all rather than reporting a collision.
        # Scoped to `turn_columns` (this turn only, not `add_column`), and
        # only when the plan left the name to be inferred, so a fresh
        # question's deliberate `as_name` still overwrites a stale column from
        # an earlier turn exactly as designed.
        if step.as_name is None and name in self.session.turn_columns:
            province = (citation.get("filters") or {}).get("province")
            candidate = f"{name}_{slugify(province)}" if province else name
            suffix = 2
            while candidate in self.session.turn_columns:
                candidate = f"{name}_{suffix}"
                suffix += 1
            name = candidate
        name = self.session.touch_column(name)
        # Three slices of one line share a name; the label says which slice
        # this is, or the composer cannot tell the FX column from the TL one.
        label = series.name + {"FX": " (YP)", "TL": " (TL)"}.get(currency or "", "")
        self.session.artifact.add_column(name, values, ColumnLineage(
            transform=transform,
            column=name, label=label, source=series.source, unit=series.unit,
            temporal_semantics=series.temporal_semantics, key=series.key,
            # The published grain, recorded before any resampling: a weekly
            # series lands on the monthly index but stays a weekly series, and
            # the verifier needs to know that before it is divided by one.
            grain=candidate_grain({"source": series.source,
                                   "native_frequency": (series.extra or {}).get("native_frequency")}),
            citation=citation))
        self.session.cite(citation)
        return (f"{name}: {len(values)} points, {series.unit}, "
                f"{series.temporal_semantics}, {series.period_start}..{series.period_end}{resolved_by}")

    def _transform(self, step: Step, plan: Plan) -> str:
        artifact = self.session.artifact
        column = self._resolve_column(step.column) if step.operation != "sum_columns" else None
        if step.operation == "index_to_base":
            name = T.index_to_base(artifact, column, step.base_period or plan.start, step.as_name)
        elif step.operation == "deflate":
            deflator = self._resolve_column(step.other_column, required="deflate needs other_column")
            name = T.deflate(artifact, column, deflator, step.base_period or plan.start, step.as_name)
        elif step.operation == "change":
            name = T.change(artifact, column, step.periods or 1, step.as_name)
        elif step.operation == "net_change":
            name = T.net_change(artifact, column, step.periods if step.periods is not None else 1, step.as_name)
        elif step.operation == "sum_columns":
            columns = [self._resolve_column(c) for c in step.columns]
            name = T.sum_columns(artifact, columns, step.as_name)
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
        # Only a column with a lakehouse row behind it is re-fetched: the old
        # session-scoped `ingest_external` path also says source="external",
        # but its citation table is "external" (a URL), not a view.
        fetchable = lineage.citation.get("table") in (
            "bulletin_observations", "weekly_observations", "macro_observations",
            "finturk_observations", "external_observations")
        if lineage.source in LAKEHOUSE_SOURCES and lineage.key and fetchable:
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
        # This turn's own columns first: a fresh question's partner is a series
        # it fetched, not one a previous question left in the table.
        own = [c for c in self.session.turn_columns if c in artifact.column_names()]
        others = [c for c in own if c != column] + \
                 [c for c in artifact.column_names() if c != column and c not in own]
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
            # A follow-up charts what is on screen too ("ayni grafige ekle");
            # a turn that touched no column at all ("bunun grafigini ciz")
            # charts what is on screen and nothing else -- `None` here would
            # mean EVERY column the conversation ever built, most of which
            # the user is not looking at.
            if self.session.facts.get("is_followup") or not scope:
                scope |= set(self.session.visible_columns)
            columns = [c for c in artifact.column_names() if c in scope] or None
        # "cubuk grafik" / "bar chart": the chart tool has always drawn bars
        # on request; nothing passed the request through until now.
        question = self.session.turns[-1]["question"] if self.session.turns else ""
        kind = ("pie" if PIE_CHART_WORDS.search(question)
                else "bar" if BAR_CHART_WORDS.search(question) else "line")
        dropped: List[str] = []
        note: Optional[str] = None
        pie_meta: Optional[Dict[str, Any]] = None
        title = step.title or _chart_title(artifact, columns or artifact.column_names(),
                                           with_period=kind != "pie")
        if kind == "pie":
            # A pie is a snapshot: at the month the question named (the
            # router read it into the window), else the last month where every
            # column has a value. When the table cannot be a pie, say why and
            # draw the line chart the same data supports, rather than fail.
            # The month the question named rides on the step (a follow-up's
            # plan carries no window, because a window would trim the table
            # to that one row); otherwise the window's end, else the last.
            period = step.base_period or plan.end
            try:
                when, labels, values = resolve_pie(artifact, columns, period)
                figure = build_chart(artifact, columns, title, kind="pie", period=period)
                total = sum(values)
                pie_meta = {"period": when,
                            "shares": {label: f"%{100 * v / total:.1f}".replace(".", ",")
                                       for label, v in zip(labels, values)}}
            except PieError as exc:
                note = f"Pasta grafigi cizilemedi: {exc}. Bunun yerine cizgi grafik cizildi."
                kind = "line"
        if kind != "pie":
            figure, columns, dropped, rebase_note = self._line_or_bar(artifact, columns, title, kind)
            if rebase_note:
                note = f"{note} {rebase_note}".strip() if note else rebase_note
            if dropped and not step.title:
                figure["layout"]["title"] = {"text": _chart_title(artifact, columns)}
        # If change detection already ran on a charted column, draw its breaks
        # on the chart so the reader sees where the regimes change. A pie has
        # no time axis to mark.
        charted = columns or artifact.column_names()
        analysis = self.session.facts.get("analysis", {})
        breaks_by_column = {c: analysis[f"changepoint:{c}"]["breaks"]
                            for c in charted if f"changepoint:{c}" in analysis
                            and analysis[f"changepoint:{c}"].get("breaks")} if kind != "pie" else {}
        if breaks_by_column:
            figure = mark_breaks(figure, breaks_by_column)
        summary = chart_summary(artifact, columns)
        summary["kind"] = kind
        if pie_meta:
            summary.update(pie_meta)
        if note:
            summary["note"] = note
        if dropped:
            summary["dropped"] = dropped
            summary["dropped_note"] = ("Grafikte gosterilmeyen sutun(lar): " + ", ".join(dropped)
                                       + " -- bir grafikte en fazla iki farkli birim gosterilebilir; "
                                       "bu sutunlar tabloda duruyor.")
        self.session.facts["chart"] = summary
        self.session.facts["figure"] = figure
        marks = sum(len(b) for b in breaks_by_column.values())
        return (f"{kind} chart with {len(figure['data'])} trace(s)"
                + (f" at {pie_meta['period']}" if pie_meta else "")
                + (f", {marks} break marker(s)" if marks else "")
                + (f"; {note}" if note else "")
                + (f"; NOT charted ({len(dropped)} more unit(s)): {', '.join(dropped)}" if dropped else ""))

    def _line_or_bar(self, artifact, columns, title, kind):
        """See `_chart_title` for the title; this only draws."""
        """(figure, columns drawn, columns dropped, note) for a line/bar chart.

        Too many units for one chart: keep the two unit groups that hold the
        columns THIS turn touched first, then the largest -- the column the
        user just asked to add must not be the one that silently falls off
        (it was: "KFE'yi ekle ve ciz" charted everything except KFE).
        """
        try:
            return build_chart(artifact, columns, title, kind=kind), columns, [], None
        except ValueError as exc:
            if "different units" not in str(exc):
                raise
        wanted = columns or artifact.column_names()
        # First choice: keep every column by re-basing the non-percentage
        # ones to "first month = 100" -- the deck's own chart. Two axes then
        # suffice whenever the percentages share one unit.
        rebased_artifact, rebased, base = rebase_for_chart(artifact, wanted)
        if rebased:
            # Re-based series first, so they take the left axis and the
            # percentages the right -- the deck's layout.
            ordered = rebased + [c for c in wanted if c not in rebased]
            try:
                figure = build_chart(rebased_artifact, ordered, title, kind=kind)
                note = (", ".join(artifact.lineage[c].label for c in rebased)
                        + f" grafikte {base}=100 bazina getirildi (tabloda orijinal birimleriyle duruyor); "
                        "farkli birimler tek eksende gosterilemez.")
                return figure, ordered, [], note
            except ValueError as exc:
                if "different units" not in str(exc):
                    raise
        # Still three units (e.g. % beside puan): keep the two groups that
        touched = set(self.session.turn_columns)
        groups = [[c for c in group if c in wanted] for group in T.columns_sharing_unit(artifact)]
        groups = [g for g in groups if g]
        groups.sort(key=lambda g: (-sum(c in touched for c in g), -len(g)))
        kept = [c for group in groups[:2] for c in group]
        columns = [c for c in wanted if c in kept]
        return (build_chart(artifact, columns, title, kind=kind), columns,
                [c for c in wanted if c not in kept], None)

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
        self.session.turn_cited = []
        self.session.turn_columns = []
        self.session.visible_columns = []
        self.session.hidden_inputs = []
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
        found = match_column(name, columns)
        if found is None:
            raise ValueError(f"column {name!r} is not in the table; have {columns}")
        # A resolved column is one this turn read, so it belongs in the
        # turn's scope beside the columns the turn produced -- a deflated
        # series with its deflator out of the table explains nothing.
        return self.session.touch_column(found)
