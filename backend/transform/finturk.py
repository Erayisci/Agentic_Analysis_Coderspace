"""Derived tables for the FinTurk (il-bazli) corpus."""
import pandas as pd

from ..core import search_text
from ..domain.finturk_tables import BY_SLUG


def build_finturk_metrics(observations: pd.DataFrame) -> pd.DataFrame:
    """One row per (dataset, metric): the agent's search surface for FinTurk.

    Mirrors `transform.bulletin.build_bulletin_metrics` -- discovery over a
    905k-row fact table is a table lookup, not a `SELECT DISTINCT`. FinTurk
    publishes no per-row temporal_semantics (unlike the bulletins' per-metric
    stock/cumulative_ytd/ratio column), so it is derived here the same way the
    unit already distinguishes the one table that differs: `oranlar`'s columns
    are published ratios, everything else is a period-end level.
    """
    grouped = observations.groupby(["dataset", "metric", "metric_name", "unit"], dropna=False)
    catalog = grouped.agg(
        n_provinces=("province", "nunique"),
        n_periods=("period", "nunique"),
        n_observations=("value", "size"),
        first_period=("period", "min"),
        last_period=("period", "max"),
    ).reset_index()
    catalog["source"] = "finturk"
    catalog["temporal_semantics"] = catalog["unit"].apply(lambda u: "ratio" if u == "%" else "stock")
    # The same searchable text the other three indexes carry (see
    # `core.search_text`), so `tools.lakehouse.discover` greps one column
    # per corpus rather than a different pair of columns for this one.
    catalog["search_text"] = [
        search_text.for_finturk_metric(row.metric_name, row.dataset,
                                       BY_SLUG[row.dataset].title if row.dataset in BY_SLUG else None,
                                       row.unit)
        for row in catalog.itertuples()]
    catalog["search_fold"] = [search_text.searchable(t) for t in catalog.search_text]
    columns = ["source", "dataset", "metric", "metric_name", "unit", "temporal_semantics",
               "n_provinces", "n_periods", "n_observations", "first_period", "last_period",
               "search_text", "search_fold"]
    return catalog[columns].sort_values(["dataset", "metric"]).reset_index(drop=True)
