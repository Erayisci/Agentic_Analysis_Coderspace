"""The one outlier rule shared by the analytical tools.

A point is an outlier only when two independent checks agree: its rolling
z-score breaches `z_threshold` AND it lies outside the rolling IQR fence.
Turkish macro/regulatory data contains genuine structural breaks (a regime
shift is not an anomaly), so a single-method flag is weak evidence on its own;
requiring agreement is the mitigation Launch.MD calls for.

A flagged point is then CLASSIFIED by what happens next (`classify_flags`):
the rule compares a point with its past only, so the first observation of a
new regime looks exactly like an outlier. If the following observations come
back inside the fence it was a spike; if they stay outside on the same side it
is a regime start (a change point, not an anomaly); at the very end of the
series, with nothing after it, it is undetermined.

This module is the extraction of the scoring half of
`tools.anomaly.detect_anomalies` (İlmay's tool), plus the classification both
tools agreed to share. `detect_anomalies` remains the key-in wrapper that loads a series
and reports the flagged points; `tools.change_detection` calls `score_outliers`
on a series it already holds so that both tools agree on what an outlier is.
The maths here must stay identical to what `detect_anomalies` produced before
the split -- `tests/test_outliers.py` locks that equivalence on real data.
"""
from typing import Dict, NamedTuple, Optional

import numpy as np
import pandas as pd

from .frequency import spans_in_periods

SCORED_ON = ("change", "level")
MIN_WINDOW = 3
KINDS = ("spike", "regime_start", "undetermined")


class OutlierScores(NamedTuple):
    """Per-point scores over the scoreable part of a series (NaNs dropped)."""

    scored: pd.Series        # what was scored: pct change (on="change") or the values (on="level")
    roll_mean: pd.Series
    roll_std: pd.Series
    z_score: pd.Series       # NaN where no full window of history exists
    lower_fence: pd.Series
    upper_fence: pd.Series
    is_outlier: pd.Series    # bool; True only where BOTH checks breach

    @property
    def periods(self):
        return list(self.scored.index[self.is_outlier])


def validate_params(on: str, window: int) -> None:
    if on not in SCORED_ON:
        raise ValueError(f"on must be 'change' or 'level', got {on!r}")
    if window < MIN_WINDOW:
        raise ValueError(f"window must be >= {MIN_WINDOW}")


def score_outliers(series: pd.Series, on: str = "change", window: int = 12,
                   z_threshold: float = 3.0, iqr_multiplier: float = 1.5) -> OutlierScores:
    """Score every point of `series` against its own trailing `window` of history.

    on="change" scores the period-over-period percent change, per period
    even across a gap in the dates; on="level" scores the values as given.
    A point needs a full window behind it to be scored at all -- there is no
    partial-window comparison. Raises ValueError when the
    series has too few scoreable points for one full window.
    """
    validate_params(on, window)

    if on == "change":
        scored = series.pct_change(fill_method=None) * 100
        # Across a gap (a missing observation) the change spans several periods;
        # divide by the periods spanned so it stays "change per period" instead
        # of doubling after every hole in the data.
        if len(series) >= 3:
            scored.iloc[1:] = scored.iloc[1:] / spans_in_periods(series.index)
    else:
        scored = series
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
    is_outlier = (z_flag & iqr_flag).fillna(False).astype(bool)

    return OutlierScores(scored, roll_mean, roll_std, z_score, lower_fence, upper_fence, is_outlier)


def default_lookahead(window: int) -> int:
    """How many following observations decide spike vs regime start: about a
    quarter of the window (3 for a 12-month window, 13 for a 52-week one)."""
    return max(2, round(window / 4))


def classify_flags(scores: OutlierScores, lookahead: Optional[int] = None) -> Dict:
    """Classify every flagged point of `scores` as spike / regime_start / undetermined.

    A flagged point is a `spike` if fewer than half of the next `lookahead`
    observations are still outside the fence on the same side; `regime_start`
    if at least half are; `undetermined` if there are no following observations.
    Returns {period: kind} for the flagged periods only.
    """
    scored = scores.scored
    result: Dict = {}
    for period in scores.periods:
        pos = scored.index.get_loc(period)
        window_ahead = lookahead if lookahead is not None else 0
        following = scored.iloc[pos + 1: pos + 1 + window_ahead]
        if following.empty:
            result[period] = "undetermined"
            continue
        above = scored.loc[period] > scores.roll_mean.loc[period]
        fence = scores.upper_fence.loc[period] if above else scores.lower_fence.loc[period]
        still_out = (following > fence) if above else (following < fence)
        result[period] = "regime_start" if still_out.mean() >= 0.5 else "spike"
    return result
