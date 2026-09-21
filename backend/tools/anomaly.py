"""Detect outliers and deviations from expected behaviour in a lakehouse series.

Two independent statistical checks run over the same series -- a rolling
z-score and a rolling IQR fence -- and a point is only reported as an anomaly
when BOTH agree. Turkish macro/regulatory data contains genuine structural
breaks (a regime shift is not an anomaly), so a single-method flag is weak
evidence on its own; requiring agreement is the mitigation Launch.MD calls for
("Anomaly output is contextualised against historical windows ... before
being reported as an anomaly").

The maths lives in `tools.outliers` so that change detection applies the very
same rule before it looks for breaks. What that rule scores depends on what the
series is (`on="auto"`: the point difference of a rate, the percent change of
everything else, with a zero guard for flows), and its baseline is strictly
trailing -- see that module for why both were measured rather than assumed.

Each flag is then classified by `tools.outliers.classify_flags` as a one-off
`spike`, a `regime_start` (the series stays outside the fence afterwards -- a
change point, not an anomaly) or `undetermined` (nothing after it), and the
result also lists any gaps in the dates so a hole is never read as a jump.

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
from .outliers import (SCORED_ON, classify_flags, default_lookahead, score_outliers, scoring_for,  # noqa: F401
                       validate_params)
from .series import load_series

SCORING = SCORED_ON

PERIOD_WORD = {"day": "gun", "week": "hafta", "month": "ay", "quarter": "ceyrek", "year": "yil"}


def _score(series: pd.Series, on: str, window: int, z_threshold: float, iqr_multiplier: float,
           semantics: Optional[str] = None, name: str = "seri", lookahead: Optional[int] = None) -> dict:
    """The method itself, over a plain series -- shared by both entry points
    below so "loaded from the lakehouse" and "already have it in hand" (an
    external or derived column) can never silently score differently."""
    validate_params(on, window)
    scores = score_outliers(series, on=on, window=window, z_threshold=z_threshold,
                            iqr_multiplier=iqr_multiplier, semantics=semantics)
    scored, roll_mean, z_score = scores.scored, scores.roll_mean, scores.z_score
    if lookahead is None:
        lookahead = default_lookahead(window)
    kinds = classify_flags(scores, lookahead)
    freq_name, _, fmt = infer_frequency(series.index) if len(series) >= 3 else ("month", 12, "%Y-%m")
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

    per = PERIOD_WORD.get(freq_name, "donem")
    what = {"change": f"{per}lik % degisimi", "diff": f"{per}lik puan farki", "level": "seviyesi"}[scores.scored_on]
    months = ", ".join(a["period"] for a in anomalies[:6])
    description = (f"{name}: {what}, onceki {window} {per}in ortalamasindan |z|>={z_threshold:g} VE "
                   f"IQR x{iqr_multiplier:g} disina cikan aykiri {per}lar "
                   f"({series.index.min().strftime(fmt)}..{series.index.max().strftime(fmt)}) "
                   f"-- {len(anomalies)} {per} bulundu"
                   + (f": {months}" if months else ""))

    return {
        "scored_on": scores.scored_on,
        "scored_unit": scores.scored_unit,
        "window": window,
        "z_threshold": z_threshold,
        "iqr_multiplier": iqr_multiplier,
        "lookahead": lookahead,
        "frequency": freq_name,
        "period_start": series.index.min().strftime(fmt),
        "period_end": series.index.max().strftime(fmt),
        "n_points": int(len(series)),
        "n_scored": int(len(scored)),
        "n_unscored": scores.n_unscored,
        "n_anomalies": len(anomalies),
        "kinds": {k: sum(a["kind"] == k for a in anomalies) for k in ("spike", "regime_start", "undetermined")},
        "gaps": gaps,
        "anomalies": anomalies,
        "description": description,
    }


def detect_anomalies(
    key: str,
    source: str = "bulletin",
    dataset: Optional[str] = None,
    currency: Optional[str] = "total",
    metric: Optional[str] = None,
    on: str = "auto",
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
        on: "auto" (default) scores by the series' semantics -- the point
            difference of a rate, the percent change of everything else;
            "change", "diff" and "level" force one.
        window: trailing window, in periods, for the rolling mean/std and
            quartiles. A point needs a full window of history behind it to be
            scored at all -- there is no partial-window comparison, since that
            would compare a point against too few precedents to mean anything.
        z_threshold: |z| at or above this flags the z-score check.
        iqr_multiplier: fence width, in IQRs beyond Q1/Q3, for the IQR check.
        lookahead: observations after a flag that decide spike vs regime
            start; defaults to about a quarter of the window.

    Returns a JSON-serialisable dict: identifies the series and parameters
    used, then `anomalies` -- one entry per period flagged by both checks,
    each carrying the scored value, the raw level it came from, the z-score,
    which side of the fence it breached and its `kind` -- and a Turkish
    `description` that says what the list means.
    """
    loaded = load_series(key, source=source, dataset=dataset, currency=currency, metric=metric)
    return {**loaded.describe(), "citation": loaded.citation(),
            **_score(loaded.values, on, window, z_threshold, iqr_multiplier,
                     semantics=loaded.temporal_semantics, name=loaded.name, lookahead=lookahead)}


def detect_anomalies_in_series(
    series: pd.Series,
    describe: dict,
    citation: dict,
    on: str = "auto",
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
    states what the series actually is instead of this function guessing;
    `describe["temporal_semantics"]` is what `on="auto"` reads.
    """
    return {**describe, "citation": citation,
            **_score(series, on, window, z_threshold, iqr_multiplier,
                     semantics=describe.get("temporal_semantics"),
                     name=describe.get("name") or describe.get("value_column") or "seri",
                     lookahead=lookahead)}
