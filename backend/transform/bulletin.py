"""De-cumulation and the metric catalogue for the BDDK bulletin corpus.

The brief warns that some BDDK releases are cumulative. One is: table 2, the
income statement, is year-to-date and resets every January. Left alone it
produces a chart that climbs all year and collapses in January, and a
month-over-month "growth rate" that is arithmetic nonsense.

`decumulate` therefore adds `value_flow`: the month's own contribution, which
is the published value in January and the first difference after that. The
published figure stays in `value`, because a year-to-date total is the right
answer to "bu yılın karı" and the wrong answer to "bu ayın karı"; the agent
needs both, labelled.
"""
import pandas as pd

CUMULATIVE = "cumulative_ytd"

# The identity of one series through time, within which a difference is meaningful.
SERIES_KEYS = ["dataset", "entity_key", "metric", "currency"]


def decumulate(observations: pd.DataFrame) -> pd.DataFrame:
    """Add `value_flow` to a bulletin frame.

    Stocks and ratios get None: differencing a stock gives a net balance change,
    which is a different concept with its own caveats and is already served by
    the `growth` table. Only a cumulative series is de-cumulated here.
    """
    frame = observations.copy()
    frame["value_flow"] = pd.NA

    cumulative = frame.temporal_semantics == CUMULATIVE
    if not cumulative.any():
        return frame

    block = frame.loc[cumulative].copy()
    block["year"] = pd.to_datetime(block.period).dt.year
    block = block.sort_values(SERIES_KEYS + ["year", "period"])

    grouped = block.groupby(SERIES_KEYS + ["year"], dropna=False, sort=False)["value"]
    # First month of a year carries the year's opening figure; after that the
    # month's own contribution is the first difference.
    flow = grouped.diff()
    flow = flow.fillna(block["value"].where(grouped.cumcount() == 0))

    frame.loc[block.index, "value_flow"] = flow
    return frame


def build_bulletin_metrics(observations: pd.DataFrame) -> pd.DataFrame:
    """One row per (dataset, metric, currency, unit) the corpus actually holds.

    This is the bulletin half of the agent's series index: it is what makes
    `temporal_semantics` discoverable without reading 135k fact rows, and it is
    where the tables whose 'metric' is really a bucket are called out. Table 09
    splits deposits by size and table 10 by maturity, so their metric column
    carries bracket names, not measures -- `metric_kind` says which is which.
    """
    bucket_kinds = {
        "mevduat_turler": "size_bucket",
        "mevduat_vade": "maturity_bucket",
        "likidite_durumu": "maturity_bucket",
        "krediler": "maturity_bucket",
    }

    grouped = observations.groupby(
        ["dataset", "metric", "currency", "unit", "temporal_semantics"], dropna=False
    )
    catalog = grouped.agg(
        n_entities=("entity_key", "nunique"),
        n_observations=("value", "size"),
        first_period=("period", "min"),
        last_period=("period", "max"),
    ).reset_index()

    catalog["metric_kind"] = catalog.apply(
        lambda row: bucket_kinds.get(row.dataset, "measure")
        if row.metric not in ("balance", "toplam") else "measure",
        axis=1,
    )
    catalog["source"] = "BDDK"
    columns = ["source", "dataset", "metric", "currency", "unit", "temporal_semantics",
               "metric_kind", "n_entities", "n_observations", "first_period", "last_period"]
    return catalog[columns].sort_values(["dataset", "metric", "currency"]).reset_index(drop=True)


def build_bulletin_entities(observations: pd.DataFrame) -> pd.DataFrame:
    """One row per (dataset, entity_key): the agent's search surface.

    `macro_series` lets a question about TÜFE find its series_code without
    reading 89k fact rows, and `weekly_items` does the same for the weekly
    corpus. The monthly bulletin had no equivalent, so finding the housing-loan
    row meant SELECT DISTINCT over 135k rows -- the one query a small model
    cannot be trusted to write, standing between every question and its data.

    Measured: each (dataset, entity_key) carries exactly one entity_name, unit,
    temporal_semantics, entity_type and parent_key across all 67 months, so
    nothing here is an aggregation that could hide a conflict. `formula` and
    `footnote` DO vary by period (4 and 1 entities respectively); the latest is
    carried for discovery and bulletin_observations stays the per-period truth.
    """
    stable = ["source", "dataset", "entity_type", "entity_key", "entity_name",
              "parent_key", "unit", "temporal_semantics"]
    frame = observations.sort_values("period")

    index = frame.groupby(["dataset", "entity_key"], dropna=False).agg(
        **{column: (column, "first") for column in stable if column not in ("dataset", "entity_key")},
        formula=("formula", "last"),
        footnote=("footnote", "last"),
        first_period=("period", "min"),
        last_period=("period", "max"),
        n_periods=("period", "nunique"),
        n_observations=("value", "size"),
    ).reset_index()

    # What a query MUST filter on: an entity published in three currencies
    # returns three rows per month, and summing them triple-counts.
    for column, name in (("currency", "currencies"), ("metric", "metrics")):
        combos = frame.groupby(["dataset", "entity_key"])[column].apply(
            lambda values: ",".join(sorted(values.dropna().unique())) or None
        ).rename(name)
        index = index.merge(combos, on=["dataset", "entity_key"], how="left")

    children = frame[frame.parent_key.notna()].groupby(
        ["dataset", "parent_key"]).entity_key.nunique().rename("n_children").reset_index()
    children = children.rename(columns={"parent_key": "entity_key"})
    index = index.merge(children, on=["dataset", "entity_key"], how="left")
    index["n_children"] = index.n_children.fillna(0).astype(int)

    columns = [*stable, "currencies", "metrics", "n_children", "formula", "footnote",
               "first_period", "last_period", "n_periods", "n_observations"]
    return index[columns].sort_values(["dataset", "entity_key"]).reset_index(drop=True)


def check_decumulation(observations: pd.DataFrame) -> list:
    """Every cumulative series must have a flow for every month it covers.

    A missing flow means a year with no January, which would silently turn a
    year-to-date jump into a "monthly" figure.
    """
    cumulative = observations[observations.temporal_semantics == CUMULATIVE]
    if cumulative.empty:
        return []

    missing = int(cumulative.value_flow.isna().sum())
    datasets = sorted(cumulative.dataset.unique())
    return [{
        "source": "BDDK",
        "check": f"de-cumulation of {', '.join(datasets)}",
        "passed": missing == 0,
        "detail": f"{len(cumulative):,} cumulative rows, {missing} without a monthly flow",
    }]
