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
"""
import contextlib
import io
import warnings
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

MIN_OBSERVATIONS = 24
ADF_ALPHA = 0.05
GRANGER_ALPHA = 0.05


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

    corr_diff = float(np.corrcoef(values[target], values[predictor])[0, 1])
    corr_level = float(np.corrcoef(aligned[target], aligned[predictor])[0, 1])

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
        "correlation_diff": round(corr_diff, 4),
        "correlation_level": round(corr_level, 4),
        "description": description,
        "inputs": [target, predictor],
    }
