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

import pandas as pd

from .frequency import find_gaps, infer_frequency
from .outliers import classify_flags, default_lookahead, score_outliers, validate_params
from .series import load_series


def _score(series: pd.Series, on: str, window: int, z_threshold: float, iqr_multiplier: float,
           lookahead: Optional[int] = None) -> dict:
    """The method itself, over a plain series -- shared by both entry points
    below so "loaded from the lakehouse" and "already have it in hand" (an
    external or derived column) can never silently score differently.

    The maths lives in `tools.outliers.score_outliers` so that change
    detection applies the very same rule before it looks for breaks; each
    flag is then classified by `tools.outliers.classify_flags` as a one-off
    `spike`, a `regime_start` (the series stays outside the fence afterwards
    -- a change point, not an anomaly) or `undetermined` (nothing after it).
    """
    validate_params(on, window)
    scores = score_outliers(series, on=on, window=window, z_threshold=z_threshold,
                            iqr_multiplier=iqr_multiplier)
    scored, roll_mean, z_score = scores.scored, scores.roll_mean, scores.z_score
    if lookahead is None:
        lookahead = default_lookahead(window)
    kinds = classify_flags(scores, lookahead)
    fmt = infer_frequency(series.index)[2] if len(series) >= 3 else "%Y-%m"   # YYYY-MM-DD for weekly/daily
    gaps = find_gaps(series.index, fmt) if len(series) >= 3 else []

    anomalies = []
    for period in scores.periods:
        anomalies.append({
            "period": period.strftime(fmt),
            "scored_value": round(float(scored.loc[period]), 6),
            "raw_value": float(series.loc[period]),
            "z_score": round(float(z_score.loc[period]), 3),
            "direction": "above" if scored.loc[period] > roll_mean.loc[period] else "below",
            "kind": kinds[period],
        })

    return {
        "scored_on": on,
        "window": window,
        "z_threshold": z_threshold,
        "iqr_multiplier": iqr_multiplier,
        "lookahead": lookahead,
        "period_start": series.index.min().strftime(fmt),
        "period_end": series.index.max().strftime(fmt),
        "n_points": int(len(series)),
        "n_scored": int(len(scored)),
        "n_anomalies": len(anomalies),
        "kinds": {k: sum(a["kind"] == k for a in anomalies) for k in ("spike", "regime_start", "undetermined")},
        "gaps": gaps,
        "anomalies": anomalies,
    }


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
    lookahead: Optional[int] = None,
) -> dict:
    """Flag periods where a series' rolling z-score AND IQR fence both breach.

    Args:
        key, source, dataset, currency, metric: identify the series, passed
            straight through to `load_series` -- `key` is an entity_key for the
            bulletin and weekly corpora and a series_code for macro. Only for
            a series the lakehouse actually holds; an external or derived
            column has no lakehouse row to load and goes through
            `detect_anomalies_in_series` instead.
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
    loaded = load_series(key, source=source, dataset=dataset, currency=currency, metric=metric)
    return {**loaded.describe(), "citation": loaded.citation(),
            **_score(loaded.values, on, window, z_threshold, iqr_multiplier, lookahead)}


def detect_anomalies_in_series(
    series: pd.Series,
    describe: dict,
    citation: dict,
    on: str = "change",
    window: int = 12,
    z_threshold: float = 3.0,
    iqr_multiplier: float = 1.5,
    lookahead: Optional[int] = None,
) -> dict:
    """Same method as `detect_anomalies`, for a series that is already in
    hand rather than loadable from the lakehouse -- an `ingest_external`
    column (its only copy lives on the artifact, not in any table) or a
    `transform`-derived one.

    `describe`/`citation` are carried through verbatim into the result
    (mirroring `SeriesResult.describe()`/`.citation()`'s shape) so the caller
    states what the series actually is instead of this function guessing.
    """
    return {**describe, "citation": citation, **_score(series, on, window, z_threshold, iqr_multiplier, lookahead)}
