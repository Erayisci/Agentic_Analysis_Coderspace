"""Load one time series from the lakehouse as a pandas Series indexed by period."""
from typing import Optional

import duckdb
import pandas as pd

from ..core.config import DUCKDB_PATH


def load_series(dataset: str, entity_key: str, currency: str = "total",
                metric: Optional[str] = None) -> pd.Series:
    """One (dataset, entity, currency[, metric]) series from bulletin_observations.

    Returns a float Series indexed by month-start Timestamp, sorted ascending.
    Raises ValueError when nothing matches, so a typo fails loudly instead of
    returning an empty series that downstream code would silently accept.
    """
    sql = """
        SELECT period, value
        FROM bulletin_observations
        WHERE dataset = ? AND entity_key = ? AND currency = ?
    """
    params = [dataset, entity_key, currency]
    if metric is not None:
        sql += " AND metric = ?"
        params.append(metric)
    sql += " ORDER BY period"

    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        frame = con.execute(sql, params).df()
    finally:
        con.close()

    if frame.empty:
        raise ValueError(f"no rows for dataset={dataset!r} entity_key={entity_key!r} currency={currency!r}")
    if frame.period.duplicated().any():
        raise ValueError("more than one row per period -- pass metric= to disambiguate")

    return pd.Series(frame.value.astype(float).values,
                     index=pd.to_datetime(frame.period), name=entity_key)