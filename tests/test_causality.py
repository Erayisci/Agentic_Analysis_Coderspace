"""The causality tool through its two-Series entry point, `analyze_causality`.

`granger_both_directions` (the executor's frame-based entry point) is pinned
in tests/test_tools.py on the sign of the effect, the differenced correlation
and the description; these tests cover the same computation from the
cause/effect side: input validation, direction naming, the classification and
the lead-lag profile, on synthetic pairs whose answer is known by construction.
"""
import json

import numpy as np
import pandas as pd
import pytest

from backend.tools.causality import LIMITATIONS, MIN_OBSERVATIONS, analyze_causality


def _monthly_index(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2015-01-01", periods=n, freq="MS")


def _pair(seed: int, n: int = 120, lag: int = 2, beta: float = 0.9, noise: float = 0.4):
    """y[t] = beta * x[t-lag] + noise: x leads y, y does not lead x."""
    rng = np.random.default_rng(seed)
    x = rng.normal(0, 1, n)
    y = rng.normal(0, noise, n)
    for t in range(lag, n):
        y[t] += beta * x[t - lag]
    index = _monthly_index(n)
    return pd.Series(x, index=index), pd.Series(y, index=index)


def test_detects_forward_predictive_relationship():
    x, y = _pair(42)
    result = analyze_causality(cause=x, effect=y, cause_name="x", effect_name="y", max_lag=6)
    assert result["cause"] == "x" and result["effect"] == "y"
    assert result["directions"]["x->y"]["predictive"]
    assert result["directions"]["x->y"]["p_value"] < 0.05
    assert result["classification"] in {"directional_predictive_evidence", "bidirectional_predictive_evidence"}
    assert result["verdict"] in {"predictor->target", "both"}


def test_nonstationary_series_are_differenced_together():
    """Random walks are I(1): both are differenced, and the test says so."""
    rng = np.random.default_rng(123)
    n = 100
    x = pd.Series(np.cumsum(rng.normal(0, 1, n)), index=_monthly_index(n))
    y = pd.Series(np.cumsum(rng.normal(0, 1, n)), index=_monthly_index(n))
    result = analyze_causality(cause=x, effect=y, cause_name="x", effect_name="y")
    assert result["differenced"] is True
    assert result["stationary_after_differencing"] is True
    assert all(p > 0.05 for p in result["adf_p_levels"].values())
    assert result["cointegration_p"] is not None          # both I(1) -> tested on the levels


def test_stationary_series_are_not_unnecessarily_differenced():
    rng = np.random.default_rng(99)
    n = 120
    x = pd.Series(rng.normal(size=n), index=_monthly_index(n))
    y = pd.Series(rng.normal(size=n), index=_monthly_index(n))
    result = analyze_causality(cause=x, effect=y, cause_name="x", effect_name="y")
    assert result["differenced"] is False
    assert result["cointegration_p"] is None
    assert result["correlation_diff"] == result["correlation_level"]


def test_constant_series_is_rejected():
    n = 60
    x = pd.Series(np.ones(n), index=_monthly_index(n))
    y = pd.Series(np.arange(n, dtype=float), index=_monthly_index(n))
    with pytest.raises(ValueError, match="constant"):
        analyze_causality(cause=x, effect=y, cause_name="x", effect_name="y")


def test_too_few_aligned_observations_are_rejected():
    rng = np.random.default_rng(5)
    n = MIN_OBSERVATIONS - 4
    with pytest.raises(ValueError, match=f"at least {MIN_OBSERVATIONS}"):
        analyze_causality(cause=pd.Series(rng.normal(size=n), index=_monthly_index(n)),
                          effect=pd.Series(rng.normal(size=n), index=_monthly_index(n)))


def test_only_the_shared_periods_are_used():
    x, y = _pair(7, n=120)
    result = analyze_causality(cause=x.iloc[:100], effect=y.iloc[20:], cause_name="x", effect_name="y")
    assert result["n_observations"] == 80
    assert result["period_start"] == "2016-09" and result["period_end"] == "2023-04"


def test_duplicate_periods_are_rejected():
    index = pd.DatetimeIndex(["2021-01-01", "2021-01-01", *pd.date_range("2021-02-01", periods=30, freq="MS")])
    x = pd.Series(np.arange(len(index), dtype=float), index=index)
    with pytest.raises(ValueError, match="duplicate periods"):
        analyze_causality(cause=x, effect=x.copy(), cause_name="x", effect_name="y")


def test_positive_lag_means_cause_leads_effect():
    """y[t] depends on x[t-2], so the strongest lead-lag correlation is at a positive lag."""
    x, y = _pair(88, noise=0.1, beta=1.0)
    result = analyze_causality(cause=x, effect=y, cause_name="x", effect_name="y", max_lag=4)
    strongest = result["lead_lag"]["strongest"]
    assert strongest is not None and strongest["lag"] == 2
    assert result["lead_lag"]["sign_convention"] == "positive lag means cause leads effect"
    assert len(result["lead_lag"]["correlations"]) == 2 * result["max_lag"] + 1


def test_result_names_both_directions_cause_first():
    rng = np.random.default_rng(17)
    n = 100
    result = analyze_causality(cause=pd.Series(rng.normal(size=n), index=_monthly_index(n)),
                               effect=pd.Series(rng.normal(size=n), index=_monthly_index(n)),
                               cause_name="rate", effect_name="loan")
    assert set(result["directions"]) == {"rate->loan", "loan->rate"}
    assert result["directions"]["rate->loan"]["predictor"] == "rate"
    assert result["inputs"] == ["loan", "rate"]           # target first, as the executor keys it


def test_result_does_not_claim_real_world_causation():
    rng = np.random.default_rng(23)
    n = 100
    result = analyze_causality(cause=pd.Series(rng.normal(size=n), index=_monthly_index(n)),
                               effect=pd.Series(rng.normal(size=n), index=_monthly_index(n)))
    assert result["description"].endswith("Granger = ongorulebilirlik, nedensellik kaniti degildir.")
    assert result["limitations"] == LIMITATIONS
    assert any("not structural causation" in limitation for limitation in result["limitations"])


def test_result_is_json_serialisable():
    x, y = _pair(55)
    json.dumps(analyze_causality(cause=x, effect=y, cause_name="x", effect_name="y"))
