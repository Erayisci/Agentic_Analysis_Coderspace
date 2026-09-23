"""The one outlier rule shared by the analytical tools.

A point is an outlier only when two independent checks agree: its rolling
z-score breaches `z_threshold` AND it lies outside the rolling IQR fence.
Turkish macro/regulatory data contains genuine structural breaks (a regime
shift is not an anomaly), so a single-method flag is weak evidence on its own;
requiring agreement is the mitigation Launch.MD calls for.

What is scored depends on what the series is (`scored_series`). A stock
(every balance in `bulletin_observations`) trends upward every month, so its
level would flag the whole tail; its period-over-period percent change is what
can break from history. A rate is already a percentage, and the percent change
of a percentage ("faiz %10 arttı" for 18.4 -> 20.2) is a number nobody quotes
-- its point difference is (`on="diff"`). A flow can pass through zero, where a
percent change explodes; those periods are left unscored and counted rather
than reported as thousand-percent anomalies. `on="auto"` picks by the series'
declared `temporal_semantics`.

The baseline is strictly TRAILING: the point being scored is compared with the
`window` observations *before* it, never with itself. Measured on the housing
loan series, an inclusive window contaminated its own mean and variance enough
to hide both real breaks (2023-03, 2024-10); a trailing window also keeps a
past flag stable when new months arrive.

A flagged point is then CLASSIFIED by what happens next (`classify_flags`):
the rule compares a point with its past only, so the first observation of a
new regime looks exactly like an outlier. If the following observations come
back inside the fence it was a spike; if they stay outside on the same side it
is a regime start (a change point, not an anomaly); at the very end of the
series, with nothing after it, it is undetermined.

This module is the scoring half of `tools.anomaly.detect_anomalies`, extracted
so that `tools.change_detection` applies the very same rule before it looks
for breaks -- both tools agree on what an outlier is, and
`tests/test_outliers.py` locks the wrapper and the rule to the same periods on
real data.
"""
from typing import Dict, NamedTuple, Optional, Tuple

import numpy as np
import pandas as pd

from .frequency import spans_in_periods

SCORED_ON = ("auto", "change", "diff", "level")
MIN_WINDOW = 3
KINDS = ("spike", "regime_start", "undetermined")

# Relative to the series' median magnitude: a previous value this small makes
# a percent change meaningless (a flow crossing zero), so the period is not
# scored rather than scored as +3000%.
FLOW_ZERO_GUARD = 0.05


class OutlierScores(NamedTuple):
    """Per-point scores over the scoreable part of a series (NaNs dropped)."""

    scored: pd.Series        # what was scored: pct change, point difference or the values
    roll_mean: pd.Series
    roll_std: pd.Series
    z_score: pd.Series       # NaN where no full window of history exists
    lower_fence: pd.Series
    upper_fence: pd.Series
    is_outlier: pd.Series    # bool; True only where BOTH checks breach
    scored_on: str = "change"   # "change" | "diff" | "level" -- what `scored` holds
    scored_unit: str = "%"      # "%" | "puan" | "seviye"
    n_unscored: int = 0         # periods left unscored by the flow zero guard

    @property
    def periods(self):
        return list(self.scored.index[self.is_outlier])


def scoring_for(semantics: Optional[str]) -> str:
    """Which quantity to score for a series with these temporal semantics."""
    if semantics in ("rate", "ratio"):
        return "diff"
    return "change"


def validate_params(on: str, window: int) -> None:
    if on not in SCORED_ON:
        raise ValueError(f"on must be one of {SCORED_ON}, got {on!r}")
    if window < MIN_WINDOW:
        raise ValueError(f"window must be >= {MIN_WINDOW}")


def scored_series(series: pd.Series, on: str, semantics: Optional[str] = None
                  ) -> Tuple[pd.Series, str, str, int]:
    """(scored values, the name of what was scored, its unit, n left unscored).

    A change or difference across a gap (a missing observation) spans several
    periods; it is divided by the periods spanned so it stays "per period"
    instead of doubling after every hole in the data.
    """
    if on == "auto":
        on = scoring_for(semantics)
    clean = series.dropna()
    if on == "level":
        return clean, "level", "seviye", 0
    spans = spans_in_periods(clean.index) if len(clean) >= 3 else None
    if on == "diff":
        diffed = clean.diff()
        if spans is not None:
            diffed.iloc[1:] = diffed.iloc[1:] / spans
        return diffed.dropna(), "diff", "puan", 0
    pct = clean.pct_change(fill_method=None) * 100
    if spans is not None:
        pct.iloc[1:] = pct.iloc[1:] / spans
    unscored = 0
    if semantics == "flow":
        guard = FLOW_ZERO_GUARD * float(clean.abs().median() or 0)
        tiny = clean.shift(1).abs() < guard
        unscored = int((tiny & pct.notna()).sum())
        pct = pct.mask(tiny)
    return pct.dropna(), "change", "%", unscored


def score_outliers(series: pd.Series, on: str = "auto", window: int = 12,
                   z_threshold: float = 3.0, iqr_multiplier: float = 1.5,
                   semantics: Optional[str] = None) -> OutlierScores:
    """Score every point of `series` against its own trailing `window` of history.

    A point needs a full window behind it to be scored at all -- there is no
    partial-window comparison. Raises ValueError when the series has too few
    scoreable points for one full window.
    """
    validate_params(on, window)
    scored, scored_on, scored_unit, n_unscored = scored_series(series, on, semantics)

    if len(scored) <= window:
        raise ValueError(
            f"series has {len(scored)} scoreable point(s), need more than "
            f"window={window} to compute a rolling baseline"
        )

    # shift(1): the baseline is the `window` points before this one.
    history = scored.shift(1).rolling(window, min_periods=window)
    roll_mean = history.mean()
    roll_std = history.std(ddof=0)
    z_score = (scored - roll_mean) / roll_std.replace(0, np.nan)

    roll_q1 = history.quantile(0.25)
    roll_q3 = history.quantile(0.75)
    roll_iqr = roll_q3 - roll_q1
    lower_fence = roll_q1 - iqr_multiplier * roll_iqr
    upper_fence = roll_q3 + iqr_multiplier * roll_iqr

    z_flag = z_score.abs() >= z_threshold
    iqr_flag = (scored < lower_fence) | (scored > upper_fence)
    is_outlier = (z_flag & iqr_flag).fillna(False).astype(bool)

    return OutlierScores(scored, roll_mean, roll_std, z_score, lower_fence, upper_fence, is_outlier,
                         scored_on, scored_unit, n_unscored)


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
