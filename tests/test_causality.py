"""Tests for the deterministic causality tool."""

import json

import numpy as np
import pandas as pd
import pytest

from backend.tools.causality import analyze_causality


def _monthly_index(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2015-01-01", periods=n, freq="MS")


def test_detects_forward_predictive_relationship():
    """x[t-2] directly influences y[t], so x should help predict y."""
    rng = np.random.default_rng(42)
    n = 120

    x = rng.normal(0, 1, n)
    y = rng.normal(0, 0.4, n)

    for t in range(2, n):
        y[t] += 0.9 * x[t - 2]

    index = _monthly_index(n)

    result = analyze_causality(
        cause=pd.Series(x, index=index),
        effect=pd.Series(y, index=index),
        cause_name="x",
        effect_name="y",
        max_lag=6,
    )

    assert result["forward"]["significant"]
    assert result["forward"]["p_value"] < 0.05

    assert result["classification"] in {
        "directional_predictive_evidence",
        "bidirectional_predictive_evidence",
    }


def test_nonstationary_series_are_differenced():
    """Random walks should normally require differencing before VAR/Granger."""
    rng = np.random.default_rng(123)
    n = 100

    x = np.cumsum(rng.normal(0, 1, n))
    y = np.cumsum(rng.normal(0, 1, n))

    result = analyze_causality(
        cause=pd.Series(x, index=_monthly_index(n)),
        effect=pd.Series(y, index=_monthly_index(n)),
        cause_name="x",
        effect_name="y",
    )

    assert result["stationarity"]["x"]["difference_order"] >= 1
    assert result["stationarity"]["y"]["difference_order"] >= 1

    assert result["stationarity"]["x"]["stationary"]
    assert result["stationarity"]["y"]["stationary"]

    mapping = {
        0: "level",
        1: "first_difference",
        2: "second_difference",
    }

    for side in ("cause", "effect"):
        order = result["effective_test"][side]["difference_order"]

        assert result["effective_test"][side]["transformation"] == mapping[order]


def test_stationary_series_are_not_unnecessarily_differenced():
    """White noise is stationary, so it should normally stay in levels."""
    rng = np.random.default_rng(99)
    n = 120

    x = rng.normal(size=n)
    y = rng.normal(size=n)

    result = analyze_causality(
        cause=pd.Series(x, index=_monthly_index(n)),
        effect=pd.Series(y, index=_monthly_index(n)),
        cause_name="x",
        effect_name="y",
    )

    assert result["stationarity"]["x"]["difference_order"] == 0
    assert result["stationarity"]["y"]["difference_order"] == 0

    assert result["effective_test"]["cause"]["transformation"] == "level"
    assert result["effective_test"]["cause"]["difference_order"] == 0

    assert result["effective_test"]["effect"]["transformation"] == "level"
    assert result["effective_test"]["effect"]["difference_order"] == 0


def test_constant_series_is_rejected():
    n = 60
    index = _monthly_index(n)

    x = pd.Series(np.ones(n), index=index)
    y = pd.Series(np.arange(n, dtype=float), index=index)

    with pytest.raises(ValueError, match="constant"):
        analyze_causality(
            cause=x,
            effect=y,
            cause_name="x",
            effect_name="y",
        )


def test_too_few_aligned_observations_are_rejected():
    rng = np.random.default_rng(5)
    n = 20

    with pytest.raises(ValueError, match="at least 24"):
        analyze_causality(
            cause=pd.Series(
                rng.normal(size=n),
                index=_monthly_index(n),
            ),
            effect=pd.Series(
                rng.normal(size=n),
                index=_monthly_index(n),
            ),
        )


def test_duplicate_periods_are_rejected():
    index = pd.DatetimeIndex([
        "2021-01-01",
        "2021-01-01",
        *pd.date_range(
            "2021-02-01",
            periods=30,
            freq="MS",
        ),
    ])

    x = pd.Series(
        np.arange(len(index), dtype=float),
        index=index,
    )

    y = pd.Series(
        np.arange(len(index), dtype=float),
        index=index,
    )

    with pytest.raises(ValueError, match="duplicate periods"):
        analyze_causality(
            cause=x,
            effect=y,
            cause_name="x",
            effect_name="y",
        )


def test_positive_lag_means_cause_leads_effect():
    """y[t] depends on x[t-2], so the strongest relation should be positive lag."""
    rng = np.random.default_rng(88)
    n = 120

    x = rng.normal(size=n)
    y = rng.normal(scale=0.1, size=n)

    for t in range(2, n):
        y[t] += x[t - 2]

    result = analyze_causality(
        cause=pd.Series(x, index=_monthly_index(n)),
        effect=pd.Series(y, index=_monthly_index(n)),
        cause_name="x",
        effect_name="y",
        max_lag=4,
    )

    strongest = result["lead_lag"]["strongest"]

    assert strongest is not None
    assert strongest["lag"] > 0


def test_result_contains_both_directions():
    rng = np.random.default_rng(17)
    n = 100

    result = analyze_causality(
        cause=pd.Series(
            rng.normal(size=n),
            index=_monthly_index(n),
        ),
        effect=pd.Series(
            rng.normal(size=n),
            index=_monthly_index(n),
        ),
        cause_name="rate",
        effect_name="loan",
    )

    assert result["forward"]["direction"] == "rate -> loan"
    assert result["reverse"]["direction"] == "loan -> rate"


def test_result_does_not_claim_real_world_causation():
    rng = np.random.default_rng(23)
    n = 100

    result = analyze_causality(
        cause=pd.Series(
            rng.normal(size=n),
            index=_monthly_index(n),
        ),
        effect=pd.Series(
            rng.normal(size=n),
            index=_monthly_index(n),
        ),
    )

    assert "not proof" in result["interpretation"].lower()
    assert any(
        "not structural causation" in limitation
        for limitation in result["limitations"]
    )


def test_result_is_json_serialisable():
    rng = np.random.default_rng(55)
    n = 100

    result = analyze_causality(
        cause=pd.Series(
            rng.normal(size=n),
            index=_monthly_index(n),
        ),
        effect=pd.Series(
            rng.normal(size=n),
            index=_monthly_index(n),
        ),
        cause_name="x",
        effect_name="y",
    )

    json.dumps(result)
