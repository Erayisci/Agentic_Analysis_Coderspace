"""Tests for backend.tools -- the pure functions registered as agent tools."""
import json

import numpy as np
import pandas as pd
import pytest

from backend.tools.anomaly import detect_anomalies
from backend.tools.series import SeriesResult


def _synthetic_series(n=36, spike_at=None, spike_value=None, freq="MS"):
    """A flat-ish series with an optional single injected spike, indexed by month.

    Uniform noise on purpose: it has no tails, so a "stable" series really
    contains no point more than ~1.7 sd from a trailing 12-month baseline.
    Gaussian noise does -- seed 0 put its 13th point 3.3 sd below the twelve
    before it, which a trailing baseline correctly flags and an inclusive one
    (contaminated by the point itself) happened to hide.
    """
    rng = np.random.default_rng(0)
    values = 100 + rng.uniform(-1, 1, n)
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

    result = detect_anomalies("entity", dataset="ds", on="level", window=12, z_threshold=1e6)

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


# --- anomaly: baseline and semantics ------------------------------------------

def test_the_baseline_excludes_the_scored_point(patch_series):
    """An inclusive window lets a spike inflate its own baseline std and hide;
    the baseline must be the twelve points BEFORE the one being scored."""
    from backend.tools.anomaly import _score
    series = _synthetic_series(n=36, spike_at=30, spike_value=250.0)
    result = _score(series, "level", 12, 3.0, 1.5, semantics="stock")
    # With the spike excluded from its own baseline the z-score is enormous;
    # an inclusive window (spike inside its own std) gave ~3.
    assert result["anomalies"][0]["z_score"] > 50
    # And the month AFTER the spike is scored against a baseline that now
    # contains the spike, so it is not flagged for merely returning to normal.
    assert all(a["period"] != "2023-08" for a in result["anomalies"])


def test_a_rate_series_is_scored_in_points_not_percent_of_percent(patch_series):
    series = _synthetic_series(n=36)
    series.iloc[30] = series.iloc[29] + 12.0    # a 12-point jump on a ~100 level
    patch_series(series, unit="%", temporal_semantics="rate")
    result = detect_anomalies("entity", dataset="ds")   # on="auto"
    assert result["scored_on"] == "diff" and result["scored_unit"] == "puan"
    assert any(a["period"] == "2023-07" and abs(a["scored_value"] - 12.0) < 1e-6 for a in result["anomalies"])


def test_a_stock_series_is_scored_on_its_percent_change_by_default(patch_series):
    patch_series(_synthetic_series(n=36))
    result = detect_anomalies("entity", dataset="ds")
    assert result["scored_on"] == "change" and result["scored_unit"] == "%"


def test_a_flow_near_zero_is_left_unscored_not_exploded(patch_series):
    series = _synthetic_series(n=36)
    series.iloc[20] = 0.01                      # a flow month at ~zero
    patch_series(series, temporal_semantics="flow")
    result = detect_anomalies("entity", dataset="ds")
    assert result["n_unscored"] >= 1
    # 0.01 -> ~100 is a +1,000,000% change; it must not be reported as one.
    assert all(abs(a["scored_value"]) < 10_000 for a in result["anomalies"])


def test_anomaly_result_describes_itself_in_turkish(patch_series):
    patch_series(_synthetic_series(n=36, spike_at=30, spike_value=250.0))
    result = detect_anomalies("entity", dataset="ds", on="level", window=12)
    assert "onceki 12 ayin" in result["description"] and "1 ay bulundu: 2023-07" in result["description"]


def test_the_trailing_baseline_finds_the_housing_loan_breaks():
    """Measured on the real series: the inclusive baseline flagged nothing,
    the trailing one flags the two months the change really broke from its
    history. Pinned by period, not count -- new months may add flags."""
    result = detect_anomalies("tuketici_kredileri_konut")
    assert "2023-03" in {a["period"] for a in result["anomalies"]}


# --- changepoint --------------------------------------------------------------

def _level_shift(n_before=30, n_after=30, before=100.0, after=130.0, sigma=1.0):
    rng = np.random.default_rng(1)
    values = np.concatenate([before + rng.normal(0, sigma, n_before), after + rng.normal(0, sigma, n_after)])
    index = pd.date_range("2021-01-01", periods=n_before + n_after, freq="MS")
    return pd.Series(values, index=index)


def _describe(unit="milyon TL", semantics="stock"):
    return {"name": "Synthetic", "unit": unit, "temporal_semantics": semantics, "value_column": "value"}


def test_changepoint_finds_an_injected_level_shift_and_reports_its_size():
    from backend.tools.changepoint import detect_changepoints_in_series
    result = detect_changepoints_in_series(_level_shift(), _describe(), {})
    assert result["n_breakpoints"] == 1
    b = result["breakpoints"][0]
    assert b["period"] == "2023-07" and b["direction"] == "up" and b["shift_unit"] == "%"
    assert abs(b["shift"] - 30.0) < 2.0               # +30% in the series' own terms
    assert "1 kirilma" in result["description"] and "2023-07" in result["description"]


def test_changepoint_reports_a_rate_shift_in_points():
    from backend.tools.changepoint import detect_changepoints_in_series
    result = detect_changepoints_in_series(_level_shift(before=18.0, after=40.0, sigma=0.5),
                                           _describe(unit="%", semantics="rate"), {})
    b = result["breakpoints"][0]
    assert b["shift_unit"] == "puan" and abs(b["shift"] - 22.0) < 1.0


@pytest.mark.parametrize("n", [60, 240])
def test_changepoint_penalty_scales_with_length_so_noise_has_no_breaks(n):
    from backend.tools.changepoint import detect_changepoints_in_series
    rng = np.random.default_rng(2)
    series = pd.Series(100 + rng.normal(0, 1, n), index=pd.date_range("2021-01-01", periods=n, freq="MS"))
    result = detect_changepoints_in_series(series, _describe(), {})
    assert result["n_breakpoints"] == 0 and "tek rejim" in result["description"]


def test_changepoint_segments_carry_slopes():
    from backend.tools.changepoint import detect_changepoints_in_series
    result = detect_changepoints_in_series(_level_shift(), _describe(), {})
    assert len(result["segments"]) == 2
    assert all("slope_per_month" in s and "mean" in s for s in result["segments"])
    json.dumps(result)


def test_changepoint_on_the_housing_rate_finds_the_2023_regime_shift():
    from backend.tools.changepoint import detect_changepoints
    result = detect_changepoints("TP.KTF12", source="macro", currency=None)
    assert "2023-07" in {b["period"] for b in result["breakpoints"]}
    assert result["shift_unit"] == "puan"


# --- causality ----------------------------------------------------------------

def _lead_lag_pair(n=120, lag=2, beta=0.8):
    """y_t = beta * x_{t-lag} + noise: x leads y, y does not lead x."""
    rng = np.random.default_rng(3)
    x = rng.normal(0, 1, n)
    y = np.zeros(n)
    for t in range(lag, n):
        y[t] = beta * x[t - lag] + rng.normal(0, 0.3)
    index = pd.date_range("2015-01-01", periods=n, freq="MS")
    return pd.DataFrame({"x": x, "y": y}, index=index)


def test_granger_detects_an_injected_lead_lag_with_its_sign():
    from backend.tools.causality import granger_both_directions
    result = granger_both_directions(_lead_lag_pair(), target="y", predictor="x")
    assert result["verdict"] == "predictor->target"
    assert result["directions"]["x->y"]["predictive"] and result["directions"]["x->y"]["effect_sign"] == "positive"
    assert not result["directions"]["y->x"]["predictive"]
    assert result["lag"] >= 2                          # BIC has to reach the true lag


def test_granger_correlates_the_differences_and_labels_the_level_correlation():
    from backend.tools.causality import granger_both_directions
    # Two random walks share a trend by construction; the level correlation is
    # spurious and the differenced one is what the description quotes.
    rng = np.random.default_rng(4)
    n = 80
    a = np.cumsum(rng.normal(0.5, 1, n)); b = np.cumsum(rng.normal(0.5, 1, n))
    frame = pd.DataFrame({"a": a, "b": b}, index=pd.date_range("2018-01-01", periods=n, freq="MS"))
    result = granger_both_directions(frame, "a", "b")
    assert result["differenced"] and result["cointegration_p"] is not None
    assert abs(result["correlation_diff"]) < abs(result["correlation_level"])
    assert f"{result['correlation_diff']:+.2f}" in result["description"]
    assert result["description"].endswith("nedensellik kaniti degildir.")
    json.dumps(result)


# --- decomposition ------------------------------------------------------------

def test_decompose_growth_identity_holds_and_reads_real_decline():
    from backend.agent.state import AnalysisArtifact, ColumnLineage
    from backend.tools.transforms import decompose_growth
    index = pd.date_range("2021-01-01", periods=37, freq="MS")
    artifact = AnalysisArtifact()
    artifact.add_column("konut", pd.Series(np.linspace(100.0, 245.0, 37), index=index),
                        ColumnLineage(column="konut", label="Konut", source="bulletin", unit="milyon TL",
                                      temporal_semantics="stock"))
    artifact.add_column("kfe", pd.Series(np.linspace(100.0, 1239.0, 37), index=index),
                        ColumnLineage(column="kfe", label="KFE", source="macro", unit="endeks",
                                      temporal_semantics="index"))
    result = decompose_growth(artifact, "konut", "kfe")
    assert result["nominal_pct"] == 145.0 and result["price_pct"] == 1139.0
    assert abs((1 + result["nominal_pct"] / 100) / (1 + result["price_pct"] / 100) - 1 - result["real_pct"] / 100) < 1e-4  # real_pct is rounded to 2 dp
    assert result["real_pct"] < -70 and "reel stok daraldi" in result["description"]
    assert [y["year"] for y in result["by_year"]] == [2022, 2023]
    assert result["inputs"] == ["konut", "kfe"]
    json.dumps(result)


def test_decompose_refuses_a_rate_or_a_non_index_deflator():
    from backend.agent.state import AnalysisArtifact, ColumnLineage
    from backend.tools.transforms import decompose_growth
    index = pd.date_range("2021-01-01", periods=12, freq="MS")
    artifact = AnalysisArtifact()
    for name, unit, sem in (("faiz", "%", "rate"), ("konut", "milyon TL", "stock"), ("mevduat", "milyon TL", "stock")):
        artifact.add_column(name, pd.Series(np.arange(12, dtype=float) + 1, index=index),
                            ColumnLineage(column=name, label=name, source="bulletin", unit=unit, temporal_semantics=sem))
    with pytest.raises(ValueError, match="percentage"):
        decompose_growth(artifact, "faiz", "konut")
    with pytest.raises(ValueError, match="expected a price index"):
        decompose_growth(artifact, "konut", "mevduat")


def test_in_usd_divides_a_tl_amount_by_the_rate_and_relabels_the_unit():
    from backend.agent.state import AnalysisArtifact, ColumnLineage
    from backend.tools import transforms as T
    index = pd.date_range("2021-01-01", periods=3, freq="MS")
    artifact = AnalysisArtifact(title="t")
    artifact.add_column("fx", pd.Series([100.0, 200.0, 300.0], index=index), ColumnLineage(
        column="fx", label="FX", source="bulletin", unit="milyon TL", temporal_semantics="stock"))
    artifact.add_column("kur", pd.Series([10.0, 20.0, 30.0], index=index), ColumnLineage(
        column="kur", label="USD", source="macro", unit="TL", temporal_semantics="rate"))
    name = T.in_usd(artifact, "fx", "kur", "fx_usd")
    assert artifact.frame[name].tolist() == [10.0, 10.0, 10.0]
    line = artifact.lineage[name]
    assert line.unit == "milyon USD" and line.derived_from == ["fx", "kur"]
    with pytest.raises(ValueError, match="in_usd needs"):
        T.in_usd(artifact, "kur", "fx")
