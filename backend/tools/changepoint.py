"""Locate level shifts in a series: where its mean moved and stayed moved.

PELT (`ruptures`) with an L2 cost over the z-scored level. Two choices are
deliberate and both were measured on the housing-loan rate `TP.KTF12`:

- **The penalty scales with the series, not a constant.** A fixed `pen=5`
  found the 2023-07 regime shift only because that series happens to be 67
  points long; the number of breaks it reports on a longer or shorter window
  is then a function of the window. `pen = penalty_scale * ln(n)` on
  standardised data is the BIC shape, unit-free, and finds 2023-07 for every
  scale between 1 and 3.
- **The cost is on the level, and the result says what shifted.** A break is
  reported with the mean before and after and the size of the shift in the
  series' own terms -- points for a rate, percent for a balance -- because
  "there is a breakpoint at 2023-07" is not a finding until it says which way
  and by how much.

A stock grows every month, so any long enough window of it contains "breaks"
that are only the trend crossing the penalty. `segments` therefore also carry
a slope per month, and the description names the largest shift rather than
listing every cut, so the composer does not read a staircase as three regime
changes.

Shares the anomaly tool's history policy: the executor hands it the column's
full lakehouse history rather than the plan's window, so a break just before
the window's start is not mistaken for the level the window opens at.
"""
import math
from typing import Optional

import numpy as np
import pandas as pd

from .series import load_series

MIN_POINTS = 10


def detect_changepoints_in_series(series: pd.Series, describe: dict, citation: dict,
                                  min_size: int = 6, penalty_scale: float = 2.0) -> dict:
    """PELT level-shift detection over a series already in hand.

    `describe`/`citation` carry what the series is (unit, semantics, name),
    which decides whether a shift is reported in points or percent.
    """
    import ruptures

    series = series.dropna()
    n = int(len(series))
    if n < MIN_POINTS:
        raise ValueError(f"series has {n} points; need at least {MIN_POINTS} for change detection")
    if min_size < 2:
        raise ValueError("min_size must be >= 2")

    values = series.to_numpy(dtype=float)
    spread = float(values.std()) or 1.0
    standardised = ((values - values.mean()) / spread).reshape(-1, 1)
    penalty = penalty_scale * math.log(n)
    cuts = ruptures.Pelt(model="l2", min_size=min_size, jump=1).fit(standardised).predict(pen=penalty)
    breaks = [i for i in cuts if 0 < i < n]

    semantics = describe.get("temporal_semantics")
    unit = describe.get("unit") or ""
    in_points = semantics in ("rate", "ratio") or unit == "%"
    shift_unit = "puan" if in_points else "%"

    segments, previous = [], 0
    for cut in breaks + [n]:
        block = series.iloc[previous:cut]
        x = np.arange(len(block), dtype=float)
        slope = float(np.polyfit(x, block.to_numpy(dtype=float), 1)[0]) if len(block) > 1 else 0.0
        segments.append({"from": block.index[0].strftime("%Y-%m"), "to": block.index[-1].strftime("%Y-%m"),
                         "n": int(len(block)), "mean": round(float(block.mean()), 4),
                         "slope_per_month": round(slope, 4)})
        previous = cut

    breakpoints = []
    for i, cut in enumerate(breaks):
        before, after = segments[i]["mean"], segments[i + 1]["mean"]
        shift = after - before if in_points else (100 * (after / before - 1) if before else float("nan"))
        breakpoints.append({"period": series.index[cut].strftime("%Y-%m"),
                            "before_mean": before, "after_mean": after,
                            "shift": round(float(shift), 4), "shift_unit": shift_unit,
                            "direction": "up" if after > before else "down"})

    name = describe.get("name") or describe.get("value_column") or "seri"
    span = f"{series.index.min():%Y-%m}..{series.index.max():%Y-%m}"
    if breakpoints:
        largest = max(breakpoints, key=lambda b: abs(b["shift"]) if not math.isnan(b["shift"]) else -1)
        listed = ", ".join(b["period"] for b in breakpoints)
        description = (f"{name} seviyesinde {len(breakpoints)} kirilma ({span}): {listed}. En buyugu "
                       f"{largest['period']}: ortalama {largest['before_mean']:g} -> {largest['after_mean']:g} "
                       f"({largest['shift']:+g} {shift_unit}, {'yukari' if largest['direction'] == 'up' else 'asagi'})")
    else:
        description = f"{name} seviyesinde kirilma bulunmadi ({span}): tek rejim"

    return {
        **describe, "citation": citation,
        "method": f"PELT (l2, z-skor, pen={penalty_scale:g}*ln n={penalty:.2f}, min_size={min_size})",
        "penalty": round(penalty, 4),
        "period_start": series.index.min().strftime("%Y-%m"),
        "period_end": series.index.max().strftime("%Y-%m"),
        "n_points": n,
        "n_breakpoints": len(breakpoints),
        "breakpoints": breakpoints,
        "segments": segments,
        "shift_unit": shift_unit,
        "description": description,
    }


def detect_changepoints(key: str, source: str = "bulletin", dataset: Optional[str] = None,
                        currency: Optional[str] = "total", metric: Optional[str] = None,
                        min_size: int = 6, penalty_scale: float = 2.0) -> dict:
    """Level shifts in a lakehouse series, loaded through `load_series` so the
    corpus traps (de-cumulation, retired items, currency splits) are handled
    before detection."""
    loaded = load_series(key, source=source, dataset=dataset, currency=currency, metric=metric)
    return detect_changepoints_in_series(loaded.values, loaded.describe(), loaded.citation(),
                                         min_size=min_size, penalty_scale=penalty_scale)
