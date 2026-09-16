"""Tests for backend.tools -- the pure functions registered as agent tools."""
import json

import numpy as np
import pandas as pd
import pytest

from backend.tools.anomaly import detect_anomalies
from backend.tools.series import SeriesResult


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
    """Point detect_anomalies at a fixed series instead of the real lakehouse.

    `load_series` returns a SeriesResult rather than a bare Series, so the fake
    carries the same metadata the real one would -- the tool now echoes the
    unit and temporal semantics into its output and a bare Series would not
    exercise that.
    """
    def _patch(series, **overrides):
        fields = dict(values=series, source="bulletin", key="entity", name="Synthetic",
                      unit="milyon TL", temporal_semantics="stock", value_column="value",
                      dataset="ds", currency="total", metric=None)
        fields.update(overrides)
        monkeypatch.setattr("backend.tools.anomaly.load_series",
                            lambda *a, **k: SeriesResult(**fields))
    return _patch


# --- core detection behaviour -----------------------------------------------

def test_flags_an_injected_level_spike(patch_series):
    series = _synthetic_series(n=36, spike_at=30, spike_value=250.0)
    patch_series(series)

    result = detect_anomalies("entity", dataset="ds", on="level", window=12)

    assert result["n_anomalies"] == 1
    anomaly = result["anomalies"][0]
    assert anomaly["period"] == "2023-07"
    assert anomaly["raw_value"] == 250.0
    assert anomaly["direction"] == "above"


def test_flags_an_injected_change_spike(patch_series):
    series = _synthetic_series(n=36)
    series.iloc[30] = series.iloc[29] * 3
    patch_series(series)

    result = detect_anomalies("entity", dataset="ds", on="change", window=12)

    assert result["n_anomalies"] >= 1
    assert any(a["period"] == series.index[30].strftime("%Y-%m") for a in result["anomalies"])


def test_stable_series_has_no_anomalies(patch_series):
    series = _synthetic_series(n=36)
    patch_series(series)

    result = detect_anomalies("entity", dataset="ds", on="level", window=12)

    assert result["n_anomalies"] == 0
    assert result["anomalies"] == []


def test_result_is_json_serialisable(patch_series):
    series = _synthetic_series(n=36, spike_at=30, spike_value=250.0)
    patch_series(series)

    result = detect_anomalies("entity", dataset="ds", on="level", window=12)

    json.dumps(result)  # raises if anything (e.g. a numpy/pandas scalar) leaks through


def test_single_method_agreement_is_not_enough(patch_series):
    """A point far outside the IQR fence but within a z-threshold that never
    triggers must not be reported -- both checks must agree."""
    series = _synthetic_series(n=36, spike_at=30, spike_value=250.0)
    patch_series(series)

    result = detect_anomalies("entity", dataset="ds", on="level", window=12, z_threshold=100.0)

    assert result["n_anomalies"] == 0


# --- parameter validation ---------------------------------------------------

def test_rejects_invalid_on(patch_series):
    patch_series(_synthetic_series())
    with pytest.raises(ValueError, match="on must be"):
        detect_anomalies("entity", dataset="ds", on="bogus")


def test_rejects_too_small_window(patch_series):
    patch_series(_synthetic_series())
    with pytest.raises(ValueError, match="window must be"):
        detect_anomalies("entity", dataset="ds", window=2)


def test_rejects_series_shorter_than_window(patch_series):
    patch_series(_synthetic_series(n=10))
    with pytest.raises(ValueError, match="scoreable point"):
        detect_anomalies("entity", dataset="ds", on="level", window=12)


# --- integration against the real lakehouse ---------------------------------

def test_runs_against_real_housing_loan_series():
    """Smoke test on the pinned tier-1 table: must run end to end and stay
    internally consistent, without asserting a specific anomaly count -- new
    months of real data can change what gets flagged."""
    result = detect_anomalies("tuketici_kredileri_konut")

    assert result["n_points"] == 67  # 2021-01 .. 2026-07, per CLAUDE.md
    assert result["period_start"] == "2021-01"
    assert result["n_anomalies"] == len(result["anomalies"])
    # The unit is not decoration: a flagged value reported without it is not an
    # answer, and this corpus mixes milyon TL with bin TL.
    assert result["unit"] == "milyon TL"
    assert result["temporal_semantics"] == "stock"
    assert result["citation"]["table"] == "bulletin_observations"
    json.dumps(result)


def test_a_cumulative_series_is_scored_on_its_flow_not_its_reset():
    """kar_zarar is year-to-date. Scored on `value`, every January is a huge
    negative "anomaly" that is really the reset; load_series serves value_flow."""
    result = detect_anomalies("donem_net_kari_zarari", dataset="kar_zarar", on="change")

    assert result["value_column"] == "value_flow"
    assert result["temporal_semantics"] == "cumulative_ytd"
    assert not any(a["period"].endswith("-01") and a["direction"] == "below"
                   for a in result["anomalies"]), "January reset leaked into the scores"
