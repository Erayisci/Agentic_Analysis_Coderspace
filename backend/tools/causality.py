"""Granger causality between two aligned monthly series, in both directions.

What the test answers: does the past of X help predict Y beyond Y's own
past? That is predictability, not causation, and the result says so in its
own `description` -- the composer is allowed to repeat that sentence and not
to strengthen it.

Choices, each measured on the reference pair (housing-loan stock vs
housing-loan rate, 2021-01..2025-12) before being fixed here:

- **Both series are differenced when either is non-stationary**, and the
  ADF test is repeated after differencing. Both are I(1) there; without
  differencing the F-tests are on two trends.
- **One lag, chosen by BIC on a VAR**, rather than the smallest p-value over
  six lags. Six tries against a 0.05 threshold is a 0.26 threshold in
  disguise; on that pair the BIC lag is 1.
- **The correlation is on the differenced series** (-0.30 there), and the
  level correlation (+0.79, the shared trend) is reported beside it with a
  label -- it is the only signed number a reader would otherwise have, and
  its sign is wrong.
- **The sign of the effect** is the sum of the predictor's lag coefficients
  in the target's VAR equation. Granger gives none; without it "faiz konutu
  öncülüyor" cannot say in which direction.
- **Cointegration** is tested on the levels when both are I(1), because
  differencing two cointegrated series discards their long-run relation, and
  the result should say when that caveat applies.

Runs on the table's aligned window, not the full history: the relationship
the question asks about is the one over the period it named. (The anomaly
and changepoint tools fetch full history because they need a baseline; this
one needs the pair as the user sees it.)

Two entry points, one computation. `granger_both_directions` takes the
artifact's frame and two column names, which is how the executor holds the
pair; `analyze_causality` takes two Series with a candidate-cause /
candidate-effect reading, validates them (duplicate periods, a constant
series) and calls the same function. Both return the same dict, which also
carries the lead-lag correlation profile and a `limitations` list so the
result states its own caveats rather than leaving them to the composer.
"""
import contextlib
import io
import warnings
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

MIN_OBSERVATIONS = 24
ADF_ALPHA = 0.05
GRANGER_ALPHA = 0.05

LIMITATIONS = [
    "Granger causality measures predictive precedence, not structural causation.",
    "Omitted variables may explain an observed relationship.",
    "Results depend on the available sample and the selected lag.",
    "Lead-lag correlation is descriptive and is not itself a causality test.",
]


def _lead_lag_correlations(cause: pd.Series, effect: pd.Series, max_lag: int) -> Dict[str, Any]:
    """Correlation of effect(t) with cause(t - lag) for lag in -max_lag..max_lag.

    A positive lag means the candidate cause leads the effect. Computed on the
    same (differenced or level) values the Granger test used, so the profile
    and the test describe one pair of series.
    """
    correlations: List[Dict[str, Any]] = []
    for lag in range(-max_lag, max_lag + 1):
        aligned = pd.concat([cause.shift(lag).rename("cause"), effect.rename("effect")], axis=1).dropna()
        value = aligned["cause"].corr(aligned["effect"]) if len(aligned) >= 3 else None
        correlations.append({"lag": lag, "correlation": None if value is None or pd.isna(value)
                             else round(float(value), 4)})
    valid = [c for c in correlations if c["correlation"] is not None]
    strongest = max(valid, key=lambda c: abs(c["correlation"])) if valid else None
    return {"sign_convention": "positive lag means cause leads effect",
            "correlations": correlations, "strongest": strongest}


def _adf_p(values: np.ndarray) -> float:
    from statsmodels.tsa.stattools import adfuller
    with warnings.catch_warnings():
        # statsmodels 0.15 warns that the tuple return will become an object;
        # index [1] is the p-value in both, and pinning `result_object` would
        # break the 0.14 floor in pyproject.
        warnings.simplefilter("ignore", FutureWarning)
        return float(adfuller(values, autolag="AIC")[1])


def _granger_p(frame: pd.DataFrame, target: str, predictor: str, lag: int) -> Dict[int, float]:
    """p-values of the SSR F-test for predictor -> target at lags 1..lag."""
    from statsmodels.tsa.stattools import grangercausalitytests
    with contextlib.redirect_stdout(io.StringIO()):
        raw = grangercausalitytests(frame[[target, predictor]].to_numpy(dtype=float), maxlag=lag)
    # int(): statsmodels keys lags by numpy.int64, which json.dumps refuses.
    return {int(k): round(float(v[0]["ssr_ftest"][1]), 5) for k, v in raw.items()}


def granger_both_directions(frame: pd.DataFrame, target: str, predictor: str,
                            max_lag: int = 6, describe: Optional[Dict[str, Any]] = None) -> dict:
    """Granger tests target<-predictor and predictor<-target on one aligned frame.

    `describe` maps column -> {name, unit, temporal_semantics}; used only for
    the Turkish description.
    """
    from statsmodels.tsa.api import VAR
    from statsmodels.tsa.stattools import coint

    aligned = frame[[target, predictor]].dropna()
    n = int(len(aligned))
    if n < MIN_OBSERVATIONS:
        raise ValueError(f"need at least {MIN_OBSERVATIONS} aligned observations, have {n}")

    adf_levels = {c: _adf_p(aligned[c].to_numpy(dtype=float)) for c in (target, predictor)}
    nonstationary = [c for c, p in adf_levels.items() if p > ADF_ALPHA]
    differenced = bool(nonstationary)
    values = aligned.diff().dropna() if differenced else aligned
    adf_after = ({c: _adf_p(values[c].to_numpy(dtype=float)) for c in (target, predictor)}
                 if differenced else adf_levels)

    cointegration_p = None
    if len(nonstationary) == 2:
        cointegration_p = round(float(coint(aligned[target], aligned[predictor])[1]), 4)

    # One lag by BIC; the ceiling keeps the VAR estimable on ~60 points.
    max_lag = max(1, min(max_lag, len(values) // 8))
    with contextlib.redirect_stdout(io.StringIO()):
        order = VAR(values.to_numpy(dtype=float)).select_order(maxlags=max_lag)
    lag = int(order.bic) if order.bic and int(order.bic) > 0 else 1
    fitted = VAR(values[[target, predictor]].to_numpy(dtype=float)).fit(lag)
    coefs = fitted.coefs  # shape (lag, 2, 2): [lag][equation][variable]

    directions: Dict[str, Dict[str, Any]] = {}
    for eq_index, (y, x) in enumerate(((target, predictor), (predictor, target))):
        p_by_lag = _granger_p(values, y, x, lag)
        p_value = p_by_lag[lag]
        x_index = 1 - eq_index
        effect = float(sum(coefs[k][eq_index][x_index] for k in range(lag)))
        directions[f"{x}->{y}"] = {
            "predictor": x, "target": y, "p_value": p_value, "p_values_by_lag": p_by_lag,
            "predictive": p_value < GRANGER_ALPHA,
            "effect_sign": "positive" if effect > 0 else "negative",
            "effect_coefficient_sum": round(effect, 6),
        }

    forward = directions[f"{predictor}->{target}"]["predictive"]
    backward = directions[f"{target}->{predictor}"]["predictive"]
    verdict = {(True, True): "both", (True, False): "predictor->target",
               (False, True): "target->predictor", (False, False): "none"}[(forward, backward)]
    classification = {"both": "bidirectional_predictive_evidence",
                      "predictor->target": "directional_predictive_evidence",
                      "target->predictor": "reverse_predictive_evidence",
                      "none": "no_predictive_evidence"}[verdict]

    corr_diff = float(np.corrcoef(values[target], values[predictor])[0, 1])
    corr_level = float(np.corrcoef(aligned[target], aligned[predictor])[0, 1])
    lead_lag = _lead_lag_correlations(values[predictor], values[target], max_lag)

    names = {c: (describe or {}).get(c, {}).get("name") or c for c in (target, predictor)}
    span = f"{aligned.index.min():%Y-%m}..{aligned.index.max():%Y-%m}"

    def sentence(x: str, y: str) -> str:
        d = directions[f"{x}->{y}"]
        verb = "ongormeye yardim eder" if d["predictive"] else "ongormeye yardim etmez"
        sign = "ayni yonde" if d["effect_sign"] == "positive" else "ters yonde"
        return f"{names[x]} gecmisi {names[y]}'yi {verb} (lag {lag}, p={d['p_value']:.3f}, etki {sign})"

    description = (f"Granger testi ({span}, {n} ay, {'birinci farklar' if differenced else 'seviyeler'}): "
                   f"{sentence(predictor, target)}; {sentence(target, predictor)}. "
                   f"Aylik degisimlerin korelasyonu {corr_diff:+.2f}"
                   + (f" (seviyelerde {corr_level:+.2f}, ortak trend)" if differenced else "")
                   + (f"; esbutunlesme p={cointegration_p:.2f}" if cointegration_p is not None else "")
                   + ". Granger = ongorulebilirlik, nedensellik kaniti degildir.")

    return {
        "target": target, "predictor": predictor,
        "cause": predictor, "effect": target,
        "n_observations": n,
        "period_start": aligned.index.min().strftime("%Y-%m"),
        "period_end": aligned.index.max().strftime("%Y-%m"),
        "adf_p_levels": {c: round(p, 4) for c, p in adf_levels.items()},
        "differenced": differenced,
        "stationary_after_differencing": all(p <= ADF_ALPHA for p in adf_after.values()),
        "cointegration_p": cointegration_p,
        "lag": lag, "lag_rule": "BIC", "max_lag": max_lag,
        "directions": directions,
        "verdict": verdict,
        "classification": classification,
        "correlation_diff": round(corr_diff, 4),
        "correlation_level": round(corr_level, 4),
        "lead_lag": lead_lag,
        "description": description,
        "limitations": list(LIMITATIONS),
        "inputs": [target, predictor],
    }


def _validate_series(series: pd.Series, name: str) -> pd.Series:
    if not isinstance(series, pd.Series):
        raise TypeError(f"{name!r} must be a pandas Series")
    if series.index.duplicated().any():
        raise ValueError(f"{name!r} contains duplicate periods")
    clean = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).sort_index().dropna()
    if clean.empty:
        raise ValueError(f"{name!r} has no numeric observations")
    if clean.nunique() < 2:
        raise ValueError(f"{name!r} is constant; causality cannot be tested")
    return clean.astype(float)


def analyze_causality(cause: pd.Series, effect: pd.Series, *, cause_name: str = "cause",
                      effect_name: str = "effect", max_lag: int = 6,
                      describe: Optional[Dict[str, Any]] = None) -> dict:
    """Granger both ways between a candidate cause and a candidate effect.

    The same computation as `granger_both_directions`, for callers holding two
    Series rather than a frame: `cause` is the predictor, `effect` the target,
    and only the periods both publish are used. Raises ValueError for duplicate
    periods, a constant series or fewer than `MIN_OBSERVATIONS` aligned points.
    """
    cause = _validate_series(cause, cause_name)
    effect = _validate_series(effect, effect_name)
    frame = pd.concat([cause.rename(cause_name), effect.rename(effect_name)], axis=1, join="inner").dropna()
    return granger_both_directions(frame, target=effect_name, predictor=cause_name,
                                   max_lag=max_lag, describe=describe)
