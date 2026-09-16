"""Detect outliers and deviations from expected behaviour in a lakehouse series.

Two independent statistical checks run over the same series -- a rolling
z-score and a rolling IQR fence -- and a point is only reported as an anomaly
when BOTH agree. Turkish macro/regulatory data contains genuine structural
breaks (a regime shift is not an anomaly), so a single-method flag is weak
evidence on its own; requiring agreement is the mitigation Launch.MD calls for
("Anomaly output is contextualised against historical windows ... before
being reported as an anomaly").

Scoring runs on the period-over-period change by default, not the raw level.
Every value in `bulletin_observations` is a period-end outstanding balance
(a stock): a steadily growing book trends upward every month, which would
make a level-based z-score flag the entire tail of the series as anomalous.
Scoring the change isolates months where the *rate* of change itself broke
from its own recent history.

The series is loaded through `tools.series.load_series`, so the corpus-specific
traps are already handled before scoring: a year-to-date series arrives
de-cumulated (otherwise every January is an "anomaly" that is really a reset),
a retired weekly item is refused rather than silently duplicated, and the unit
and temporal semantics travel with the result so the caller can state what the
flagged numbers are.
"""
from typing import Optional

import numpy as np

from .series import load_series


def detect_anomalies(
    key: str,
    source: str = "bulletin",
    dataset: Optional[str] = None,
    currency: Optional[str] = "total",
    metric: Optional[str] = None,
    on: str = "change",
    window: int = 12,
    z_threshold: float = 3.0,
    iqr_multiplier: float = 1.5,
) -> dict:
    """Flag periods where a series' rolling z-score AND IQR fence both breach.

    Args:
        key, source, dataset, currency, metric: identify the series, passed
            straight through to `load_series` -- `key` is an entity_key for the
            bulletin and weekly corpora and a series_code for macro.
        on: "change" scores the period-over-period percent change (default);
            "level" scores the raw values as loaded.
        window: trailing window, in periods, for the rolling mean/std and
            quartiles. A point needs a full window of history behind it to be
            scored at all -- there is no partial-window comparison, since that
            would compare a point against too few precedents to mean anything.
        z_threshold: |z| at or above this flags the z-score check.
        iqr_multiplier: fence width, in IQRs beyond Q1/Q3, for the IQR check.

    Returns a JSON-serialisable dict: identifies the series and parameters
    used, then `anomalies` -- one entry per period flagged by both checks,
    each carrying the scored value, the raw level it came from, the z-score
    and which side of the fence it breached.
    """
    if on not in ("change", "level"):
        raise ValueError(f"on must be 'change' or 'level', got {on!r}")
    if window < 3:
        raise ValueError("window must be >= 3")

    loaded = load_series(key, source=source, dataset=dataset, currency=currency, metric=metric)
    series = loaded.values

    scored = series.pct_change(fill_method=None) * 100 if on == "change" else series
    scored = scored.dropna()

    if len(scored) <= window:
        raise ValueError(
            f"series has {len(scored)} scoreable point(s), need more than "
            f"window={window} to compute a rolling baseline"
        )

    roll_mean = scored.rolling(window, min_periods=window).mean()
    roll_std = scored.rolling(window, min_periods=window).std(ddof=0)
    z_score = (scored - roll_mean) / roll_std.replace(0, np.nan)

    roll_q1 = scored.rolling(window, min_periods=window).quantile(0.25)
    roll_q3 = scored.rolling(window, min_periods=window).quantile(0.75)
    roll_iqr = roll_q3 - roll_q1
    lower_fence = roll_q1 - iqr_multiplier * roll_iqr
    upper_fence = roll_q3 + iqr_multiplier * roll_iqr

    z_flag = z_score.abs() >= z_threshold
    iqr_flag = (scored < lower_fence) | (scored > upper_fence)
    is_anomaly = (z_flag & iqr_flag).fillna(False)

    anomalies = []
    for period in scored.index[is_anomaly]:
        anomalies.append({
            "period": period.strftime("%Y-%m"),
            "scored_value": round(float(scored.loc[period]), 6),
            "raw_value": float(series.loc[period]),
            "z_score": round(float(z_score.loc[period]), 3),
            "direction": "above" if scored.loc[period] > roll_mean.loc[period] else "below",
        })

    return {
        **loaded.describe(),
        "citation": loaded.citation(),
        "scored_on": on,
        "window": window,
        "z_threshold": z_threshold,
        "iqr_multiplier": iqr_multiplier,
        "period_start": series.index.min().strftime("%Y-%m"),
        "period_end": series.index.max().strftime("%Y-%m"),
        "n_points": int(len(series)),
        "n_scored": int(len(scored)),
        "n_anomalies": len(anomalies),
        "anomalies": anomalies,
    }
