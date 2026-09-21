"""Frequency of a dated series, inferred from its index -- shared by the tools.

Nothing here assumes monthly data: the spacing is measured (median gap between
consecutive dates, robust to a missing observation), classified, and used to
choose the date format for reporting and to detect gaps.
"""
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

# (name, periods per year, strftime format for reporting)
FREQUENCIES = (
    ("day", 365, "%Y-%m-%d"),
    ("week", 52, "%Y-%m-%d"),
    ("month", 12, "%Y-%m"),
    ("quarter", 4, "%Y-%m"),
    ("year", 1, "%Y"),
)


def _gaps_in_days(index: pd.DatetimeIndex) -> np.ndarray:
    return np.diff(index.values).astype("timedelta64[h]").astype(float) / 24.0


def infer_frequency(index: pd.DatetimeIndex) -> Tuple[str, int, str]:
    """Median spacing of the index -> (name, periods_per_year, date_format)."""
    if len(index) < 3:
        raise ValueError("need at least 3 observations to infer a frequency")
    days = float(np.median(_gaps_in_days(index)))
    if days <= 1.5:
        return FREQUENCIES[0]
    if days <= 8:
        return FREQUENCIES[1]
    if days <= 35:
        return FREQUENCIES[2]
    if days <= 100:
        return FREQUENCIES[3]
    return FREQUENCIES[4]


def spans_in_periods(index: pd.DatetimeIndex) -> np.ndarray:
    """For each consecutive pair of dates, how many periods apart they are
    (1 normally; 2 when one observation is missing between them, and so on)."""
    gaps = _gaps_in_days(index)
    return np.maximum(1, np.round(gaps / np.median(gaps))).astype(int)


def find_gaps(index: pd.DatetimeIndex, fmt: str) -> List[Dict]:
    """Every place where more than one period separates consecutive dates."""
    spans = spans_in_periods(index)
    return [{"after": index[i].strftime(fmt), "before": index[i + 1].strftime(fmt),
             "missing_periods": int(spans[i] - 1)}
            for i in np.flatnonzero(spans > 1)]
