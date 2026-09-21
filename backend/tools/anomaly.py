"""Detect outliers and deviations from expected behaviour in a lakehouse series.

Two independent statistical checks run over the same series -- a rolling
z-score and a rolling IQR fence -- and a point is only reported as an anomaly
when BOTH agree. Turkish macro/regulatory data contains genuine structural
breaks (a regime shift is not an anomaly), so a single-method flag is weak
evidence on its own; requiring agreement is the mitigation Launch.MD calls for
("Anomaly output is contextualised against historical windows ... before
being reported as an anomaly").

What is scored depends on what the series is. A stock (every balance in
`bulletin_observations`) trends upward every month, so its level would flag
the whole tail; its month-over-month percent change is what can break from
history. A rate is already a percentage, and the percent change of a
percentage ("faiz %10 arttı" for 18.4 -> 20.2) is a number nobody quotes --
its point difference is (`on="diff"`). A flow can pass through zero, where a
percent change explodes; those months are left unscored and counted rather
than reported as thousand-percent anomalies. `on="auto"` picks by the
series' declared `temporal_semantics`.

The baseline is strictly trailing: the point being scored is compared with
the `window` months *before* it, never with itself. Measured on the housing
loan series, an inclusive window contaminated its own mean and variance
enough to hide both real breaks (2023-03, 2024-10); a trailing window also
keeps a past flag stable when new months arrive.

The series is loaded through `tools.series.load_series`, so the corpus-specific
traps are already handled before scoring: a year-to-date series arrives
de-cumulated (otherwise every January is an "anomaly" that is really a reset),
a retired weekly item is refused rather than silently duplicated, and the unit
and temporal semantics travel with the result so the caller can state what the
flagged numbers are.
"""
from typing import Optional

import numpy as np
import pandas as pd

from .series import load_series

SCORING = ("auto", "change", "diff", "level")

# Relative to the series' median magnitude: a previous value this small makes
# a percent change meaningless (a flow crossing zero), so the month is not
# scored rather than scored as +3000%.
FLOW_ZERO_GUARD = 0.05


def scoring_for(semantics: Optional[str]) -> str:
    """Which quantity to score for a series with these temporal semantics."""
    if semantics in ("rate", "ratio"):
        return "diff"
    return "change"


def _scored_series(series: pd.Series, on: str, semantics: Optional[str]) -> tuple:
    """(scored values, the name of what was scored, its unit, n left unscored)."""
    if on == "auto":
        on = scoring_for(semantics)
    if on == "level":
        return series.dropna(), "level", "seviye", 0
    if on == "diff":
        return series.diff().dropna(), "diff", "puan", 0
    pct = series.pct_change(fill_method=None) * 100
    unscored = 0
    if semantics == "flow":
        guard = FLOW_ZERO_GUARD * float(series.abs().median() or 0)
        tiny = series.shift(1).abs() < guard
        unscored = int((tiny & pct.notna()).sum())
        pct = pct.mask(tiny)
    return pct.dropna(), "change", "%", unscored


def _score(series: pd.Series, on: str, window: int, z_threshold: float, iqr_multiplier: float,
           semantics: Optional[str] = None, name: str = "seri") -> dict:
    """The method itself, over a plain series -- shared by both entry points
    below so "loaded from the lakehouse" and "already have it in hand" (an
    external or derived column) can never silently score differently."""
    if on not in SCORING:
        raise ValueError(f"on must be one of {SCORING}, got {on!r}")
    if window < 3:
        raise ValueError("window must be >= 3")

    scored, scored_on, scored_unit, n_unscored = _scored_series(series, on, semantics)

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

    what = {"change": "aylik % degisimi", "diff": "aylik puan farki", "level": "seviyesi"}[scored_on]
    months = ", ".join(a["period"] for a in anomalies[:6])
    description = (f"{name}: {what}, onceki {window} ayin ortalamasindan |z|>={z_threshold:g} VE "
                   f"IQR x{iqr_multiplier:g} disina cikan aykiri aylar ({series.index.min():%Y-%m}.."
                   f"{series.index.max():%Y-%m}) -- {len(anomalies)} ay bulundu"
                   + (f": {months}" if months else ""))

    return {
        "scored_on": scored_on,
        "scored_unit": scored_unit,
        "window": window,
        "z_threshold": z_threshold,
        "iqr_multiplier": iqr_multiplier,
        "period_start": series.index.min().strftime("%Y-%m"),
        "period_end": series.index.max().strftime("%Y-%m"),
        "n_points": int(len(series)),
        "n_scored": int(len(scored)),
        "n_unscored": n_unscored,
        "n_anomalies": len(anomalies),
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

    Returns a JSON-serialisable dict: identifies the series and parameters
    used, then `anomalies` -- one entry per period flagged by both checks,
    each carrying the scored value, the raw level it came from, the z-score
    and which side of the fence it breached -- and a Turkish `description`
    that says what the list means.
    """
    loaded = load_series(key, source=source, dataset=dataset, currency=currency, metric=metric)
    return {**loaded.describe(), "citation": loaded.citation(),
            **_score(loaded.values, on, window, z_threshold, iqr_multiplier,
                     semantics=loaded.temporal_semantics, name=loaded.name)}


def detect_anomalies_in_series(
    series: pd.Series,
    describe: dict,
    citation: dict,
    on: str = "auto",
    window: int = 12,
    z_threshold: float = 3.0,
    iqr_multiplier: float = 1.5,
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
                     name=describe.get("name") or describe.get("value_column") or "seri")}
