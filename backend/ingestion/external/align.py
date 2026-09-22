"""Native observations of an external series -> the monthly grain.

The same aggregation the EVDS path applies (`transform.macro.aggregate_monthly`):
`value_avg`, `value_last` and `value_sum` are all kept, `value` follows the
series' `monthly_rule`, and `value_sum` is nulled for anything that is not a
flow so nobody reads it as meaningful. A rule is declared per series by the
label heuristics, never chosen at query time.
"""
import pandas as pd

from ...transform.macro import aggregate_monthly

FREQUENCY_BY_MEDIAN_DAYS = (
    (2.0, "daily"), (9.0, "weekly"), (35.0, "monthly"), (95.0, "quarterly"), (400.0, "annual"),
)


def infer_frequency(index: pd.DatetimeIndex) -> str:
    """From the median spacing of the dates; 'irregular' when nothing fits."""
    stamps = pd.DatetimeIndex(index).sort_values().unique()
    if len(stamps) < 2:
        return "irregular"
    median = pd.Series(stamps[1:] - stamps[:-1]).dt.days.median()
    for ceiling, name in FREQUENCY_BY_MEDIAN_DAYS:
        if median <= ceiling:
            return name
    return "irregular"


def to_monthly(native: pd.Series, series_key: str, source_id: str, rule: str, grain: str) -> pd.DataFrame:
    """One row per month with the schema of `external_observations`."""
    frame = pd.DataFrame({
        "date": pd.DatetimeIndex(native.index), "series_key": series_key, "value": native.to_numpy(dtype=float),
        "grain": grain,
    })
    monthly = aggregate_monthly(frame, key="series_key", rules=pd.Series({series_key: rule}), carry=["grain"])
    monthly["source_id"] = source_id
    return monthly[["period", "series_key", "source_id", "value", "value_avg", "value_last",
                    "value_sum", "n_native_obs", "monthly_rule"]]
