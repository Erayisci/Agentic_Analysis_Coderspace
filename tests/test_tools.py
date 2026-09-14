"""Tests for backend.tools -- the pure functions registered as agent tools."""
import json

import numpy as np
import pandas as pd
import pytest

from backend.tools.anomaly import detect_anomalies


def _synthetic_series(n=36, spike_at=None, spike_value=None, freq="MS"):
    """A flat-ish series with an optional single injected spike, indexed by month."""
    rng = np.random.default_rng(0)
    values = 100 + rng.normal(0, 1, n)
    if spike_at is not None:
        values[spike_at] = spike_value
    index = pd.date_range("2021-01-01", periods=n, freq=freq)
    return pd.Series(values, index=index, name="synthetic")


@pytest.fixture
def patch_series(monkeypatch):
    """Point detect_anomalies at a fixed series instead of the real lakehouse."""
    def _patch(series):
        monkeypatch.setattr("backend.tools.anomaly.load_series", lambda *a, **k: series)
    return _patch


# --- core detection behaviour -----------------------------------------------

def test_flags_an_injected_level_spike(patch_series):
    series = _synthetic_series(n=36, spike_at=30, spike_value=250.0)
    patch_series(series)

    result = detect_anomalies("ds", "entity", on="level", window=12)

    assert result["n_anomalies"] == 1
    anomaly = result["anomalies"][0]
    assert anomaly["period"] == "2023-07"
    assert anomaly["raw_value"] == 250.0
    assert anomaly["direction"] == "above"


def test_flags_an_injected_change_spike(patch_series):
    series = _synthetic_series(n=36)
    series.iloc[30] = series.iloc[29] * 3
    patch_series(series)

    result = detect_anomalies("ds", "entity", on="change", window=12)

    assert result["n_anomalies"] >= 1
    assert any(a["period"] == series.index[30].strftime("%Y-%m") for a in result["anomalies"])


def test_stable_series_has_no_anomalies(patch_series):
    series = _synthetic_series(n=36)
    patch_series(series)

    result = detect_anomalies("ds", "entity", on="level", window=12)

    assert result["n_anomalies"] == 0
    assert result["anomalies"] == []


def test_result_is_json_serialisable(patch_series):
    series = _synthetic_series(n=36, spike_at=30, spike_value=250.0)
    patch_series(series)

    result = detect_anomalies("ds", "entity", on="level", window=12)

    json.dumps(result)  # raises if anything (e.g. a numpy/pandas scalar) leaks through


def test_single_method_agreement_is_not_enough(patch_series):
    """A point far outside the IQR fence but within a z-threshold that never
    triggers must not be reported -- both checks must agree."""
    series = _synthetic_series(n=36, spike_at=30, spike_value=250.0)
    patch_series(series)

    result = detect_anomalies("ds", "entity", on="level", window=12, z_threshold=100.0)

    assert result["n_anomalies"] == 0


# --- parameter validation ---------------------------------------------------

def test_rejects_invalid_on(patch_series):
    patch_series(_synthetic_series())
    with pytest.raises(ValueError, match="on must be"):
        detect_anomalies("ds", "entity", on="bogus")


def test_rejects_too_small_window(patch_series):
    patch_series(_synthetic_series())
    with pytest.raises(ValueError, match="window must be"):
        detect_anomalies("ds", "entity", window=2)


def test_rejects_series_shorter_than_window(patch_series):
    patch_series(_synthetic_series(n=10))
    with pytest.raises(ValueError, match="scoreable point"):
        detect_anomalies("ds", "entity", on="level", window=12)


# --- integration against the real lakehouse ---------------------------------

def test_runs_against_real_housing_loan_series():
    """Smoke test on the pinned tier-1 table: must run end to end and stay
    internally consistent, without asserting a specific anomaly count -- new
    months of real data can change what gets flagged."""
    result = detect_anomalies("tuketici_kredileri", "tuketici_kredileri_konut")

    assert result["n_points"] == 67  # 2021-01 .. 2026-07, per CLAUDE.md
    assert result["period_start"] == "2021-01"
    assert result["n_anomalies"] == len(result["anomalies"])
    json.dumps(result)
