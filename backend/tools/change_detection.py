"""Change-point detection: where a series changes level, trend or volatility.

The brief's Change Detection Tool asks for "trend, seviye veya davranış
değişimleri" -- three different questions, answered by one engine (PELT,
Killick et al. 2012, via `ruptures`) run on three different signals:

    kind="level"       the values themselves           -> a jump to a new plateau
    kind="trend"       growth rate per period          -> the series speeds up or slows down
    kind="volatility"  spread of the growth rate       -> calm becomes jumpy (or the reverse)
    kind="auto"        level for rates/ratios/%, trend for everything else

Frequency-agnostic: the spacing of the index is inferred, breaks are reported in
the series' own periods (YYYY-MM for monthly, YYYY-MM-DD for weekly/daily), and
the minimum segment length defaults to about a quarter of a year at that
frequency so a "regime" means the same thing for a weekly and a monthly series.

Every break also carries a confidence grade. The detector is run at all three
sensitivities and a break is "solid" if it appears at every setting (within a
tolerance of about half a minimum segment, because a lower penalty can nudge a
cut by an observation or two), "moderate" at two, "tentative" at one. This is
the tool's own statement of how sure it is, so the composer never has to guess.

Before looking for breaks, the signal is checked with the same outlier rule the
anomaly tool uses (`tools.outliers`, rolling z-score AND IQR fence). A flagged
point is capped at the fence, not removed, so one wild observation cannot
inflate the series' spread and hide a real break; the capped points are listed
in the result. Volatility is never capped -- a spike is what it measures -- and
a series shorter than the outlier window skips the check and says so.

A year-to-date cumulative series is refused: its January reset would be reported
as the largest break every year. `tools.series.load_series` already serves such
series de-cumulated (`cumulative_as="flow"`), so this only bites when raw
published values reach the tool by another route.
"""
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import ruptures as rpt

from .frequency import find_gaps, infer_frequency, spans_in_periods  # noqa: F401  (re-exported)
from .outliers import classify_flags, score_outliers
from .series import load_series

KINDS = ("auto", "trend", "level", "volatility")
LEVEL_SEMANTICS = ("rate", "ratio")

# The planner picks a sensitivity word; it maps to a PELT penalty of
# multiplier * log(n) applied to the standardised signal. Standardising makes
# the penalty independent of the series' scale; the log(n) term (the usual
# BIC-style choice) makes it independent of the series' length, so "medium"
# means the same thing for 66 monthly points and 295 weekly ones. Nothing here
# is tuned to a particular series.
SENSITIVITY_MULTIPLIER = {"low": 3.0, "medium": 2.0, "high": 1.0}
CONFIDENCE = {3: "solid", 2: "moderate", 1: "tentative"}


YTD_RESET_RATIO = 0.3        # a January below this share of the previous December counts as a reset
YTD_RISING_SHARE = 0.9       # within a year, at least this share of month-to-month steps must be non-decreasing


def january_resets(series: pd.Series, reset_ratio: float = YTD_RESET_RATIO) -> Optional[bool]:
    """Does EVERY available January sit below `reset_ratio` x the preceding
    December? None when fewer than two Januaries can be checked."""
    s = series.dropna()
    ratios = []
    for ts, value in s.items():
        if ts.month != 1:
            continue
        prev = s.get(ts - pd.offsets.MonthBegin(1))
        if prev is None or prev == 0:
            continue
        ratios.append(abs(value) / abs(prev))
    if len(ratios) < 2:
        return None
    return all(r < reset_ratio for r in ratios)


def rises_within_years(series: pd.Series, rising_share: float = YTD_RISING_SHARE) -> Optional[bool]:
    """A running total can only go up between two Januaries; a seasonal flow
    goes up and down. True when at least `rising_share` of the month-to-month
    steps that do NOT cross a January are non-decreasing. None if too few."""
    s = series.dropna()
    steps = [(s.iloc[i + 1] - s.iloc[i]) for i in range(len(s) - 1) if s.index[i + 1].month != 1]
    if len(steps) < 10:
        return None
    return float(np.mean([step >= 0 for step in steps])) >= rising_share


def looks_year_to_date(series: pd.Series, reset_ratio: float = YTD_RESET_RATIO) -> bool:
    """Backstop heuristic for UNLABELLED monthly data. True only when BOTH
    fingerprints of a year-to-date series are present: every January resets
    (`january_resets`) AND the series almost never falls between Januaries
    (`rises_within_years`). The second check is what tells a running total
    apart from a strongly seasonal flow with a big December and a small
    January -- that flow resets too, but it goes up and down all year.

    The primary mechanism is the `temporal_semantics` label the lakehouse
    attaches to every series; this test only exists for series that reach the
    tool without one. It assumes a calendar-year reset and monthly spacing, and
    returns False (does not judge) for any other frequency. A cumulative-
    since-inception series never resets and can only be caught by the label.
    """
    s = series.dropna()
    if len(s) < 14 or infer_frequency(s.index)[0] != "month":
        return False
    return bool(january_resets(s, reset_ratio)) and bool(rises_within_years(s))


def growth_rate(series: pd.Series) -> pd.Series:
    """Log growth PER PERIOD from one observation to the next, in percent. Length = n - 1.

    Across a gap (a missing observation) the log difference spans several
    periods; it is divided by the number of periods spanned so the value stays
    "growth per period" rather than doubling after every hole in the data.
    """
    clean = series.dropna()
    if (clean <= 0).any():
        raise ValueError("growth rate needs strictly positive values -- use kind='level' for this series")
    growth = np.diff(np.log(clean.values)) * 100.0 / spans_in_periods(clean.index)
    return pd.Series(growth, index=clean.index[1:], name="growth_pct")


def resolve_kind(kind: str, temporal_semantics: Optional[str], unit: Optional[str]) -> str:
    if kind != "auto":
        return kind
    if temporal_semantics in LEVEL_SEMANTICS or (unit or "").strip() == "%":
        return "level"
    return "trend"


def _signal(series: pd.Series, kind: str) -> pd.Series:
    if kind == "level":
        return series.dropna()
    growth = growth_rate(series)
    if kind == "trend":
        return growth
    return (growth - growth.mean()) ** 2  # volatility: squared deviations


def _segment_stat(chunk: pd.Series, kind: str) -> float:
    if kind == "volatility":
        return float(np.sqrt(chunk.mean()))  # back to a typical swing, in %
    return float(chunk.mean())


MAX_CAP_PASSES = 3
SEASONAL_AUTOCORR = 0.5      # year-over-year correlation at or above this earns a warning
GRADUAL_MIN_R2 = 0.2         # a straight line must explain at least this much to be called a drift


def _shape(z: np.ndarray, ends: List[int], penalty: float) -> Dict:
    """Is the standardised signal better described as flat steps or one straight line?

    Both candidates are charged with the same penalty per parameter PELT used:
    the staircase pays for each cut, the line pays once for its slope. Lower
    total wins. Returns {"shape", "line": {slope_per_period_sd, r2}, "staircase_cost", "line_cost"}.
    """
    n = len(z)
    bounds = [0] + list(ends)
    stair_ss = sum(float(((z[a:b] - z[a:b].mean()) ** 2).sum()) for a, b in zip(bounds[:-1], bounds[1:]))
    staircase_cost = stair_ss + penalty * (len(ends) - 1)
    t = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(t, z, 1)
    line_ss = float(((z - (slope * t + intercept)) ** 2).sum())
    total_ss = float(((z - z.mean()) ** 2).sum()) or 1.0
    r2 = 1.0 - line_ss / total_ss
    line_cost = line_ss + penalty * 1
    if len(ends) > 1 and line_cost <= staircase_cost and r2 >= GRADUAL_MIN_R2:
        shape = "gradual"
    elif len(ends) > 1:
        shape = "stepwise"
    elif r2 >= GRADUAL_MIN_R2:
        shape = "gradual"
    else:
        shape = "flat"
    # Residuals around the description that won -- what is left after the
    # steps (or the line) are removed. Seasonality is measured on these, so a
    # level shift or a drift is not mistaken for a yearly pattern.
    if shape == "gradual":
        residual = z - (slope * t + intercept)
    else:
        residual = z.copy()
        for a, b in zip(bounds[:-1], bounds[1:]):
            residual[a:b] = z[a:b] - z[a:b].mean()
    return {"shape": shape, "line": {"slope_per_period_sd": round(float(slope), 4), "r2": round(r2, 3)},
            "staircase_cost": round(staircase_cost, 2), "line_cost": round(line_cost, 2), "residual": residual}


def _seasonality(residual: np.ndarray, per_year: int) -> Optional[float]:
    """Correlation of the de-stepped / de-trended residual with itself one year
    earlier; None if the series is too short to hold two full years."""
    if per_year < 2 or len(residual) < 2 * per_year + 2:
        return None
    value = pd.Series(residual).autocorr(lag=per_year)
    return None if value is None or np.isnan(value) else round(float(value), 3)


def _cap_outliers(signal: pd.Series, window: int, lookahead: int, fmt: str):
    """Apply the shared outlier rule to `signal`; return (capped signal, report).

    Flags are classified with the shared `classify_flags`: a `spike` is capped
    at the fence; a `regime_start` is the break we are looking for and is left
    untouched; an `undetermined` flag (nothing after it) is capped too, since it
    cannot start a regime anyway.

    Capping is repeated (up to MAX_CAP_PASSES) because a spike inside another
    point's trailing window inflates that window's spread and can mask a second
    spike right after it; once the first is capped, the second becomes visible.
    """
    if len(signal) <= window:
        return signal, {"applied": False, "reason": f"series has {len(signal)} points, window is {window}",
                        "window": window, "capped": [], "kept_as_regime_start": []}
    capped = signal.copy()
    report, kept, seen = [], [], set()
    passes = 0
    for passes in range(1, MAX_CAP_PASSES + 1):
        scores = score_outliers(capped, on="level", window=window)
        kinds = classify_flags(scores, lookahead)
        new = {p: k for p, k in kinds.items() if p not in seen}
        if not new:
            break
        for period, kind in new.items():
            seen.add(period)
            original = float(signal.loc[period])
            if kind == "regime_start":
                kept.append(period.strftime(fmt))
                continue
            above = capped.loc[period] > scores.roll_mean.loc[period]
            fence = float(scores.upper_fence.loc[period] if above else scores.lower_fence.loc[period])
            capped.loc[period] = fence
            report.append({"period": period.strftime(fmt), "kind": kind, "original": round(original, 4),
                           "capped_to": round(fence, 4), "z_score": round(float(scores.z_score.loc[period]), 3),
                           "pass": passes})
    report.sort(key=lambda r: r["period"])
    kept.sort()
    return capped, {"applied": True, "window": window, "lookahead": lookahead, "passes": passes,
                    "rule": "rolling z>=3 AND IQR fence, classified spike/regime_start (shared with tools.anomaly); "
                            "spikes capped at the fence, regime starts kept; repeated until no new flags",
                    "capped": report, "kept_as_regime_start": kept}


def detect_change_points(series: pd.Series, kind: str = "auto", sensitivity: str = "medium",
                         min_segment: Optional[int] = None, match_tolerance: Optional[int] = None,
                         cap_outliers: bool = True, outlier_window: Optional[int] = None,
                         temporal_semantics: Optional[str] = None, unit: Optional[str] = None,
                         name: Optional[str] = None) -> Dict:
    """Locate periods where `series` changes level, trend or volatility.

    `temporal_semantics` and `unit` (from `SeriesResult` or a column's lineage)
    drive kind="auto" and the year-to-date guard; both are optional. `name` is
    what the Turkish `description` calls the series (defaults to `series.name`).

    Returns a JSON-serialisable dict:
        kind, frequency, measure          what the numbers below are
        n_breakpoints, breakpoints        count and periods, for a one-line summary
        breaks:   [{period, before, after, shift, shift_unit, direction, shift_in_sd,
                    support, confidence, recent}, ...]
                  `shift` is in the series' own terms: points for a rate or ratio
                  level, percent for any other level, growth points for trend and
                  volatility -- "a break at 2023-07" is not a finding until it says
                  which way and by how much
        description: one Turkish sentence naming the count and the largest break,
                  for the composer and the deterministic summary
        segments: [{start, end, periods, value}, ...]   (at the requested sensitivity)
        confidence_summary:    {"solid": k, "moderate": k, "tentative": k}
        breaks_by_sensitivity: {"low": [...], "medium": [...], "high": [...]}
        outliers:              {applied, window, lookahead, rule, capped: [...], kept_as_regime_start: [...]}
        shape:                 "stepwise" | "gradual" | "flat"  (staircase vs one straight line, same penalty)
        trend_line:            {slope_per_period_sd, r2}
        seasonal_autocorr:     year-over-year correlation of the signal, or null when too short
        gaps:                  [{after, before, missing_periods}, ...]
        warnings:              Turkish caveats for the composer to repeat to the user (the composer writes Turkish)
        method, sensitivity, min_segment, match_tolerance, n_obs
    `match_tolerance` (observations) decides when breaks at two sensitivities
    count as the same break; defaults to half a minimum segment.
    `cap_outliers` runs the shared outlier rule first (never for volatility);
    `outlier_window` defaults to a year of observations, at least 12.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if sensitivity not in SENSITIVITY_MULTIPLIER:
        raise ValueError(f"sensitivity must be one of {sorted(SENSITIVITY_MULTIPLIER)}")
    clean = series.dropna()
    if len(clean) < 3:
        raise ValueError(f"series has {len(clean)} points; need at least 3 for change detection")
    if (temporal_semantics == "cumulative_ytd" or temporal_semantics is None) and looks_year_to_date(clean):
        raise ValueError(f"{series.name!r} looks year-to-date cumulative (resets every January); "
                         "load it with cumulative_as='flow' or de-cumulate before detecting changes")

    kind = resolve_kind(kind, temporal_semantics, unit)
    freq_name, per_year, fmt = infer_frequency(clean.index)
    gaps = find_gaps(clean.index, fmt)
    if min_segment is None:
        min_segment = max(4, round(per_year / 4))  # about a quarter of a year

    signal = _signal(clean, kind)
    if len(signal) < 2 * min_segment:
        raise ValueError(f"need at least {2 * min_segment} observations of {freq_name}ly data, got {len(signal)}")

    if outlier_window is None:
        outlier_window = max(12, per_year)
    if cap_outliers and kind != "volatility":
        signal, outliers = _cap_outliers(signal, outlier_window, min_segment, fmt)
    else:
        outliers = {"applied": False, "reason": "volatility measures the spikes themselves" if kind == "volatility"
                    else "disabled by caller", "window": outlier_window, "capped": [], "kept_as_regime_start": []}

    spread = signal.std(ddof=0)
    z = ((signal - signal.mean()) / spread).values if spread > 0 else np.zeros(len(signal))
    log_n = float(np.log(len(signal)))
    penalty = SENSITIVITY_MULTIPLIER[sensitivity] * log_n
    algo = rpt.Pelt(model="l2", min_size=min_segment, jump=1).fit(z)
    ends_at = {word: algo.predict(pen=mult * log_n) for word, mult in SENSITIVITY_MULTIPLIER.items()}
    ends = ends_at[sensitivity]
    shape = _shape(z, ends, penalty)
    seasonal = _seasonality(shape["residual"], per_year)
    warnings: List[str] = []
    if gaps:
        warnings.append(f"Veride {len(gaps)} boşluk var; boşluk üzerindeki büyüme dönem başına hesaplandı.")
    if (temporal_semantics is None and freq_name == "month" and january_resets(clean)
            and not rises_within_years(clean)):
        warnings.append("Her Ocak bir önceki Aralık'ın çok altında, ancak seri yıl içinde de düşüyor: büyük "
                        "olasılıkla birikimli değil, güçlü mevsimsellik taşıyan bir aylık akış. Kaynağı doğrulayın.")
    if shape["shape"] == "gradual" and len(ends) > 1:
        warnings.append("Düz bir çizgi bu seriyi basamaklar kadar iyi açıklıyor; kırılmalar belirgin olaylar "
                        "değil, kademeli bir sürüklenmeyi tarif ediyor.")
    if seasonal is not None and seasonal >= SEASONAL_AUTOCORR:
        warnings.append(f"Güçlü yıllık örüntü (bir yıl önceki değerle korelasyon {seasonal}); kırılmalar rejim "
                        "değişimi yerine mevsimselliği yansıtıyor olabilir.")

    # Cut positions (segment ends minus the final one) at every sensitivity,
    # used to grade each requested break by how many settings agree with it.
    cuts_at = {word: e[:-1] for word, e in ends_at.items()}
    if match_tolerance is None:
        match_tolerance = max(1, round(min_segment / 2))

    bounds = [0] + ends
    segments: List[Dict] = []
    for start, end in zip(bounds[:-1], bounds[1:]):
        chunk = signal.iloc[start:end]
        segments.append({
            "start": chunk.index[0].strftime(fmt),
            "end": chunk.index[-1].strftime(fmt),
            "periods": int(len(chunk)),
            "value": round(_segment_stat(chunk, kind), 3),
        })
    # The size of a shift in the series' own terms. A level break in a rate or
    # ratio is a point difference; in a balance it is a percent change; a trend
    # or volatility break is a difference of growth percentages, i.e. points.
    in_points = kind != "level" or temporal_semantics in LEVEL_SEMANTICS or (unit or "").strip() == "%"
    shift_unit = "puan" if in_points else "%"

    def _shift(before: float, after: float) -> float:
        if in_points:
            return after - before
        return 100.0 * (after / before - 1.0) if before else float("nan")

    breaks = []
    for i, cut in enumerate(cuts_at[sensitivity]):
        support = sum(any(abs(cut - other) <= match_tolerance for other in cuts) for cuts in cuts_at.values())
        before, after = segments[i]["value"], segments[i + 1]["value"]
        breaks.append({
            "period": segments[i + 1]["start"],
            "before": before,
            "after": after,
            "shift": round(float(_shift(before, after)), 4),
            "shift_unit": shift_unit,
            "direction": "up" if after > before else "down",
            "shift_in_sd": round(abs(after - before) / float(spread), 2) if spread > 0 else 0.0,
            "support": int(support),
            "confidence": CONFIDENCE[int(support)],
            # Only the LAST break can be too recent to trust: its regime runs to
            # the end of the data and is still short (< 2 min_segments), so we
            # cannot yet know whether it persists. Flagged, not hidden.
            "recent": bool(i == len(segments) - 2 and segments[-1]["periods"] < 2 * min_segment),
        })
    per = {"day": "günlük", "week": "haftalık", "month": "aylık", "quarter": "çeyreklik", "year": "yıllık"}[freq_name]
    measure = {
        "level": f"ortalama değer{f' ({unit})' if unit else ''}",
        "trend": f"ortalama büyüme, {per} %",
        "volatility": f"büyümedeki tipik dalgalanma, {per} ±%",
    }[kind]

    # One sentence for the composer: the count, then the largest break with its
    # direction and size, so a staircase is not narrated as three regime changes.
    label = name or (str(series.name) if series.name is not None else "seri")
    where = {"level": "seviyesinde", "trend": "buyume hizinda", "volatility": "oynakliginda"}[kind]
    span = f"{clean.index.min().strftime(fmt)}..{clean.index.max().strftime(fmt)}"
    grade = {"solid": "yuksek", "moderate": "orta", "tentative": "dusuk"}
    if breaks:
        largest = max(breaks, key=lambda b: abs(b["shift"]) if not np.isnan(b["shift"]) else -1)
        listed = ", ".join(b["period"] for b in breaks)
        description = (f"{label} {where} {len(breaks)} kirilma ({span}): {listed}. En buyugu "
                       f"{largest['period']}: {largest['before']:g} -> {largest['after']:g} "
                       f"({largest['shift']:+g} {shift_unit}, {'yukari' if largest['direction'] == 'up' else 'asagi'}, "
                       f"guven {grade[largest['confidence']]}{', cok yeni' if largest['recent'] else ''})")
    else:
        description = f"{label} {where} kirilma bulunmadi ({span}): tek rejim"

    return {
        "series": str(series.name),
        "kind": kind,
        "frequency": freq_name,
        "measure": measure,
        "unit": unit,
        "shift_unit": shift_unit,
        "period_start": clean.index.min().strftime(fmt),
        "period_end": clean.index.max().strftime(fmt),
        "n_obs": int(len(clean)),
        "n_points": int(len(clean)),
        "method": f"PELT (l2, pen={penalty:.1f} = {SENSITIVITY_MULTIPLIER[sensitivity]:g}*log(n)) "
                  f"on the standardised {kind} signal; confidence from agreement across all three sensitivities",
        "sensitivity": sensitivity,
        "min_segment": int(min_segment),
        "n_breakpoints": len(breaks),
        "breakpoints": [b["period"] for b in breaks],
        "breaks": breaks,
        "segments": segments,
        "confidence_summary": {word: sum(b["confidence"] == word for b in breaks) for word in CONFIDENCE.values()},
        "breaks_by_sensitivity": {
            word: [signal.index[c].strftime(fmt) for c in cuts] for word, cuts in cuts_at.items()
        },
        "match_tolerance": int(match_tolerance),
        "outliers": outliers,
        "shape": shape["shape"],
        "trend_line": shape["line"],
        "seasonal_autocorr": seasonal,
        "gaps": gaps,
        "warnings": warnings,
        "description": description,
    }


def detect_change_points_for(key: str, source: str = "bulletin", dataset: Optional[str] = None,
                             currency: Optional[str] = "total", metric: Optional[str] = None,
                             start: Optional[str] = None, end: Optional[str] = None,
                             kind: str = "auto", sensitivity: str = "medium",
                             min_segment: Optional[int] = None, match_tolerance: Optional[int] = None,
                             cap_outliers: bool = True, outlier_window: Optional[int] = None) -> Dict:
    """Load a lakehouse series and detect its change points; result carries the
    series description and citation, like `tools.anomaly.detect_anomalies`."""
    loaded = load_series(key, source=source, dataset=dataset, currency=currency, metric=metric,
                         start=start, end=end)
    result = detect_change_points(loaded.values, kind=kind, sensitivity=sensitivity, min_segment=min_segment,
                                  match_tolerance=match_tolerance, cap_outliers=cap_outliers,
                                  outlier_window=outlier_window,
                                  temporal_semantics=loaded.temporal_semantics, unit=loaded.unit,
                                  name=loaded.name)
    return {**result, **loaded.describe(), "citation": loaded.citation()}
