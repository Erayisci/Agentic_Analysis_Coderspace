"""Change-point detection: trend / level / volatility on lakehouse series, and
the executor's `analyze: changepoint` step routed through it."""
import json

import numpy as np
import pandas as pd
import pytest

from backend.agent.executor import Executor
from backend.agent.planner import Plan, Step
from backend.agent.state import Session
from backend.core.config import DUCKDB_PATH
from backend.tools.change_detection import (detect_change_points, detect_change_points_for, growth_rate,
                                            infer_frequency, january_resets, looks_year_to_date, resolve_kind,
                                            rises_within_years)
from backend.tools.series import load_series

pytestmark = pytest.mark.skipif(not DUCKDB_PATH.exists(), reason="run the lakehouse build first")


@pytest.fixture(scope="module")
def housing():
    return load_series("tuketici_kredileri_konut")           # stock, milyon TL


@pytest.fixture(scope="module")
def mortgage_rate():
    return load_series("TP.KTF12", source="macro")            # rate, %


@pytest.fixture(scope="module")
def loan_to_deposit():
    return load_series("toplam_nakdi_krediler_toplam_mevduat", dataset="rasyolar", currency=None)


# --- helpers --------------------------------------------------------------

def test_growth_rate_shape(housing):
    growth = growth_rate(housing.values)
    assert len(growth) == len(housing.values) - 1
    assert growth.index[0].strftime("%Y-%m") == "2021-02"


def test_infer_frequency_monthly_and_weekly(housing):
    assert infer_frequency(housing.values.index)[0] == "month"
    assert infer_frequency(housing.values.resample("W").interpolate().index)[0] == "week"


def test_auto_kind_follows_semantics_and_unit():
    assert resolve_kind("auto", "stock", "milyon TL") == "trend"
    assert resolve_kind("auto", "rate", "%") == "level"
    assert resolve_kind("auto", "ratio", "%") == "level"
    assert resolve_kind("auto", None, "%") == "level"
    assert resolve_kind("volatility", "rate", "%") == "volatility"   # explicit wins


def test_year_to_date_heuristic_fires_on_raw_profit_only():
    raw = load_series("donem_net_kari_zarari", cumulative_as="ytd").values
    flow = load_series("donem_net_kari_zarari", cumulative_as="flow").values
    assert january_resets(raw) is True and rises_within_years(raw) is True
    assert looks_year_to_date(raw) is True
    assert looks_year_to_date(flow) is False


def _seasonal_flow(years=5, seed=2):
    """A monthly flow with a huge December and a tiny January, wobbling all year:
    resets like a running total but is not one."""
    rng = np.random.default_rng(seed)
    pattern = np.array([1, 3, 2, 4, 3, 5, 4, 6, 5, 7, 6, 20], dtype=float)   # Jan..Dec
    values = np.tile(pattern, years) * (1 + rng.normal(0, .05, 12 * years))
    return _monthly(values, "seasonal_flow")


def test_seasonal_flow_resets_in_january_but_is_not_year_to_date():
    flow = _seasonal_flow()
    assert january_resets(flow) is True                 # the reset test alone would misfire
    assert rises_within_years(flow) is False            # ...but it falls within years
    assert looks_year_to_date(flow) is False
    result = detect_change_points(flow, kind="level")   # accepted, with a warning, not refused
    assert any("mevsimsellik" in w for w in result["warnings"])


def test_a_running_total_rises_within_years_and_is_refused():
    rng = np.random.default_rng(4)
    monthly_flow = np.abs(rng.normal(10, 3, 60))
    ytd = np.concatenate([np.cumsum(monthly_flow[i:i + 12]) for i in range(0, 60, 12)])
    series = _monthly(ytd, "ytd")
    assert looks_year_to_date(series) is True
    with pytest.raises(ValueError, match="year-to-date"):
        detect_change_points(series, kind="level")


# --- trend (balances) -----------------------------------------------------

def test_trend_break_on_housing_loans(housing):
    result = detect_change_points(housing.values, kind="trend")
    json.dumps(result)
    assert result["frequency"] == "month" and result["min_segment"] == 4
    assert result["n_breakpoints"] == len(result["breaks"]) == len(result["breakpoints"])
    assert any(p.startswith("2023") for p in result["breakpoints"]), result["breakpoints"]
    assert sum(s["periods"] for s in result["segments"]) == len(housing.values) - 1


# --- level (rates, ratios) ------------------------------------------------

def test_auto_picks_level_for_the_mortgage_rate(mortgage_rate):
    result = detect_change_points(mortgage_rate.values, kind="auto",
                                  temporal_semantics=mortgage_rate.temporal_semantics, unit=mortgage_rate.unit)
    assert result["kind"] == "level"
    assert result["measure"].startswith("ortalama değer")
    assert any(p.startswith("2023") for p in result["breakpoints"]), result["breakpoints"]   # 2023 hiking cycle
    first, last = result["segments"][0]["value"], result["segments"][-1]["value"]
    assert last > first                                                                       # ~18% -> ~35%+


def test_level_break_on_loan_to_deposit(loan_to_deposit):
    result = detect_change_points(loan_to_deposit.values, kind="level")
    assert any(p.startswith("2022") for p in result["breakpoints"]), result["breakpoints"]
    assert result["segments"][0]["value"] > result["segments"][-1]["value"]                 # ~106% -> ~87%


# --- volatility -----------------------------------------------------------

def test_volatility_runs_and_is_serialisable(housing):
    result = detect_change_points(housing.values, kind="volatility")
    json.dumps(result)
    assert all(s["value"] >= 0 for s in result["segments"])


# --- frequency handling ---------------------------------------------------

def test_weekly_series_reports_weekly_periods(housing):
    weekly = housing.values.resample("W").interpolate()
    weekly.name = "housing_weekly"
    result = detect_change_points(weekly, kind="trend")
    assert result["frequency"] == "week" and result["min_segment"] == 13
    assert "haftalık" in result["measure"]
    assert len(result["segments"][0]["start"]) == 10                                          # YYYY-MM-DD


def test_real_weekly_bulletin_series_is_accepted():
    loaded = load_series("5690", source="weekly")   # weekly bulletin: a) Konut under Krediler
    result = detect_change_points(loaded.values, temporal_semantics=loaded.temporal_semantics, unit=loaded.unit)
    assert result["frequency"] == "week"
    assert sum(s["periods"] for s in result["segments"]) == len(loaded.values) - 1


def _monthly(values, name):
    return pd.Series(values, index=pd.date_range("2021-01-01", periods=len(values), freq="MS"), name=name)


# --- confidence grading ---------------------------------------------------

def test_every_break_carries_a_consistent_confidence_grade(housing):
    result = detect_change_points(housing.values, kind="trend")
    for b in result["breaks"]:
        assert 1 <= b["support"] <= 3
        assert b["confidence"] == {3: "solid", 2: "moderate", 1: "tentative"}[b["support"]]
        assert b["shift_in_sd"] >= 0
        assert b["period"] in result["breaks_by_sensitivity"][result["sensitivity"]]
    assert sum(result["confidence_summary"].values()) == result["n_breakpoints"]
    assert set(result["breaks_by_sensitivity"]) == {"low", "medium", "high"}


def test_a_break_with_a_short_after_regime_is_marked_recent():
    rng = np.random.default_rng(5)
    noise = _monthly(1 + rng.normal(0, .2, 60), "noise")
    result = detect_change_points(noise, kind="level")
    assert result["breaks"], "this seeded noise series produces an end-of-series break at medium"
    last = result["breaks"][-1]
    assert last["recent"] is True and result["segments"][-1]["periods"] < 2 * result["min_segment"]
    assert all(b["recent"] is False for b in result["breaks"][:-1])
    # the housing burst of 2023 is short but ENDED -- not recent; and its last regime is long
    housing_result = detect_change_points(load_series("tuketici_kredileri_konut").values, kind="trend")
    assert all(b["recent"] is False for b in housing_result["breaks"])


def test_adjacent_spikes_are_both_capped_across_passes():
    rng = np.random.default_rng(5)
    two = _monthly(1 + rng.normal(0, .2, 60), "two")
    two.iloc[[20, 21]] = 8.0
    result = detect_change_points(two, kind="level")
    capped = {c["period"]: c["pass"] for c in result["outliers"]["capped"]}
    # With a trailing baseline the second spike is scored against a window
    # that holds the first, and 8.0 still clears it: both go in pass 1, and
    # the repeat pass exists for the case where the first one masks the next.
    assert set(capped) == {"2022-09", "2022-10"}
    assert all(c["kind"] == "spike" for c in result["outliers"]["capped"])
    assert result["breakpoints"] == []                     # no false regime around the pair
    assert result["outliers"]["passes"] == max(capped.values()) + 1   # the last pass found nothing new


def test_break_found_at_the_strictest_setting_is_solid(housing):
    result = detect_change_points(housing.values, kind="trend", sensitivity="medium")
    strict = result["breaks_by_sensitivity"]["low"]
    assert strict, "expected at least one break at low sensitivity on housing loans"
    graded = {b["period"]: b["confidence"] for b in result["breaks"]}
    assert all(graded[p] == "solid" for p in strict if p in graded)


def test_high_sensitivity_exposes_tentative_breaks(housing):
    high = detect_change_points(housing.values, kind="trend", sensitivity="high")
    medium = detect_change_points(housing.values, kind="trend", sensitivity="medium")
    assert high["confidence_summary"]["tentative"] >= 1          # breaks only high finds
    assert medium["confidence_summary"]["tentative"] == 0         # medium's breaks all also appear at high


def test_tolerance_matches_breaks_nudged_by_an_observation(housing):
    # medium finds 2023-03 / 2023-07, high finds 2023-02 / 2023-06: same events, one month apart.
    strict = detect_change_points(housing.values, kind="trend", match_tolerance=2)
    exact = detect_change_points(housing.values, kind="trend", match_tolerance=0)
    by_period = lambda r: {b["period"]: b["support"] for b in r["breaks"]}   # noqa: E731
    assert by_period(strict)["2023-07"] >= 2
    assert by_period(exact)["2023-07"] <= by_period(strict)["2023-07"]
    assert strict["match_tolerance"] == 2 and exact["match_tolerance"] == 0


def test_level_break_on_the_rate_is_solid(mortgage_rate):
    result = detect_change_points(mortgage_rate.values, kind="level")
    assert result["breaks"] and all(b["confidence"] == "solid" for b in result["breaks"])


# --- outlier capping (shared rule with tools.anomaly) ---------------------


@pytest.fixture(scope="module")
def step_with_spike():
    rng = np.random.default_rng(4)      # seed 3's noise holds a 3-sigma dip at index 36
    base = np.where(np.arange(60) < 40, 1.0, 2.0) + rng.normal(0, .3, 60)
    clean = _monthly(base, "step")
    spiky = clean.copy()
    spiky.iloc[20] = 9.0
    return clean, spiky


def test_a_spike_no_longer_hides_a_real_break(step_with_spike):
    clean, spiky = step_with_spike
    assert detect_change_points(clean, kind="level")["breakpoints"] == ["2024-05"]
    assert detect_change_points(spiky, kind="level", cap_outliers=False)["breakpoints"] == []
    capped = detect_change_points(spiky, kind="level")
    assert capped["breakpoints"] == ["2024-05"]
    by_period = {c["period"]: c for c in capped["outliers"]["capped"]}
    assert by_period["2022-09"]["original"] == 9.0 and by_period["2022-09"]["kind"] == "spike"
    json.dumps(capped)


def test_the_first_month_of_a_new_regime_is_kept_not_capped():
    rng = np.random.default_rng(4)
    big = _monthly(np.where(np.arange(60) < 40, 1.0, 4.0) + rng.normal(0, .15, 60), "bigstep")
    result = detect_change_points(big, kind="level")
    assert result["breakpoints"] == ["2024-05"]
    assert result["outliers"]["capped"] == []
    assert "2024-05" in result["outliers"]["kept_as_regime_start"]


def test_mortgage_rate_jump_is_a_regime_start_not_an_outlier(mortgage_rate):
    result = detect_change_points(mortgage_rate.values, kind="level")
    assert result["breakpoints"] == ["2023-07"]
    assert result["outliers"]["capped"] == []
    assert "2023-07" in result["outliers"]["kept_as_regime_start"]


def test_capping_the_housing_anomaly_month_does_not_alter_the_breaks(housing):
    """The shared rule flags 2023-03 on the housing growth series -- the same
    month the anomaly tool's eval gold names -- and caps it as a spike; the
    trend breaks are the same with and without that cap."""
    on = detect_change_points(housing.values, kind="trend")
    off = detect_change_points(housing.values, kind="trend", cap_outliers=False)
    assert on["breakpoints"] == off["breakpoints"]
    assert on["outliers"]["applied"] is True
    assert [c["period"] for c in on["outliers"]["capped"]] == ["2023-03"]


def test_volatility_never_caps(step_with_spike):
    _, spiky = step_with_spike
    result = detect_change_points(spiky, kind="volatility")
    assert result["outliers"]["applied"] is False
    assert "volatility" in result["outliers"]["reason"]


def test_short_series_skips_the_outlier_check_without_failing(step_with_spike):
    clean, _ = step_with_spike
    result = detect_change_points(clean.iloc[:11], kind="level", min_segment=4)
    assert result["outliers"]["applied"] is False
    assert "window" in result["outliers"]["reason"]


# --- shape, seasonality, gaps ---------------------------------------------

def test_slow_curve_is_called_gradual_not_stepwise():
    rng = np.random.default_rng(3)
    curve = _monthly(0.5 + 2.5 / (1 + np.exp(-(np.arange(60) - 30) / 8)) + rng.normal(0, .25, 60), "curve")
    result = detect_change_points(curve, kind="level")
    assert result["shape"] == "gradual"
    assert result["trend_line"]["r2"] > 0.8 and result["trend_line"]["slope_per_period_sd"] > 0
    assert any("kademeli" in w for w in result["warnings"])


def test_real_steps_are_stepwise_and_noise_is_flat(housing, mortgage_rate):
    assert detect_change_points(housing.values, kind="trend")["shape"] == "stepwise"
    assert detect_change_points(mortgage_rate.values, kind="level")["shape"] == "stepwise"
    rng = np.random.default_rng(11)
    flat = detect_change_points(_monthly(1 + rng.normal(0, .2, 60), "flat"), kind="level")
    assert flat["shape"] == "flat" and flat["breakpoints"] == [] and flat["warnings"] == []


def test_yearly_pattern_is_warned_about_but_a_level_shift_is_not(mortgage_rate):
    rng = np.random.default_rng(3)
    wave = _monthly(1.0 + 1.5 * np.sin(2 * np.pi * np.arange(60) / 12) + rng.normal(0, .2, 60), "wave")
    result = detect_change_points(wave, kind="level")
    assert result["seasonal_autocorr"] > 0.9
    assert any("yıllık örüntü" in w for w in result["warnings"])
    # the mortgage rate's big 2023 step is persistence, not seasonality: no warning
    rate = detect_change_points(mortgage_rate.values, kind="level")
    assert abs(rate["seasonal_autocorr"]) < 0.3 and not any("yıllık" in w for w in rate["warnings"])


def test_growth_across_a_gap_is_per_period():
    steady = _monthly(100 * 1.02 ** np.arange(60), "steady")       # exactly 2%/month
    gapped = steady.drop(steady.index[30])                          # one month missing
    growth = growth_rate(gapped)
    assert abs(growth.iloc[29] - growth.iloc[0]) < 0.01            # the gap month is still ~2%, not ~4%
    result = detect_change_points(gapped, kind="trend")
    assert result["gaps"] == [{"after": "2023-06", "before": "2023-08", "missing_periods": 1}]
    assert result["breakpoints"] == []                               # a steady series has no break
    assert any("boşluk" in w for w in result["warnings"])


def test_real_series_report_no_gaps_and_are_serialisable(housing):
    result = detect_change_points(housing.values, kind="trend")
    assert result["gaps"] == []
    json.dumps(result)


# --- guards ---------------------------------------------------------------

def test_raw_year_to_date_series_is_refused():
    raw = load_series("donem_net_kari_zarari", cumulative_as="ytd")
    with pytest.raises(ValueError, match="year-to-date"):
        detect_change_points(raw.values, temporal_semantics=raw.temporal_semantics, unit=raw.unit)


def test_decumulated_flow_is_accepted():
    result = detect_change_points_for("donem_net_kari_zarari")   # loader serves the flow
    assert result["kind"] == "trend" or result["kind"] == "level"
    assert "citation" in result and result["temporal_semantics"] == "cumulative_ytd"


def test_higher_sensitivity_never_finds_fewer_breaks(housing):
    low = detect_change_points(housing.values, sensitivity="low")["n_breakpoints"]
    high = detect_change_points(housing.values, sensitivity="high")["n_breakpoints"]
    assert high >= low


def test_bad_arguments_rejected(housing):
    with pytest.raises(ValueError):
        detect_change_points(housing.values, kind="magnitude")
    with pytest.raises(ValueError):
        detect_change_points(housing.values, sensitivity="extreme")


# --- through the agent's executor -----------------------------------------

def test_executor_changepoint_step_uses_lineage_to_pick_kind():
    plan = Plan(intent="series_analysis", start="2021-01-01", end="2025-12-01", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin",
             dataset="tuketici_kredileri", as_name="konut"),
        Step(op="fetch_series", key="TP.KTF12", source="macro", as_name="faiz"),
        Step(op="analyze", method="changepoint", column="konut"),
        Step(op="analyze", method="changepoint", column="faiz"),
    ])
    session = Executor(Session()).run(plan)
    assert all(step.ok for step in session.audit), [s.detail for s in session.audit if not s.ok]
    konut = session.facts["analysis"]["changepoint:konut"]
    faiz = session.facts["analysis"]["changepoint:faiz"]
    assert konut["kind"] == "trend" and konut["unit"] == "milyon TL"
    assert faiz["kind"] == "level" and faiz["unit"] == "%"
    assert konut["n_breakpoints"] >= 1 and faiz["n_breakpoints"] >= 1
    json.dumps(session.facts["analysis"])


def test_plan_can_ask_for_volatility_and_sensitivity_and_the_executor_passes_them_on():
    plan = Plan(intent="series_analysis", start="2021-01-01", end="2025-12-01", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin",
             dataset="tuketici_kredileri", as_name="konut"),
        Step(op="analyze", method="changepoint", column="konut", kind="volatility", sensitivity="high"),
    ])
    session = Executor(Session()).run(plan)
    assert all(step.ok for step in session.audit), [s.detail for s in session.audit if not s.ok]
    result = session.facts["analysis"]["changepoint:konut"]
    assert result["kind"] == "volatility" and result["sensitivity"] == "high"
    assert result["outliers"]["applied"] is False                     # volatility never caps
    assert "dalgalanma" in result["measure"]


def test_step_rejects_unknown_kind_and_sensitivity():
    with pytest.raises(Exception):
        Step(op="analyze", method="changepoint", column="konut", kind="magnitude")
    with pytest.raises(Exception):
        Step(op="analyze", method="changepoint", column="konut", sensitivity="extreme")
    assert Step(op="analyze", method="changepoint", column="konut").kind is None   # unset means auto


def test_planner_prompt_tells_the_model_when_to_use_volatility():
    from backend.agent.planner import PLANNER_SYSTEM
    assert "kind=volatility" in PLANNER_SYSTEM and "dalgalanma" in PLANNER_SYSTEM
    assert "sensitivity=high" in PLANNER_SYSTEM


def test_user_facing_strings_are_turkish_and_the_composer_passes_them_on(housing):
    from backend.agent.composer import COMPOSER_SYSTEM
    result = detect_change_points(housing.values, kind="trend")
    assert result["measure"] == "ortalama büyüme, aylık %"
    assert "warnings" in COMPOSER_SYSTEM and "confidence" in COMPOSER_SYSTEM and "recent" in COMPOSER_SYSTEM
