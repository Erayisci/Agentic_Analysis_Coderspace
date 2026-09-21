"""Load one time series from the lakehouse, with the metadata that makes it safe to use.

This is the seam every analytical tool goes through, so the domain rules that
would otherwise have to be re-learned by each of them live here once:

- **Source routing.** The lakehouse holds four fact tables with four grains
  and four key vocabularies (`bulletin_observations` keyed on
  dataset+entity_key, `weekly_observations` on BDDK's item id,
  `macro_observations` on an EVDS series_code, `finturk_observations` on a
  dataset+metric pair further sliced by province). A tool should name a
  series, not know which table it came from.
- **Cumulative series.** `kar_zarar` is year-to-date and resets every January.
  Differencing `value` across that reset produces a large negative number that
  means nothing, so a cumulative series is served from `value_flow` -- the
  month's own contribution -- unless the caller explicitly asks for the
  published year-to-date figure.
- **Superseded weekly items.** A retired weekly item and the item that replaced
  it duplicate each other over the overlap, so any series built without
  `retired_on IS NULL` silently double-counts.
- **Units and semantics travel with the values.** A number without its unit is
  not an answer, and a transform that combines `bin TL` with `milyon TL` is off
  by 1000x. `SeriesResult` carries both so the executor can refuse, and the
  composer can state, what the numbers actually are.
"""
from typing import NamedTuple, Optional

import duckdb
import pandas as pd

from ..core.config import DUCKDB_PATH
from ..core.labels import slugify
from ..domain.weekly_tables import BY_SLUG as WEEKLY_TABLES

SOURCES = ("bulletin", "weekly", "macro", "finturk")
CUMULATIVE = "cumulative_ytd"

# FinTurk's taraf (bank-group) dimension is not exposed as a caller filter,
# the same way the monthly bulletin pins taraf=10001 without letting a caller
# pick a bank group: 10001 is the whole sector, and adding a taraf parameter
# here would grow the Step DSL for a filter nothing has asked for yet.
FINTURK_TARAF = 10001
FINTURK_EXCLUDED_PROVINCE = "YURT DIŞI"

# When a bulletin entity publishes several measures and the caller named none,
# this is the order of preference. "balance" and "toplam" are the headline
# figures; the sectoral table's "nakdi" (cash loans) is its equivalent.
DEFAULT_METRICS = ("balance", "toplam", "toplam_nakdi", "nakdi", "rasyo")


class SeriesResult(NamedTuple):
    """One series plus everything needed to describe it truthfully."""

    values: pd.Series           # float, indexed by period (ascending, unique)
    source: str                 # bulletin | weekly | macro | finturk
    key: str                    # entity_key or series_code
    name: str                   # human-readable label, as published
    unit: str
    temporal_semantics: str     # stock | flow | rate | index | ratio | cumulative_ytd
    value_column: str           # which column was read -- value, value_flow, value_last, ...
    dataset: Optional[str] = None
    currency: Optional[str] = None
    metric: Optional[str] = None
    province: Optional[str] = None      # finturk only -- None means summed across provinces

    @property
    def period_start(self) -> str:
        return self.values.index.min().strftime("%Y-%m-%d")

    @property
    def period_end(self) -> str:
        return self.values.index.max().strftime("%Y-%m-%d")

    def citation(self) -> dict:
        """Provenance for one series, for the trust layer to quote verbatim."""
        table = {"bulletin": "bulletin_observations", "weekly": "weekly_observations",
                 "macro": "macro_observations", "finturk": "finturk_observations"}[self.source]
        if self.source == "macro":
            key_field, filters = "series_code", {"currency": self.currency, "metric": self.metric}
        elif self.source == "finturk":
            # Every filter here is a real WHERE clause, so the citation's SQL
            # reproduces the column as written; the national sum is stated
            # as `aggregate`, not smuggled in as a fake province value.
            key_field, filters = "metric", {"taraf_code": FINTURK_TARAF, "province": self.province}
        else:
            key_field, filters = "entity_key", {"currency": self.currency, "metric": self.metric}
        filters = {"dataset": self.dataset, key_field: self.key, **filters}
        aggregate = (f"SUM(value) over every province except '{FINTURK_EXCLUDED_PROVINCE}'"
                     if self.source == "finturk" and not self.province else None)
        return {
            "table": table,
            "filters": {k: v for k, v in filters.items() if v is not None},
            **({"aggregate": aggregate, "exclude": {"province": FINTURK_EXCLUDED_PROVINCE}} if aggregate else {}),
            "value_column": self.value_column,
            "unit": self.unit,
            "temporal_semantics": self.temporal_semantics,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "n_points": int(len(self.values)),
        }

    def describe(self) -> dict:
        """The same facts as a flat dict, for a tool's JSON return value."""
        return {"source": self.source, "key": self.key, "name": self.name, "unit": self.unit,
                "temporal_semantics": self.temporal_semantics, "value_column": self.value_column,
                "dataset": self.dataset, "currency": self.currency, "metric": self.metric,
                "province": self.province,
                "period_start": self.period_start, "period_end": self.period_end,
                "n_points": int(len(self.values))}


def _connect():
    if not DUCKDB_PATH.exists():
        raise FileNotFoundError(
            f"{DUCKDB_PATH} not found -- run `python -m backend.lakehouse.build` first"
        )
    return duckdb.connect(str(DUCKDB_PATH), read_only=True)


def _window(sql: str, params: list, start: Optional[str], end: Optional[str], column: str = "period"):
    if start:
        sql += f" AND {column} >= ?"
        params.append(start)
    if end:
        sql += f" AND {column} <= ?"
        params.append(end)
    return sql, params


def _bulletin(con, key, dataset, currency, metric, start, end, cumulative_as):
    if not dataset:
        matches = con.execute(
            "SELECT DISTINCT dataset FROM bulletin_entities WHERE entity_key = ?", [key]
        ).df().dataset.tolist()
        if not matches:
            raise ValueError(f"no bulletin entity with entity_key={key!r}; search bulletin_entities first")
        if len(matches) > 1:
            raise ValueError(f"entity_key={key!r} exists in {matches}; pass dataset= to disambiguate")
        dataset = matches[0]

    meta = con.execute(
        "SELECT entity_name, unit, temporal_semantics, currencies, metrics FROM bulletin_entities "
        "WHERE dataset = ? AND entity_key = ?", [dataset, key]
    ).df()
    if meta.empty:
        raise ValueError(f"no bulletin entity dataset={dataset!r} entity_key={key!r}")
    meta = meta.iloc[0]

    # A cumulative series is served as the month's own figure unless the caller
    # asks for the published year-to-date total; see the module docstring.
    column = "value_flow" if (meta.temporal_semantics == CUMULATIVE and cumulative_as == "flow") else "value"

    sql = (f"SELECT period, {column} AS value FROM bulletin_observations "
           "WHERE dataset = ? AND entity_key = ?")
    params = [dataset, key]
    published = (meta.currencies or "").split(",") if meta.currencies else []
    if currency is not None and published:
        if currency not in published:
            raise ValueError(f"{key!r} publishes currencies {published}, not {currency!r}")
        sql += " AND currency = ?"
        params.append(currency)
    else:
        currency = None
    published_metrics = (meta.metrics or "").split(",") if meta.metrics else []
    if metric is None and len(published_metrics) > 1:
        # An entity in a bucketed table publishes several measures, so an
        # unfiltered read returns one row per measure per month. Failing here
        # costs the whole turn; choosing the headline measure and recording the
        # choice in the citation keeps the answer honest and available.
        metric = next((m for m in DEFAULT_METRICS if m in published_metrics),
                      sorted(published_metrics)[0])
    if metric is not None:
        sql += " AND metric = ?"
        params.append(metric)
    sql, params = _window(sql, params, start, end)
    frame = con.execute(sql + " ORDER BY period", params).df()
    return frame, dict(name=meta.entity_name, unit=meta.unit, semantics=meta.temporal_semantics,
                       value_column=column, dataset=dataset, currency=currency, metric=metric,
                       available_metrics=published_metrics)


def _weekly(con, key, currency, metric, start, end, include_retired):
    meta = con.execute(
        "SELECT entity_name, entity_type, retired_on, is_informational, dataset "
        "FROM weekly_items WHERE entity_key = ?", [key]
    ).df()
    if meta.empty:
        raise ValueError(f"no weekly item with entity_key={key!r}; search weekly_items first")
    meta = meta.iloc[0]
    if pd.notna(meta.retired_on) and not include_retired:
        raise ValueError(
            f"weekly item {key!r} was retired on {meta.retired_on:%Y-%m-%d}: BDDK replaced its "
            "definition and the replacement duplicates it over the overlap. Use the current item, "
            "or pass include_retired=True to read the superseded definition deliberately."
        )

    sql = ("SELECT o.period, o.value FROM weekly_observations o "
           "JOIN weekly_items i USING (dataset, entity_key) WHERE o.entity_key = ?")
    params = [key]
    if not include_retired:
        sql += " AND i.retired_on IS NULL"
    if currency is not None:
        sql += " AND o.currency = ?"
        params.append(currency)
    if metric is not None:
        sql += " AND o.metric = ?"
        params.append(metric)
    sql, params = _window(sql, params, start, end, "o.period")
    frame = con.execute(sql + " ORDER BY o.period", params).df()
    unit, semantics = con.execute(
        "SELECT min(unit), min(temporal_semantics) FROM weekly_observations WHERE entity_key = ?", [key]
    ).fetchone()
    # Unlike the bulletin, a weekly entity_key is BDDK's own item id, not a
    # parent/child qualified key -- so its entity_name alone ("a) Konut")
    # carries no signal of which table it belongs to, and reads as unrelated
    # to the near-identical bulletin series it usually gets fetched beside.
    table = WEEKLY_TABLES.get(meta.dataset)
    name = f"{table.title} / {meta.entity_name}" if table else meta.entity_name
    return frame, dict(name=name, unit=unit, semantics=semantics, value_column="value",
                       dataset=meta.dataset, currency=currency, metric=metric,
                       is_informational=bool(meta.is_informational))


def _macro(con, key, start, end, column):
    meta = con.execute(
        "SELECT name_tr, unit, temporal_semantics, monthly_rule, native_frequency "
        "FROM macro_series WHERE series_code = ?", [key]
    ).df()
    if meta.empty:
        raise ValueError(f"no macro series with series_code={key!r}; search macro_series first")
    meta = meta.iloc[0]
    if column not in ("value", "value_avg", "value_last", "value_sum"):
        raise ValueError(f"unknown macro column {column!r}")

    sql = f"SELECT period, {column} AS value FROM macro_observations WHERE series_code = ?"
    sql, params = _window(sql, [key], start, end)
    frame = con.execute(sql + " ORDER BY period", params).df()
    return frame, dict(name=meta.name_tr, unit=meta.unit, semantics=meta.temporal_semantics,
                       value_column=column, dataset=None, currency=None, metric=None,
                       monthly_rule=meta.monthly_rule, native_frequency=meta.native_frequency)


def _finturk(con, key, dataset, province, start, end):
    if not dataset:
        matches = con.execute(
            "SELECT DISTINCT dataset FROM finturk_metrics WHERE metric = ?", [key]
        ).df().dataset.tolist()
        if not matches:
            raise ValueError(f"no FinTurk metric with metric={key!r}; search finturk_metrics first")
        if len(matches) > 1:
            raise ValueError(f"metric={key!r} exists in {matches}; pass dataset= to disambiguate")
        dataset = matches[0]

    meta = con.execute(
        "SELECT metric_name, unit, temporal_semantics FROM finturk_metrics "
        "WHERE dataset = ? AND metric = ?", [dataset, key]
    ).df()
    if meta.empty:
        raise ValueError(f"no FinTurk metric dataset={dataset!r} metric={key!r}")
    meta = meta.iloc[0]

    if province:
        # Naive `.strip().upper()` is the exact trap `core.labels.slugify`'s
        # docstring warns about elsewhere in this codebase: Python's `.upper()`
        # turns 'istanbul' into 'ISTANBUL', not the DB's 'İSTANBUL' -- so a
        # model or user typing the ASCII-only "Istanbul" would silently get
        # zero rows. Resolve through the slug instead, the same way
        # `bulletin_entities.entity_key` is matched case-safely.
        provinces = con.execute("SELECT DISTINCT province FROM finturk_observations").df().province.tolist()
        by_slug = {slugify(p): p for p in provinces}
        resolved = by_slug.get(slugify(province))
        if resolved is None:
            raise ValueError(f"unknown FinTurk province {province!r}; known provinces: {sorted(provinces)}")
        province = resolved
        sql = ("SELECT period, value FROM finturk_observations "
               "WHERE dataset = ? AND metric = ? AND taraf_code = ? AND province = ?")
        params = [dataset, key, FINTURK_TARAF, province]
        sql, params = _window(sql, params, start, end)
        name = f"{meta.metric_name} ({province})"
    else:
        # No province named: the national figure, summed across every province.
        # There is no published Türkiye-wide row in this product (see
        # domain.finturk_tables) -- this is the only way to one, and "YURT
        # DIŞI" (customers booked abroad) is excluded from it on purpose.
        # A RATIO cannot be summed: 81 provincial NPL ratios added up read
        # "%266", which is what this returned once. The national ratio is
        # the monthly bulletin's `rasyolar` table, not this product.
        if meta.temporal_semantics == "ratio" or str(meta.unit).strip() == "%":
            raise ValueError(
                f"FinTurk metric {key!r} ({meta.metric_name}) is a ratio and cannot be summed across "
                "provinces; name a province, or use the monthly bulletin's rasyolar table for Türkiye")
        sql = ("SELECT period, sum(value) AS value FROM finturk_observations "
               "WHERE dataset = ? AND metric = ? AND taraf_code = ? AND province != ?")
        params = [dataset, key, FINTURK_TARAF, FINTURK_EXCLUDED_PROVINCE]
        sql, params = _window(sql, params, start, end)
        sql += " GROUP BY period"
        name = f"{meta.metric_name} (Türkiye)"

    frame = con.execute(sql + " ORDER BY period", params).df()
    return frame, dict(name=name, unit=meta.unit, semantics=meta.temporal_semantics,
                       value_column="value", dataset=dataset, currency=None, metric=key,
                       province=province)


def load_series(
    key: str,
    source: str = "bulletin",
    dataset: Optional[str] = None,
    currency: Optional[str] = "total",
    metric: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    cumulative_as: str = "flow",
    include_retired: bool = False,
    column: str = "value",
    province: Optional[str] = None,
) -> SeriesResult:
    """One series from the lakehouse as a `SeriesResult`.

    Args:
        key: `entity_key` for bulletin/weekly, `series_code` for macro,
            `metric` for finturk.
        source: which corpus -- "bulletin" (monthly BDDK), "weekly" (BDDK weekly
            bulletin, observed on Fridays), "macro" (TCMB EVDS, monthly grain),
            or "finturk" (BDDK il-bazli, quarterly).
        dataset: required only when a key is ambiguous across tables of the same
            source; inferred from the source's own index table otherwise.
        currency: "total" (TL+FX) by default. Pass None for tables that publish
            no currency split; an entity that does publish one is never returned
            unfiltered, because summing TL, FX and total triple-counts.
        cumulative_as: "flow" serves a year-to-date series as the month's own
            figure (`value_flow`); "ytd" returns the published cumulative value.
        include_retired: read a superseded weekly definition on purpose.
        column: macro only -- "value" follows the series' own monthly_rule;
            "value_last" / "value_avg" override it (month-end FX vs average rate).
        province: finturk only. None sums every province (there is no published
            national total row in this product); naming one il filters to it.

    Raises ValueError when nothing matches, when the key is ambiguous, or when a
    filter contradicts what the series publishes -- a typo must fail loudly
    rather than return an empty series that downstream code would accept.
    """
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
    if cumulative_as not in ("flow", "ytd"):
        raise ValueError(f"cumulative_as must be 'flow' or 'ytd', got {cumulative_as!r}")

    con = _connect()
    try:
        if source == "bulletin":
            frame, meta = _bulletin(con, key, dataset, currency, metric, start, end, cumulative_as)
        elif source == "weekly":
            frame, meta = _weekly(con, key, currency, metric, start, end, include_retired)
        elif source == "finturk":
            frame, meta = _finturk(con, key, dataset, province, start, end)
        else:
            frame, meta = _macro(con, key, start, end, column)
    finally:
        con.close()

    if frame.empty:
        raise ValueError(f"no rows for source={source!r} key={key!r} in the requested window")
    if frame.period.duplicated().any():
        hint = f"; available metrics: {meta['available_metrics']}" if meta.get("available_metrics") else ""
        raise ValueError(f"more than one row per period for {key!r} -- pass metric= to disambiguate{hint}")
    if frame.value.isna().all():
        raise ValueError(f"{key!r} has no values in column {meta['value_column']!r} over the requested window")

    values = pd.Series(frame.value.astype(float).values,
                       index=pd.DatetimeIndex(pd.to_datetime(frame.period)), name=key)
    return SeriesResult(
        values=values, source=source, key=key, name=meta["name"], unit=meta["unit"],
        temporal_semantics=meta["semantics"], value_column=meta["value_column"],
        dataset=meta["dataset"], currency=meta["currency"], metric=meta["metric"],
        province=meta.get("province"),
    )
