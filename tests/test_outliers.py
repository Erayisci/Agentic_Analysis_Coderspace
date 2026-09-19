"""The shared outlier rule must be exactly what detect_anomalies computed before
the split, and both tools must keep agreeing on real data."""
import numpy as np
import pandas as pd
import pytest

from backend.core.config import DUCKDB_PATH
from backend.tools.anomaly import detect_anomalies
from backend.tools.outliers import score_outliers, validate_params
from backend.tools.series import load_series


def _series(n=36, spike_at=None, spike_value=None):
    rng = np.random.default_rng(0)
    values = 100 + rng.normal(0, 1, n)
    if spike_at is not None:
        values[spike_at] = spike_value
    return pd.Series(values, index=pd.date_range("2021-01-01", periods=n, freq="MS"), name="synthetic")


def test_scores_flag_an_injected_spike_and_nothing_else():
    scores = score_outliers(_series(spike_at=20, spike_value=130), on="level", window=12)
    assert [p.strftime("%Y-%m") for p in scores.periods] == ["2022-09"]
    assert scores.is_outlier.dtype == bool
    assert scores.z_score.iloc[:11].isna().all()          # no full window yet -> not scored


def test_stable_series_has_no_outliers():
    assert score_outliers(_series(), on="change", window=12).periods == []


def test_both_checks_must_agree():
    # A mild bump breaches the IQR fence but not |z| >= 3 -> not an outlier.
    s = _series(spike_at=20, spike_value=103.5)
    scores = score_outliers(s, on="level", window=12, iqr_multiplier=0.5)
    assert scores.periods == []


def test_parameter_validation_matches_the_anomaly_tool():
    with pytest.raises(ValueError, match="on must be"):
        validate_params("delta", 12)
    with pytest.raises(ValueError, match="window must be"):
        validate_params("level", 2)
    with pytest.raises(ValueError, match="scoreable point"):
        score_outliers(_series(n=10), on="level", window=12)


@pytest.mark.skipif(not DUCKDB_PATH.exists(), reason="run the lakehouse build first")
@pytest.mark.parametrize("key,kwargs", [
    ("tuketici_kredileri_konut", {}),
    ("TP.KTF12", {"source": "macro"}),
    ("donem_net_kari_zarari", {}),
])
def test_wrapper_and_shared_rule_flag_the_same_periods_on_real_data(key, kwargs):
    via_tool = detect_anomalies(key, on="change", window=12, **kwargs)
    loaded = load_series(key, **kwargs)
    direct = score_outliers(loaded.values, on="change", window=12)
    assert [a["period"] for a in via_tool["anomalies"]] == [p.strftime("%Y-%m") for p in direct.periods]
    assert via_tool["n_scored"] == len(direct.scored)


# --- classification: spike vs regime start (shared by both tools) ---------

from backend.tools.outliers import classify_flags, default_lookahead   # noqa: E402
from backend.tools.series import SeriesResult                          # noqa: E402


def _step(n=60, at=40, level=4.0, noise=0.15):
    rng = np.random.default_rng(3)
    values = np.where(np.arange(n) < at, 1.0, level) + rng.normal(0, noise, n)
    return pd.Series(values, index=pd.date_range("2021-01-01", periods=n, freq="MS"), name="step")


def test_default_lookahead_is_a_quarter_of_the_window():
    assert default_lookahead(12) == 3
    assert default_lookahead(52) == 13
    assert default_lookahead(4) == 2          # never below 2


def test_a_spike_is_classified_as_spike():
    scores = score_outliers(_series(spike_at=20, spike_value=130), on="level", window=12)
    kinds = classify_flags(scores, lookahead=3)
    assert [(p.strftime("%Y-%m"), k) for p, k in kinds.items()] == [("2022-09", "spike")]


def test_the_first_point_of_a_step_is_classified_as_regime_start():
    scores = score_outliers(_step(), on="level", window=12)
    kinds = classify_flags(scores, lookahead=3)
    assert kinds, "the step's first point should be flagged"
    first = min(kinds)
    assert first.strftime("%Y-%m") == "2024-05" and kinds[first] == "regime_start"


def test_a_flag_at_the_very_end_is_undetermined():
    scores = score_outliers(_series(n=30, spike_at=29, spike_value=130), on="level", window=12)
    kinds = classify_flags(scores, lookahead=3)
    assert list(kinds.values()) == ["undetermined"]


@pytest.fixture
def patch_series(monkeypatch):
    def _patch(series):
        fields = dict(values=series, source="bulletin", key="entity", name="Synthetic", unit="milyon TL",
                      temporal_semantics="stock", value_column="value", dataset="ds", currency="total", metric=None)
        monkeypatch.setattr("backend.tools.anomaly.load_series", lambda *a, **k: SeriesResult(**fields))
    return _patch


def test_anomaly_tool_reports_kind_and_kinds_summary(patch_series):
    patch_series(_series(spike_at=20, spike_value=130))
    out = detect_anomalies("entity", on="level", window=12)
    assert out["lookahead"] == 3
    assert [(a["period"], a["kind"]) for a in out["anomalies"]] == [("2022-09", "spike")]
    assert out["kinds"] == {"spike": 1, "regime_start": 0, "undetermined": 0}
    assert sum(out["kinds"].values()) == out["n_anomalies"]


def test_anomaly_tool_calls_a_step_a_regime_start(patch_series):
    patch_series(_step())
    out = detect_anomalies("entity", on="level", window=12)
    assert out["anomalies"][0]["period"] == "2024-05"
    assert out["anomalies"][0]["kind"] == "regime_start"


def test_anomaly_tool_output_is_json_serialisable_with_kind(patch_series):
    import json
    patch_series(_series(spike_at=20, spike_value=130))
    json.dumps(detect_anomalies("entity", on="level", window=12))


@pytest.mark.skipif(not DUCKDB_PATH.exists(), reason="run the lakehouse build first")
def test_both_tools_call_the_mortgage_rate_jump_a_regime_start_on_the_level_scale():
    from backend.tools.change_detection import detect_change_points_for
    anomaly = detect_anomalies("TP.KTF12", source="macro", on="level")
    assert [(a["period"], a["kind"]) for a in anomaly["anomalies"]] == [("2023-07", "regime_start")]
    change = detect_change_points_for("TP.KTF12", source="macro")
    assert "2023-07" in change["outliers"]["kept_as_regime_start"]
    assert change["breakpoints"] == ["2023-07"]


def test_anomaly_tool_reports_day_precise_periods_on_weekly_data(patch_series):
    weekly = pd.Series(100 + np.random.default_rng(0).normal(0, 1, 80),
                       index=pd.date_range("2021-01-08", periods=80, freq="W-FRI"), name="weekly")
    weekly.iloc[60] = 130
    patch_series(weekly)
    out = detect_anomalies("entity", on="level", window=12)
    assert out["period_start"] == "2021-01-08" and len(out["period_end"]) == 10
    assert [a["period"] for a in out["anomalies"]] == [weekly.index[60].strftime("%Y-%m-%d")]


def test_anomaly_tool_keeps_month_periods_on_monthly_data(patch_series):
    patch_series(_series(spike_at=20, spike_value=130))
    out = detect_anomalies("entity", on="level", window=12)
    assert out["anomalies"][0]["period"] == "2022-09" and out["period_start"] == "2021-01"


def _growing(n=40, monthly=0.02, noise=0.003, seed=1):
    """About `monthly` growth per month with a little noise, like real data."""
    rng = np.random.default_rng(seed)
    values = 100 * np.cumprod(1 + monthly + rng.normal(0, noise, n))
    return pd.Series(values, index=pd.date_range("2021-01-01", periods=n, freq="MS"), name="g")


def test_change_score_across_a_gap_is_per_period():
    gapped = _growing().drop(_growing().index[20])
    scores = score_outliers(gapped, on="change", window=12)
    at_gap = scores.scored.loc[gapped.index[20]]             # first point after the hole
    assert 1.0 < at_gap < 3.0                                 # ~2% per month, not ~4%
    assert scores.periods == []                               # a hole is not an anomaly
    # the naive (undivided) change at the same point would have been flagged
    naive = gapped.pct_change(fill_method=None) * 100
    assert naive.loc[gapped.index[20]] > 3.5


def test_anomaly_tool_lists_gaps_and_does_not_flag_them(patch_series):
    patch_series(_growing().drop(_growing().index[20]))
    out = detect_anomalies("entity", on="change", window=12)
    assert out["gaps"] == [{"after": "2022-08", "before": "2022-10", "missing_periods": 1}]
    assert out["n_anomalies"] == 0
