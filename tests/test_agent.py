"""Tests for the agent layer: state, DSL, routing, transforms, execution, verification.

None of these call a language model. That is the point of the architecture --
everything that decides a number is deterministic, so everything that decides a
number is testable. The model's own reliability is measured separately, by
`backend/eval/run_eval.py`, because it is a property of a deployment rather
than of this code.
"""
import duckdb
import pandas as pd
import pytest

from backend.agent.executor import Executor, _normalise_key
from backend.agent.planner import Plan, Step, template_plan
from backend.agent.router import extract_window, route
from backend.agent.state import AnalysisArtifact, ColumnLineage, Session
from backend.agent.verifier import unsupported_numbers, verify
from backend.core.config import DUCKDB_PATH
from backend.tools import transforms as T
from backend.tools.charts import build_chart
from backend.tools.lakehouse import ALLOWED_TABLES, discover, run_sql
from backend.tools.series import load_series

pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")


def needs_lakehouse():
    if not DUCKDB_PATH.exists():
        pytest.skip("run python -m backend.lakehouse.build first")


def synthetic(name="x", n=24, start=100.0, step=5.0, unit="milyon TL", semantics="stock"):
    index = pd.date_range("2021-01-01", periods=n, freq="MS")
    values = pd.Series([start + step * i for i in range(n)], index=index)
    artifact = AnalysisArtifact(title="test")
    artifact.add_column(name, values, ColumnLineage(
        column=name, label=name.title(), source="bulletin", unit=unit,
        temporal_semantics=semantics, key=name, citation={"table": "t", "filters": {}}))
    return artifact


# --- state ------------------------------------------------------------------

def test_adding_a_column_never_shortens_the_table():
    """Turn 3 of the demo says "hic bozmadan". An inner join would silently drop
    the periods the new series happens not to cover."""
    artifact = synthetic("a", n=24)
    shorter = pd.Series([1.0, 2.0], index=pd.to_datetime(["2021-01-01", "2021-02-01"]))
    artifact.add_column("b", shorter, ColumnLineage(
        column="b", label="B", source="macro", unit="endeks", temporal_semantics="index"))
    assert len(artifact.frame) == 24
    assert artifact.frame["a"].notna().all()


def test_summary_reports_units_and_semantics_per_column():
    stats = synthetic("a").summary()["a"]
    assert stats["unit"] == "milyon TL" and stats["temporal_semantics"] == "stock"
    assert stats["first_value"] == 100.0 and stats["n"] == 24


# --- transforms -------------------------------------------------------------

def test_index_to_base_starts_at_100_and_records_its_transform():
    artifact = synthetic("a")
    name = T.index_to_base(artifact, "a")
    assert artifact.frame[name].iloc[0] == 100.0
    assert artifact.lineage[name].unit == "endeks"
    assert "index_to_base" in artifact.lineage[name].transform
    assert artifact.lineage[name].derived_from == ["a"]


def test_indexing_a_percentage_is_refused():
    """Rebasing a rate to 100 produces a number that looks like an index and
    means nothing."""
    artifact = synthetic("r", unit="%", semantics="rate")
    with pytest.raises(ValueError, match="percentage"):
        T.index_to_base(artifact, "r")


def test_deflate_divides_by_the_price_index_and_keeps_the_nominal_column():
    artifact = synthetic("nominal", n=12, start=100.0, step=10.0)
    cpi = pd.Series([100.0 * (1.05 ** i) for i in range(12)], index=artifact.frame.index)
    artifact.add_column("cpi", cpi, ColumnLineage(
        column="cpi", label="CPI", source="macro", unit="endeks", temporal_semantics="index"))
    name = T.deflate(artifact, "nominal", "cpi")
    assert artifact.frame["nominal"].iloc[-1] == 210.0          # untouched
    assert artifact.frame[name].iloc[0] == pytest.approx(100.0)
    assert artifact.frame[name].iloc[-1] < artifact.frame["nominal"].iloc[-1]
    assert artifact.lineage[name].unit == "milyon TL"


def test_ratio_refuses_to_divide_across_units():
    """bin TL and milyon TL both appear in this corpus for the same quantity."""
    artifact = synthetic("a", unit="bin TL")
    artifact.add_column("b", artifact.frame["a"], ColumnLineage(
        column="b", label="B", source="bulletin", unit="milyon TL", temporal_semantics="stock"))
    with pytest.raises(ValueError, match="units differ"):
        T.ratio(artifact, "a", "b")


def test_change_is_refused_on_a_cumulative_series():
    artifact = synthetic("ytd", semantics="cumulative_ytd")
    with pytest.raises(ValueError, match="year-to-date"):
        T.change(artifact, "ytd")


def test_find_periods_answers_the_coincidence_question_arithmetically():
    """Turn 1 asks whether the rate fell in months the loan book did not grow."""
    index = pd.date_range("2021-01-01", periods=5, freq="MS")
    artifact = AnalysisArtifact()
    artifact.add_column("rate", pd.Series([10.0, 9.0, 8.0, 9.0, 7.0], index=index), ColumnLineage(
        column="rate", label="Rate", source="macro", unit="%", temporal_semantics="rate"))
    artifact.add_column("loan", pd.Series([100.0, 101.0, 99.0, 102.0, 98.0], index=index), ColumnLineage(
        column="loan", label="Loan", source="bulletin", unit="milyon TL", temporal_semantics="stock"))
    found = T.find_periods(artifact, "rate", "down", against="loan", against_direction="down")
    assert [p["period"] for p in found["periods"]] == ["2021-03", "2021-05"]


# --- charts -----------------------------------------------------------------

def test_a_chart_puts_a_second_unit_on_a_second_axis():
    artifact = synthetic("tl")
    artifact.add_column("pct", artifact.frame["tl"] / 10, ColumnLineage(
        column="pct", label="Pct", source="macro", unit="%", temporal_semantics="rate"))
    figure = build_chart(artifact, ["tl", "pct"])
    assert len(figure["data"]) == 2
    assert figure["layout"]["yaxis2"]["title"]["text"] == "%"


def test_three_units_in_one_chart_is_refused():
    artifact = synthetic("tl")
    for name, unit in (("pct", "%"), ("idx", "endeks")):
        artifact.add_column(name, artifact.frame["tl"], ColumnLineage(
            column=name, label=name, source="macro", unit=unit, temporal_semantics="index"))
    with pytest.raises(ValueError, match="different units"):
        build_chart(artifact)


# --- router -----------------------------------------------------------------

@pytest.mark.parametrize("question, expected", [
    ("2021-2025 arasindaki veriler", ("2021-01-01", "2025-12-01")),
    ("2024 yilinda ne oldu", ("2024-01-01", "2024-12-01")),
    ("2021-03 ile 2022-06 arasi", ("2021-03-01", "2022-06-01")),
    ("hic tarih yok", (None, None)),
])
def test_window_extraction(question, expected):
    assert extract_window(question) == expected


def test_a_url_in_the_prompt_routes_to_the_url_tool_without_a_model():
    decided = route("https://www.borsaistanbul.com/endeks/xtumy verisini ozetle", client=None)
    assert decided.intent == "url_analysis"
    assert decided.urls == ["https://www.borsaistanbul.com/endeks/xtumy"]


def test_followup_phrasing_only_counts_when_a_table_exists():
    question = "Bu tabloyu hic bozmadan yeni bir sutun ekle"
    assert route(question, has_artifact=True, client=None).intent == "followup"
    assert route(question, has_artifact=False, client=None).intent != "followup"


# --- planner ----------------------------------------------------------------

def test_a_step_missing_its_required_field_is_rejected():
    with pytest.raises(ValueError, match="missing required field"):
        Step(op="fetch_series")
    with pytest.raises(ValueError, match="missing required field"):
        Step(op="transform", operation="deflate")


def test_template_plans_need_no_model():
    assert template_plan("url_analysis", "q", urls=["http://a"]).steps[0].op == "read_url"
    assert template_plan("search", "q").steps[0].op == "search"


def test_the_plan_schema_is_flat_enough_for_guided_decoding():
    """Guided-decoding backends vary in $ref support, so the schema stays to one
    nested definition (Step) and no unions."""
    schema = Plan.model_json_schema()
    assert set(schema.get("$defs", {})) == {"Step"}
    assert not any("anyOf" in str(prop) and "$ref" in str(prop)
                   for prop in schema["properties"].values() if prop.get("type") == "object")


@pytest.mark.parametrize("raw, expected", [
    ("bulletin/tuketici_kredileri/tuketici_kredileri_konut", "tuketici_kredileri_konut"),
    ("macro/TP.KTF12", "TP.KTF12"),
    ("TP.KTF12", "TP.KTF12"),
    ("mevduat_katilim_fonu/b_vadeli_mevduat", "mevduat_katilim_fonu/b_vadeli_mevduat"),
])
def test_key_normalisation_strips_only_a_source_prefix(raw, expected):
    """A parent/child bulletin key legitimately contains a slash; a model
    echoing the whole discovery line does not."""
    assert _normalise_key(raw) == expected


# --- verifier ---------------------------------------------------------------

def test_verification_flags_a_table_mixing_monetary_units():
    artifact = synthetic("a", unit="bin TL")
    artifact.add_column("b", artifact.frame["a"], ColumnLineage(
        column="b", label="B", source="bulletin", unit="milyon TL",
        temporal_semantics="stock", citation={"table": "t"}))
    session = Session(artifact=artifact)
    report = verify(session)
    assert not report["passed"]
    assert any("mixed monetary units" in c["detail"] for c in report["checks"] if not c["passed"])


def test_verification_passes_on_a_well_formed_table():
    assert verify(Session(artifact=synthetic("a")))["passed"]


def test_a_number_the_tools_never_computed_is_flagged():
    allowed = {"series": {"a": {"last_value": 678970.0, "unit": "milyon TL"}}}
    assert unsupported_numbers("Kredi stogu 678.970 milyon TL oldu.", allowed) == []
    assert unsupported_numbers("Kredi stogu 999.123 milyon TL oldu.", allowed) != []


def test_years_and_small_counts_are_not_treated_as_claims():
    assert unsupported_numbers("2021 ve 2025 arasinda 60 ay boyunca", {"series": {}}) == []


# --- lakehouse tools (need a build) -----------------------------------------

def test_run_sql_rejects_writes_and_unlisted_tables():
    needs_lakehouse()
    with pytest.raises(ValueError, match="only SELECT"):
        run_sql("DELETE FROM bulletin_observations")
    with pytest.raises(ValueError, match="not available"):
        run_sql("SELECT * FROM observations")
    assert run_sql("SELECT count(*) AS n FROM bulletin_entities")["rows"][0]["n"] == 519


def test_the_agent_sql_allowlist_excludes_the_second_bddk_vocabulary():
    """`observations` reads the same BDDK table in bin TL under sector keys.
    Letting a model choose between the two vocabularies invites a 1000x error."""
    assert "observations" not in ALLOWED_TABLES
    assert "bulletin_observations" in ALLOWED_TABLES


@pytest.mark.parametrize("query, expected_key", [
    ("konut kredileri", "tuketici_kredileri_konut"),
    ("mortgage interest rate", "TP.KTF12"),
    ("enflasyon", "TP.GENENDEKS.T1"),
    ("house price index", "TP.KFE.TR"),
    ("policy rate", "TP.APIFON4"),
    ("takipteki konut kredileri", "takipteki_konut_kredileri"),
    ("konut satislari", "TP.AKONUTSAT1.KTRTOPLAM"),
    ("mevduat", "mevduat_katilim_fonu"),
])
def test_discovery_ranks_the_right_key_first(query, expected_key):
    """Discovery is the tool everything else depends on: a wrong key here is a
    confidently wrong answer downstream, so the ranking is pinned."""
    needs_lakehouse()
    candidates = discover(query, limit=3)["candidates"]
    assert candidates and candidates[0]["key"] == expected_key, [c["key"] for c in candidates]


def test_discovery_does_not_offer_a_count_when_asked_for_a_loan_amount():
    needs_lakehouse()
    top = discover("konut kredileri tutari", limit=1)["candidates"][0]
    assert top["unit"] != "adet"


# --- series routing ---------------------------------------------------------

def test_load_series_routes_all_three_corpora():
    needs_lakehouse()
    assert load_series("tuketici_kredileri_konut").unit == "milyon TL"
    assert load_series("TP.KTF12", source="macro").unit == "%"
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    key = con.execute("SELECT entity_key FROM weekly_items WHERE retired_on IS NULL LIMIT 1").fetchone()[0]
    con.close()
    assert load_series(key, source="weekly").temporal_semantics == "stock"


def test_a_weekly_series_name_carries_its_table_for_context():
    """entity_key is BDDK's item id, not a parent/child qualified key like the
    bulletin's, so a weekly entity_name alone ("a) Konut") reads as unrelated
    to the near-identical bulletin series it's usually fetched beside in the
    same table -- observed live: a 'konut kredisi' question surfaced both,
    one plainly labelled and one just "a) Konut" with no visible source."""
    needs_lakehouse()
    series = load_series("5690", source="weekly")  # Krediler / a) Konut
    assert series.name == "Krediler / a) Konut"


def test_a_cumulative_series_is_served_as_its_monthly_flow():
    needs_lakehouse()
    flow = load_series("donem_net_kari_zarari", dataset="kar_zarar")
    ytd = load_series("donem_net_kari_zarari", dataset="kar_zarar", cumulative_as="ytd")
    assert flow.value_column == "value_flow" and ytd.value_column == "value"
    # December's YTD is the whole year; its flow is one month of it.
    assert flow.values.loc["2025-12-01"] < ytd.values.loc["2025-12-01"]


def test_a_retired_weekly_item_is_refused_rather_than_double_counted():
    needs_lakehouse()
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    # 3 of the 22 retired items were retired before the corpus window and
    # publish nothing in it; pick one that actually holds data.
    key = con.execute(
        "SELECT i.entity_key FROM weekly_items i JOIN weekly_observations o USING (dataset, entity_key) "
        "WHERE i.retired_on IS NOT NULL GROUP BY 1 HAVING count(*) > 0 LIMIT 1").fetchone()[0]
    con.close()
    with pytest.raises(ValueError, match="retired"):
        load_series(key, source="weekly")
    assert len(load_series(key, source="weekly", include_retired=True).values) > 0


def test_a_series_carries_its_provenance():
    needs_lakehouse()
    citation = load_series("tuketici_kredileri_konut").citation()
    assert citation["table"] == "bulletin_observations"
    assert citation["filters"]["entity_key"] == "tuketici_kredileri_konut"
    assert citation["unit"] == "milyon TL" and citation["temporal_semantics"] == "stock"


def test_causality_result_is_json_serialisable():
    """Regression test: statsmodels' grangercausalitytests keys p_values_by_lag
    by numpy.int64, and json.dumps refuses a non-native-int dict key -- this
    result reaches the API response verbatim via session.facts, so it must
    round-trip through json.dumps, not just look right when printed."""
    import json

    needs_lakehouse()
    plan = Plan(intent="series_analysis", start="2021-01-01", end="2025-12-01", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin",
             dataset="tuketici_kredileri", as_name="konut"),
        Step(op="fetch_series", key="TP.KTF12", source="macro", as_name="faiz"),
        Step(op="analyze", method="causality", column="konut", against="faiz"),
    ])
    session = Executor(Session()).run(plan)
    assert session.audit[-1].ok, session.audit[-1].detail

    result = session.facts["analysis"]["causality:konut"]
    assert type(result["best_lag"]) is int  # not numpy.int64
    assert all(type(lag) is int for lag in result["p_values_by_lag"])
    json.dumps(result)  # raises if a numpy scalar leaked through


# --- executor end to end ----------------------------------------------------

def test_the_reference_scenario_runs_from_a_hand_written_plan():
    """No model involved: the execution layer alone must be able to produce the
    demo table, or no amount of prompting will."""
    needs_lakehouse()
    plan = Plan(intent="series_analysis", start="2021-01-01", end="2025-12-01", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin",
             dataset="tuketici_kredileri", as_name="konut"),
        Step(op="fetch_series", key="TP.KTF12", source="macro", as_name="faiz"),
        Step(op="fetch_series", key="TP.GENENDEKS.T1", source="macro", as_name="tufe"),
        Step(op="transform", operation="deflate", column="konut", other_column="tufe", as_name="konut_reel"),
        Step(op="transform", operation="index_to_base", column="konut", as_name="konut_endeks"),
        Step(op="find_periods", column="faiz", direction="down", against="konut", against_direction="down"),
        Step(op="chart", columns=["konut_endeks", "faiz"]),
    ])
    session = Executor(Session()).run(plan)
    assert all(step.ok for step in session.audit), [s.detail for s in session.audit if not s.ok]
    assert len(session.artifact.frame) == 60
    assert session.artifact.frame["konut"].iloc[0] == 276785.0
    assert session.artifact.frame["faiz"].iloc[0] == pytest.approx(18.388, abs=1e-3)
    assert session.artifact.frame["konut_endeks"].iloc[0] == 100.0
    assert verify(session)["passed"]
    assert len(session.citations) == 3


def test_a_failing_step_costs_a_column_not_the_turn():
    """Demo day runs on unseen inputs; one bad step must not lose the answer."""
    needs_lakehouse()
    plan = Plan(intent="series_analysis", start="2021-01-01", end="2021-12-01", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin", as_name="konut"),
        Step(op="transform", operation="ratio", column="konut", other_column="does_not_exist"),
        Step(op="chart"),
    ])
    session = Executor(Session()).run(plan)
    assert [step.ok for step in session.audit] == [True, False, True]
    assert "konut" in session.artifact.column_names()


def test_a_hallucinated_key_is_resolved_by_discovery():
    """The most common plan defect: the model writes TP.TUFE where the corpus
    publishes TP.GENENDEKS.T1."""
    needs_lakehouse()
    plan = Plan(intent="series_analysis", start="2021-01-01", end="2021-12-01", steps=[
        Step(op="fetch_series", key="TP.TUFE", source="macro", as_name="enflasyon"),
    ])
    session = Executor(Session()).run(plan)
    assert session.audit[0].ok
    assert "resolved to" in session.audit[0].detail
    assert len(session.artifact.frame) == 12
