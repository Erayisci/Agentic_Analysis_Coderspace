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
from backend.tools.lakehouse import ALLOWED_TABLES, discover, discover_concepts, run_sql
from backend.tools.series import SeriesResult, load_series

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
    with pytest.raises(ValueError, match="missing required field"):
        Step(op="ingest_external", url="https://example.com/x.xlsx")  # no value_column


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
    ("finturk/bireysel_bankacilik/konut_kredisi", "konut_kredisi"),
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
    # Regression: "Diger Mevduat" (Other Deposits) is a FinTurk sibling of
    # "Toplam Mevduat" (Total Deposits) close enough in every other word that,
    # with "toplam" itself stopped as boilerplate, it outranked the row this
    # question actually asked for by 0.02 points -- see the "diger" QUALIFIERS
    # entry, added instead of un-stopping "toplam" (that fixed this case but
    # broke "toplam konut kredilerinin dagilimini" the other way).
    ("Ankara'da toplam mevduat hacmi ne kadar?", "toplam_mevduat"),
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


def test_discovery_seeds_the_amount_series_beside_a_rate_word_in_one_clause():
    """"Faiz orani konut kredisi HACMINI ongormeye yardimci oluyor mu?" names
    a rate (faiz orani) and an amount (konut kredisi hacmi) in one clause --
    no "ve"/"ile" for discover_concepts to split on, so both compete in the
    same ranked pool. RATIO_WORDS' blanket +3%/-3TL bonus (correct for the
    rate half) buries the amount series under a wall of "KTF"-aliased rate
    series that also happen to be named "Konut Kredisi": measured live, the
    milyon-TL housing-loan stock did not reach the top 20, so the model chose
    a second rate series for "hacim" and ran a causality test between two
    rates instead of rate-vs-amount, a convincingly-worded wrong answer.
    AMOUNT_WORDS' seat guarantee (mirroring `discover_concepts`' per-clause
    seat, applied to a semantic role split within one clause) is what fixes
    it -- not by reweighting the shared ranking, which would just as easily
    demote the correct rate series sitting in the same query."""
    needs_lakehouse()
    candidates = discover_concepts(
        "Faiz oranı konut kredisi hacmini öngörmeye yardımcı oluyor mu?", limit=8)["candidates"]
    by_key = {c["key"]: c for c in candidates}
    assert "TP.KTF12" in by_key, "the rate half must still be offered"
    assert by_key["TP.KTF12"]["unit"] == "%"
    assert "tuketici_kredileri_konut" in by_key, (
        "the amount half must be seeded even though it never beats the KTF-aliased rate series on score")
    volume = by_key["tuketici_kredileri_konut"]
    assert volume["unit"] == "milyon TL"
    assert volume["temporal_semantics"] == "stock"


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

    result = session.facts["analysis"]["causality:konut~faiz"]
    assert type(result["lag"]) is int  # not numpy.int64
    assert all(type(lag) is int for d in result["directions"].values() for lag in d["p_values_by_lag"])
    assert set(result["directions"]) == {"faiz->konut", "konut->faiz"}
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


def _mock_excel_fetch(monkeypatch, frame: pd.DataFrame):
    """Same mocking approach as tests/test_external_series.py: fake
    web_url._fetch rather than hitting the network."""
    from io import BytesIO
    from types import SimpleNamespace

    from backend.tools import web_url

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Sheet1", index=False)
    response = SimpleNamespace(
        headers={"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        content=buf.getvalue())
    monkeypatch.setattr(web_url, "_fetch", lambda url: response)


def test_ingest_external_adds_a_column_scoped_to_this_session_only(monkeypatch):
    """The op reads an external file into a real column -- with source
    'external' and unit_verified=False in its citation, since (unlike
    bulletin/weekly/macro) nothing here labels its own unit -- and touches
    nothing under data/, which is the whole point of not writing to the
    lakehouse for an unseen, demo-day file."""
    frame = pd.DataFrame({
        "Tarih": pd.date_range("2021-01-01", periods=6, freq="MS"),
        "Altin (USD)": [1800.0, 1810.0, 1795.0, 1820.0, 1830.0, 1825.0],
    })
    _mock_excel_fetch(monkeypatch, frame)

    plan = Plan(intent="url_analysis", steps=[
        Step(op="ingest_external", url="https://example.com/altin.xlsx",
             value_column="Altin (USD)", as_name="altin"),
    ])
    session = Executor(Session()).run(plan)

    assert session.audit[0].ok
    assert "altin" in session.artifact.column_names()
    assert len(session.artifact.frame) == 6
    lineage = session.artifact.lineage["altin"]
    assert lineage.source == "external"
    assert lineage.citation["table"] == "external"
    assert lineage.citation["unit_verified"] is False
    assert session.citations and session.citations[0]["table"] == "external"


def test_an_ingested_external_column_works_with_transform_analyze_and_chart_unmodified(monkeypatch):
    """The point of the design: AnalysisArtifact.add_column does not care
    where a column came from, so an external column needs no special-casing
    anywhere else -- it can be indexed, analyzed and charted like any other.

    anomaly is the sharp case: it normally re-fetches full history from the
    lakehouse by key, and an external column has no lakehouse row to fetch --
    see test_anomaly_on_a_derived_column_scores_the_artifacts_own_values for
    the bug this guards (it broke identically for transform-derived columns,
    on real lakehouse data, with no external ingestion involved at all)."""
    values = [1800.0 + i * 5 for i in range(24)]
    values[15] = 3000.0  # an injected spike for anomaly to actually find
    frame = pd.DataFrame({
        "Tarih": pd.date_range("2021-01-01", periods=24, freq="MS"),
        "Altin (USD)": values,
    })
    _mock_excel_fetch(monkeypatch, frame)

    plan = Plan(intent="url_analysis", steps=[
        Step(op="ingest_external", url="https://example.com/altin.xlsx",
             value_column="Altin (USD)", as_name="altin"),
        Step(op="transform", operation="index_to_base", column="altin", base_period="2021-01-01"),
        Step(op="analyze", method="anomaly", column="altin", window=6),
        Step(op="chart"),
    ])
    session = Executor(Session()).run(plan)

    assert all(step.ok for step in session.audit), [s.detail for s in session.audit if not s.ok]
    index_col = [c for c in session.artifact.column_names() if c != "altin"][0]
    assert session.artifact.frame[index_col].iloc[0] == 100.0
    assert "figure" in session.facts
    anomaly_result = session.facts["analysis"]["anomaly:altin"]
    assert anomaly_result["n_anomalies"] >= 1
    assert anomaly_result["source"] == "external"


def test_anomaly_on_a_derived_column_scores_the_artifacts_own_values():
    """Regression test for a bug found independently of ingest_external:
    index_to_base/deflate/change all copy the ORIGINAL column's lineage.key
    (so it looks fetchable) but set source="derived" -- and load_series only
    accepts bulletin/weekly/macro, so analyze(anomaly) on any indexed,
    deflated or MoM/YoY-changed column crashed with 'source must be one of
    (...)', on real lakehouse data, no external ingestion needed to hit it."""
    needs_lakehouse()
    plan = Plan(intent="series_analysis", start="2021-01-01", end="2025-12-01", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin",
             dataset="tuketici_kredileri", as_name="konut"),
        Step(op="transform", operation="index_to_base", column="konut", base_period="2021-01-01"),
        Step(op="analyze", method="anomaly", column="konut_endeks"),
    ])
    session = Executor(Session()).run(plan)

    assert all(step.ok for step in session.audit), [s.detail for s in session.audit if not s.ok]
    result = session.facts["analysis"]["anomaly:konut_endeks"]
    assert result["source"] == "derived"
    assert "n_anomalies" in result


def test_ingest_external_bad_column_costs_a_step_not_the_turn(monkeypatch):
    """A bad column name records a failed step and stops there cleanly -- it
    must not raise out of Executor.run and abort the rest of the turn."""
    frame = pd.DataFrame({
        "Tarih": pd.date_range("2021-01-01", periods=6, freq="MS"),
        "Altin (USD)": [1800.0] * 6,
    })
    _mock_excel_fetch(monkeypatch, frame)

    plan = Plan(intent="url_analysis", steps=[
        Step(op="ingest_external", url="https://example.com/altin.xlsx", value_column="Does Not Exist"),
    ])
    session = Executor(Session()).run(plan)

    assert [step.ok for step in session.audit] == [False]
    assert "not found" in session.audit[0].detail
    assert session.artifact.is_empty()


def test_clear_table_empties_the_artifact_and_its_citations():
    """The only op whose job is to discard state -- must empty the session's
    in-memory table and nothing else (no lakehouse connection is even opened
    by this step, since it never touches anything under data/)."""
    artifact = synthetic("a", n=12)
    session = Session(artifact=artifact)
    session.cite({"table": "t", "filters": {}})
    assert not session.artifact.is_empty()
    assert session.citations

    plan = Plan(intent="metadata", steps=[Step(op="clear_table")])
    Executor(session).run(plan)

    assert session.audit[0].ok
    assert session.artifact.is_empty()
    assert session.citations == []


def test_clear_table_needs_no_fields():
    Step(op="clear_table")  # must not raise -- REQUIRED["clear_table"] is empty


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


# --- presentation: a chart or table only when asked ------------------------

@pytest.mark.parametrize("question, chart, table", [
    ("2021-2025 arasinda konut kredileri nasil degisti?", False, False),
    ("konut kredilerini grafik olarak ciz", True, False),
    ("konut kredilerini tablo halinde ver", False, True),
    ("plot housing loans against the policy rate", True, False),
    # "göster" is the display verb here...
    ("konut kredilerini aylik olarak gosteriniz", False, True),
    # ...and the verb "exhibit" here, which asks for prose, not a table.
    ("faiz dustugunde kredi miktari nasil degisim gostermis?", False, False),
    ("kredi hacmi 2023'te guclu bir artis gosterdi mi?", False, False),
])
def test_the_router_reads_chart_and_table_requests_from_the_question(question, chart, table):
    decided = route(question, client=None)
    assert (decided.wants_chart, decided.wants_table) == (chart, table)


def test_a_followup_keeps_the_table_it_extends():
    decided = route("Bu tabloyu hic bozmadan yeni bir sutun ekle", has_artifact=True, client=None)
    assert decided.wants_table


def test_a_chart_step_survives_only_when_the_question_asked_for_one():
    """Every plan source -- model, deterministic, template -- passes through
    the same gate, so a model that charts unasked is corrected, and a
    question that asks is charted even when the plan forgot."""
    from backend.agent.pipeline import apply_presentation

    def plan_with_chart():
        return Plan(intent="series_analysis", steps=[
            Step(op="fetch_series", key="k", source="bulletin"), Step(op="chart")])

    unasked = apply_presentation(plan_with_chart(), route("konut kredileri", client=None), "q")
    assert [s.op for s in unasked.steps] == ["fetch_series"]

    asked = apply_presentation(
        Plan(intent="series_analysis", steps=[Step(op="fetch_series", key="k", source="bulletin")]),
        route("konut kredilerini ciz", client=None), "q")
    assert [s.op for s in asked.steps] == ["fetch_series", "chart"]

    # Nothing to draw over: a metadata plan never gains a chart.
    nothing = apply_presentation(template_plan("metadata", "q"), route("listeyi ciz", client=None), "q")
    assert [s.op for s in nothing.steps] == ["discover"]


def test_a_turn_shows_the_columns_it_touched_not_everything_the_session_holds():
    """The artifact accumulates across turns by design; a turn's table must not.

    Measured on the demo conversation: question 2 ("konut kredilerini aylik
    goster") arrived with question 1's NPL and commercial-rate columns beside
    its own, and carried a coverage caveat about them -- those series simply
    span a different window. The artifact still holds them; the turn does not
    show them.
    """
    session = Session()
    session.artifact = synthetic("npl", unit="%", semantics="rate")
    konut = session.artifact.frame["npl"] * 1000
    session.artifact.add_column("konut", konut, ColumnLineage(
        column="konut", label="Konut", source="bulletin", unit="milyon TL",
        temporal_semantics="stock", key="konut", citation={"table": "t", "filters": {}}))

    session.start_turn("konut kredilerini goster")
    session.touch_column("konut")
    assert session.focus() == ["konut"]
    assert session.view().column_names() == ["konut"]
    assert session.artifact.column_names() == ["npl", "konut"]          # nothing was dropped

    # A derived column brings its input along -- a table that cannot explain
    # its own numbers is what the verifier checks for.
    session.start_turn("bu tabloyu bozmadan reel hale getir")
    T.index_to_base(session.artifact, "konut", None, "konut_endeks")
    session.touch_column("konut_endeks")
    assert session.focus(keep_previous=True) == ["konut", "konut_endeks"]

    # A follow-up that touches nothing keeps the table the user is looking
    # at; a NEW question that touches nothing shows no table at all (its
    # answer is not the previous question's numbers), but remembers the last
    # table for the next follow-up.
    session.start_turn("bu tabloyu bozmadan aynen goster")
    assert session.focus(keep_previous=True) == ["konut", "konut_endeks"]
    session.start_turn("altin fiyatlarini goster")
    assert session.focus() == [] and session.view().is_empty()
    assert session.visible_columns == ["konut", "konut_endeks"]


def test_the_planner_is_shown_the_table_on_screen_not_the_whole_session(monkeypatch):
    """On a follow-up, a column an earlier question fetched is named as
    leftover, not offered as part of the current table -- the planner copies
    what it is shown. On a fresh question, no column is the current table:
    shown the previous question's `konut`, the model reused it instead of
    fetching the line the new question was about."""
    from backend.agent import pipeline

    monkeypatch.setattr(pipeline, "discover_concepts",
                        lambda *a, **k: {"candidates": [], "by_concept": []})
    session = Session()
    session.artifact = synthetic("npl", unit="%", semantics="rate")
    session.artifact.add_column("konut", session.artifact.frame["npl"] * 1000, ColumnLineage(
        column="konut", label="Konut", source="bulletin", unit="milyon TL",
        temporal_semantics="stock", key="konut", citation={"table": "t", "filters": {}}))
    session.visible_columns = ["konut"]

    followup = route("bu tabloyu bozmadan faizi ekle", has_artifact=True, client=None)
    assert followup.is_followup
    context = pipeline.build_context("soru", session, followup)
    current = context.split("ONCEKI SORULARDAN KALAN")[0]
    assert "konut" in current and "npl" not in current
    assert "npl" in context.split("ONCEKI SORULARDAN KALAN")[1]

    fresh = route("konut kredileri", has_artifact=True, client=None)
    assert not fresh.is_followup
    context = pipeline.build_context("soru", session, fresh)
    assert "MEVCUT TABLO: bos" in context and "YENI bir soru" in context
    assert "MEVCUT TABLO (" not in context


def test_replacing_a_column_keeps_the_new_series_whole():
    """A monthly series written over a quarterly column of the same name
    keeps all its months -- `assign` aligned it onto the old index and
    silently kept only the quarter-ends."""
    quarterly = pd.Series([1.0, 2.0, 3.0], index=pd.to_datetime(["2021-03-01", "2021-06-01", "2021-09-01"]))
    artifact = AnalysisArtifact(title="t")
    line = ColumnLineage(column="konut", label="Konut", source="finturk", unit="bin TL",
                         temporal_semantics="stock", key="konut_kredisi", citation={"table": "f"})
    artifact.add_column("konut", quarterly, line)
    monthly = pd.Series(range(12), index=pd.date_range("2021-01-01", periods=12, freq="MS"), dtype=float)
    artifact.add_column("konut", monthly, ColumnLineage(
        column="konut", label="Konut", source="bulletin", unit="milyon TL",
        temporal_semantics="stock", key="tuketici_kredileri_konut", citation={"table": "b"}))
    assert artifact.frame["konut"].notna().sum() == 12
    assert artifact.lineage["konut"].source == "bulletin"


def _stale_istanbul_session() -> Session:
    """A session whose one column is the previous question's: Istanbul's
    housing loans from FinTurk, quarterly, named `konut`."""
    session = Session()
    session.start_turn("İstanbul'daki konut kredilerini il bazinda goster")
    Executor(session).run(Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="konut_kredisi", source="finturk", dataset="bireysel_bankacilik",
             province="İSTANBUL", as_name="konut")]))
    session.focus()
    assert session.artifact.lineage["konut"].source == "finturk"
    return session


DEMO_HOUSING_QUESTION = ("2021-2025 yillari arasindaki 60 aylik veriden Turkiye'de kullanilan toplam konut "
                         "kredilerinin dagilimini aylik olarak gosteriniz. Buna ek olarak konut kredisi faiz "
                         "oranlarini da gosteriniz. Faiz orani dustugu halde kredi miktarinin yukselmedigi "
                         "donemler olmus mu?")


def test_a_fresh_question_never_plans_over_a_previous_questions_column():
    """Measured live: after an Istanbul FinTurk question, the demo's national
    housing-loan question came back planned as `fetch TP.KTF12; find_periods
    faiz against konut` -- the previous turn's province column standing in
    for the national line, 22 quarterly points under a 60-month answer. The
    plan of a question the router did not read as a follow-up references
    only what it fetches itself; a reference to a stale column becomes the
    fetch the question's own discovery offered for it."""
    needs_lakehouse()
    from backend.agent.pipeline import make_plan

    session = _stale_istanbul_session()
    session.start_turn(DEMO_HOUSING_QUESTION)
    route_result = route(DEMO_HOUSING_QUESTION, has_artifact=True, client=None)
    assert not route_result.is_followup
    model_plan = Plan(intent="series_analysis", start="2021-01-01", end="2025-12-01", steps=[
        Step(op="fetch_series", key="TP.KTF12", source="macro", as_name="faiz"),
        Step(op="find_periods", column="faiz", direction="down", against="konut", against_direction="down")])
    plan = make_plan(DEMO_HOUSING_QUESTION, session, route_result, _StubPlanClient(model_plan))

    fetched = {(s.source, s.key): s for s in plan.steps if s.op == "fetch_series"}
    assert ("bulletin", "tuketici_kredileri_konut") in fetched
    assert not any(s.source == "finturk" for s in plan.steps)
    assert fetched[("bulletin", "tuketici_kredileri_konut")].as_name == "konut"
    assert [s.op for s in plan.steps] == ["fetch_series", "fetch_series", "find_periods"]
    assert "previous question's column" in plan.reasoning

    Executor(session).run(plan)
    assert all(a.ok for a in session.audit), [a.detail for a in session.audit if not a.ok]
    session.focus(keep_previous=route_result.is_followup)
    assert session.artifact.lineage["konut"].source == "bulletin"
    assert session.artifact.frame["konut"].notna().sum() == 60
    assert session.facts["find_periods"][0]["n_periods"] > 0
    # The panel's sources are this turn's, not the conversation's.
    assert {c["table"] for c in session.turn_citations()} == {"bulletin_observations", "macro_observations"}
    assert any(c["table"] == "finturk_observations" for c in session.citations)     # the session still knows


def test_a_fresh_question_leaves_a_reference_alone_when_discovery_offers_nothing_for_it(monkeypatch):
    """The one legitimate reach into an old column: the question named it
    and offered no series of its own for that slot."""
    from backend.agent.pipeline import apply_scope

    session = Session(artifact=synthetic("npl", unit="%", semantics="rate"))
    plan = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="TP.KTF12", source="macro", as_name="faiz"),
        Step(op="analyze", method="causality", column="faiz", against="npl")])
    fresh = route("faiz npl'yi onculuyor mu", has_artifact=True, client=None)
    scoped = apply_scope(plan, fresh, session, {"candidates": [], "by_concept": []})
    assert [s.op for s in scoped.steps] == ["fetch_series", "analyze"]
    assert scoped.steps[1].against == "npl"

    # A follow-up is never touched.
    followup = route("bu tabloyu bozmadan", has_artifact=True, client=None)
    plan = Plan(intent="followup", steps=[Step(op="find_periods", column="npl", direction="down")])
    assert apply_scope(plan, followup, session, {"candidates": [], "by_concept": []}).steps == plan.steps


def test_an_automatic_partner_column_is_one_this_turn_fetched():
    """`against` left blank on a fresh question is filled from the turn's own
    columns before a previous question's."""
    session = Session(artifact=synthetic("stale", unit="%", semantics="rate"))
    session.start_turn("q")
    monthly = session.artifact.frame["stale"]
    for name in ("a", "b"):
        session.artifact.add_column(name, monthly * 2, ColumnLineage(
            column=name, label=name, source="macro", unit="%", temporal_semantics="rate",
            key=name, citation={"table": "t"}))
        session.touch_column(name)
    step = Step(op="analyze", method="causality", column="a")
    against, auto = Executor(session)._second_column(step, "a")
    assert (against, auto) == ("b", True)


def test_a_new_questions_payload_cites_only_its_own_sources():
    needs_lakehouse()
    from backend.agent.pipeline import Agent
    agent = Agent()
    first = agent.ask("İstanbul'daki konut kredilerini il bazinda goster", session_id="cite")
    assert {c["table"] for c in first["citations"]} == {"finturk_observations"}
    second = agent.ask(DEMO_HOUSING_QUESTION, session_id="cite")
    assert "finturk_observations" not in {c["table"] for c in second["citations"]}
    assert {c["table"] for c in second["citations"]} <= {"bulletin_observations", "macro_observations"}
    assert not any(line.source == "finturk" for line in agent.session("cite").view().lineage.values())


def test_a_new_question_does_not_drag_the_previous_questions_columns_into_its_table():
    needs_lakehouse()
    from backend.agent.pipeline import Agent
    agent = Agent()
    first = agent.ask("2021-2024 doneminde takipteki alacaklar orani ile ticari kredi faizleri "
                      "arasindaki iliskiyi tablo halinde goster", session_id="scope")
    second = agent.ask("2021-2025 arasinda konut kredilerini ve konut kredisi faiz oranlarini "
                       "aylik tablo olarak goster", session_id="scope")
    assert first["table"]["columns"] and second["table"]["columns"]
    assert set(first["table"]["columns"]).isdisjoint(second["table"]["columns"])
    # The session kept them; this turn's table did not show them.
    assert set(first["table"]["columns"]) < set(second["table"]["all_columns"])
    assert len(second["table"]["rows"]) == 60                    # 2021-01..2025-12, not the union

    third = agent.ask("Bu tabloyu bozmadan yeni bir sutun ekle", session_id="scope")
    assert set(second["table"]["columns"]) <= set(third["table"]["columns"])
    assert set(first["table"]["columns"]).isdisjoint(third["table"]["columns"])


def test_a_turn_reports_stage_timings_and_withholds_an_unrequested_figure():
    needs_lakehouse()
    from backend.agent.pipeline import Agent
    agent = Agent()
    result = agent.ask("2021-2025 arasinda konut kredileri nasil degisti?", session_id="t")
    assert set(result["timings"]) == {"route", "plan", "execute", "verify", "compose", "total"}
    assert result["presentation"] == {"table": False, "chart": False}
    assert result["figure"] is None
    assert result["session"].facts["presentation"] == {"tablo": False, "grafik": False}

    result = agent.ask("Bu tabloyu bozmadan grafik olarak ciz", session_id="t")
    assert result["presentation"] == {"table": True, "chart": True}
    assert result["figure"] is not None
    assert result["plan"]["steps"][-1]["op"] == "chart"


def test_an_invalid_step_is_dropped_rather_than_failing_the_plan():
    """The model writes `transform` with `method` and no `operation`; that
    costs one step and a note in `reasoning`, not a repair round."""
    plan = Plan.model_validate({"intent": "series_analysis", "steps": [
        {"op": "fetch_series", "key": "TP.KTF12", "source": "macro", "as_name": "faiz"},
        {"op": "transform", "method": "changepoint", "title": "kirilma"},
        {"op": "analyze", "method": "causality"},
        {"op": "find_periods", "column": "faiz", "direction": "down"},
    ]})
    # The analyze step with no column is not dropped: the fetch before it
    # put "faiz" on the table, and `_fill_analyze_columns` takes that.
    assert [s.op for s in plan.steps] == ["fetch_series", "analyze", "find_periods"]
    assert plan.steps[1].column == "faiz"
    assert "dropped invalid step(s)" in plan.reasoning
    assert "transform" in plan.reasoning and "analyze" not in plan.reasoning

    with pytest.raises(ValueError, match="at least one step"):
        Plan.model_validate({"intent": "series_analysis", "steps": [{"op": "analyze"}]})


def test_a_year_range_is_not_read_as_a_negative_number():
    from backend.agent.verifier import numbers_in_text
    assert numbers_in_text("2021-2025 arasinda 678.970 milyon TL, -3,5 puan") == [2021.0, 2025.0, 678970.0, -3.5]
    assert unsupported_numbers("2021-2025 doneminde %145,31 artis", {"x": 145.31}) == []


def test_find_periods_says_what_its_list_means():
    """The composer reads the dict, so the dict must name the list: the months
    where the rate fell *and* loans did not rise, out of all rate-fall months."""
    artifact = AnalysisArtifact()
    periods = pd.date_range("2021-01-01", periods=6, freq="MS")
    artifact.add_column("rate", pd.Series([10, 9, 8, 8.5, 7, 6], index=periods),
                        ColumnLineage(column="rate", label="r", unit="%", temporal_semantics="rate", source="macro"))
    artifact.add_column("loan", pd.Series([100, 110, 105, 108, 120, 115], index=periods),
                        ColumnLineage(column="loan", label="l", unit="milyon TL", temporal_semantics="stock", source="bulletin"))
    found = T.find_periods(artifact, "rate", "down", against="loan", against_direction="down")
    assert found["n_column_moves"] == 4          # 02, 03, 05, 06
    assert found["n_periods"] == 2               # 03 and 06: loans fell too
    assert [p["period"] for p in found["periods"]] == ["2021-03", "2021-06"]
    assert "4 ayin icinde" in found["description"] and "yukselmedigi" in found["description"]


def test_a_rate_column_reports_its_change_in_points_not_percent():
    artifact = AnalysisArtifact()
    periods = pd.date_range("2021-01-01", periods=3, freq="MS")
    artifact.add_column("rate", pd.Series([18.4, 30.0, 37.3], index=periods),
                        ColumnLineage(column="rate", label="r", unit="%", temporal_semantics="rate", source="macro"))
    artifact.add_column("loan", pd.Series([100.0, 150.0, 200.0], index=periods),
                        ColumnLineage(column="loan", label="l", unit="milyon TL", temporal_semantics="stock", source="bulletin"))
    stats = artifact.summary()
    assert stats["rate"]["change_points"] == pytest.approx(18.9) and "change_pct" not in stats["rate"]
    assert stats["loan"]["change_pct"] == 100.0 and "change_points" not in stats["loan"]


# --- sources: every bracket in the answer resolves to a checkable line -------

def test_source_map_tags_series_and_computations_and_gives_runnable_sql():
    needs_lakehouse()
    from backend.agent.verifier import attach_sources, source_map
    plan = Plan(intent="series_analysis", start="2021-01-01", end="2025-12-01", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin",
             dataset="tuketici_kredileri", as_name="konut"),
        Step(op="fetch_series", key="TP.KTF12", source="macro", as_name="faiz"),
        Step(op="transform", operation="index_to_base", column="konut", as_name="konut_endeks"),
        Step(op="find_periods", column="faiz", direction="down", against="konut", against_direction="down"),
    ])
    session = Executor(Session()).run(plan)
    sources = source_map(session)
    assert list(sources) == ["K1", "K2", "H1", "H2"]
    assert sources["K1"]["column"] == "konut" and sources["K2"]["column"] == "faiz"
    assert sources["H1"]["inputs"] == ["K1"]                      # the index over konut
    assert sources["H2"]["inputs"] == ["K2", "K1"] and sources["H2"]["periods"]
    assert session.facts["find_periods"][0]["kaynak"] == "H2"

    # The SQL reproduces the cited column exactly.
    rows = run_sql(sources["K1"]["sql"])["rows"]
    assert len(rows) == 60 and rows[0]["value"] == 276785.0
    rows = run_sql(sources["K2"]["sql"])["rows"]
    assert len(rows) == 60 and rows[0]["value"] == pytest.approx(18.388, abs=1e-3)

    text = attach_sources("Stok 678.970 milyon TL [K1]; 4 ay [H2]; uydurma [K9].", sources)
    assert "[K9]" not in text
    assert "[K1] bulletin_observations" in text and "SQL: SELECT" in text
    assert "[H2] find_periods" in text and "[H1]" not in text.split("Kaynaklar:")[1]


def test_quotable_numbers_carry_the_source_tag():
    from backend.agent.verifier import quotable_numbers
    session = Session()
    session.artifact = synthetic("x")
    facts = quotable_numbers(session)
    assert facts["series"]["x"]["kaynak"] == "K1"


def test_a_phrase_with_a_dash_before_a_year_is_not_a_negative_number():
    from backend.agent.verifier import numbers_in_text
    assert numbers_in_text("2023 sonu-2024 başı, %17,99") == [2023.0, 2024.0, 17.99]


# --- analysis intent: the question's words pick the tool ---------------------

@pytest.mark.parametrize("question, expected", [
    ("Konut kredilerinde olagandisi hareketler var mi? Anomali analizi yap.", ["anomaly"]),
    ("Konut kredisi faiz oranlarinda rejim degisikligi oldugu donemleri bul.", ["changepoint"]),
    ("Faiz konut kredilerini onculuyor mu? Nedensellik testi yap.", ["causality"]),
    ("Faiz dustugu halde kredilerin artmamasinin sebebi fiyat artisi olabilir mi?", ["decompose"]),
    ("Mevduattaki dusus neden oldu?", ["causality"]),
    ("2021-2025 konut kredileri nasil degisim gostermis, yukselmedigi donemler olmus mu?", []),
    ("Are there structural breaks or outliers in deposits?", ["anomaly", "changepoint"]),
])
def test_the_router_reads_analysis_intent_from_the_question(question, expected):
    assert route(question, client=None).wants_analysis == expected


def test_an_analysis_question_is_not_routed_to_metadata_and_skips_the_classifier():
    class Boom:
        def structured(self, *a, **k):
            raise AssertionError("classifier must not be called")
    decided = route("Konut serilerini listele ve anomali analizi yap", client=Boom())
    assert decided.intent == "series_analysis" and decided.wants_analysis == ["anomaly"]
    assert decided.decided_by == "rules"


def _two_series_plan(**extra):
    return Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin",
             dataset="tuketici_kredileri", as_name="konut"),
        Step(op="fetch_series", key="TP.KTF12", source="macro", as_name="faiz"),
        *extra.get("steps", [])])


def test_an_analyze_step_is_appended_when_the_question_asked_and_the_model_omitted_it():
    from backend.agent.pipeline import apply_analysis
    plan = apply_analysis(_two_series_plan(), route("anomali var mi", client=None), Session())
    assert [(s.op, s.method, s.column) for s in plan.steps][-1] == ("analyze", "anomaly", "konut")

    plan = apply_analysis(_two_series_plan(), route("faiz krediyi onculuyor mu", client=None), Session())
    last = plan.steps[-1]
    assert (last.method, last.column, last.against) == ("causality", "konut", "faiz")


def test_an_analyze_step_the_model_wrote_is_not_duplicated():
    from backend.agent.pipeline import apply_analysis
    written = Step(op="analyze", method="anomaly", column="faiz", window=6)
    plan = apply_analysis(_two_series_plan(steps=[written]), route("anomali var mi", client=None), Session())
    assert [s for s in plan.steps if s.op == "analyze"] == [written]


def test_decompose_appends_a_cpi_fetch_when_no_price_index_is_planned():
    from backend.agent.pipeline import apply_analysis
    plan = apply_analysis(_two_series_plan(), route("artmamasinin sebebi fiyat olabilir mi", client=None), Session())
    ops = [(s.op, s.key or s.method) for s in plan.steps]
    assert ("fetch_series", "TP.GENENDEKS.T1") in ops
    last = plan.steps[-1]
    assert (last.method, last.column, last.against) == ("decompose", "konut", "tufe")
    assert "fetched as deflator" in plan.reasoning


def test_decompose_uses_the_price_index_already_in_the_plan():
    from backend.agent.pipeline import apply_analysis
    plan = _two_series_plan(steps=[Step(op="fetch_series", key="TP.KFE.TR", source="macro", as_name="kfe")])
    plan = apply_analysis(plan, route("sebebi fiyat artisi olabilir mi", client=None), Session())
    last = plan.steps[-1]
    assert (last.method, last.column, last.against) == ("decompose", "konut", "kfe")
    assert not any(s.key == "TP.GENENDEKS.T1" for s in plan.steps)


def test_causality_is_skipped_when_only_one_column_is_planned():
    from backend.agent.pipeline import apply_analysis
    plan = Plan(intent="series_analysis", steps=[Step(op="fetch_series", key="TP.KTF12", source="macro")])
    plan = apply_analysis(plan, route("nedensellik", client=None), Session())
    assert all(s.op != "analyze" for s in plan.steps)


def test_a_step_rejects_unknown_fields():
    with pytest.raises(ValueError):
        Step(op="analyze", method="anomaly", column="x", z_threshold=2.0)


def test_causality_without_against_falls_back_to_the_other_column():
    needs_lakehouse()
    plan = Plan(intent="series_analysis", start="2021-01-01", end="2025-12-01", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin",
             dataset="tuketici_kredileri", as_name="konut"),
        Step(op="fetch_series", key="TP.KTF12", source="macro", as_name="faiz"),
        Step(op="analyze", method="causality", column="konut"),
    ])
    session = Executor(Session()).run(plan)
    assert session.audit[-1].ok, session.audit[-1].detail
    assert "against chosen automatically" in session.audit[-1].detail
    result = session.facts["analysis"]["causality:konut~faiz"]
    assert result["against_auto"] and result["inputs"] == ["konut", "faiz"]


def test_the_demo_decomposition_ties_the_three_numbers_together():
    """The real failure: nominal +145%, KFE +1139%, real -36% reached the
    composer as three unrelated facts. One fact now states the relation."""
    needs_lakehouse()
    plan = Plan(intent="series_analysis", start="2021-01-01", end="2025-12-01", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin",
             dataset="tuketici_kredileri", as_name="konut"),
        Step(op="fetch_series", key="TP.KFE.TR", source="macro", as_name="kfe"),
        Step(op="analyze", method="decompose", column="konut", against="kfe"),
    ])
    session = Executor(Session()).run(plan)
    assert session.audit[-1].ok, session.audit[-1].detail
    result = session.facts["analysis"]["decompose:konut~kfe"]
    assert result["nominal_pct"] == pytest.approx(145.3, abs=0.2)
    assert result["price_pct"] == pytest.approx(1139.3, abs=1.0)
    assert result["real_pct"] == pytest.approx(-80.2, abs=0.3)
    assert "tutarli" in result["description"]


def test_anomaly_refetch_keeps_the_columns_currency_and_metric(monkeypatch):
    """A TL-only column re-scored on the total series is a different series."""
    needs_lakehouse()
    from backend.agent import executor as ex
    captured = {}
    real = ex.load_series

    def spy(key, **kwargs):
        captured.update(kwargs)
        return real(key, **kwargs)
    monkeypatch.setattr(ex, "load_series", spy)
    plan = Plan(intent="series_analysis", start="2024-01-01", end="2025-12-01", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin",
             dataset="tuketici_kredileri", currency="TL", as_name="konut_tl"),
        Step(op="analyze", method="anomaly", column="konut_tl"),
    ])
    session = Executor(Session()).run(plan)
    assert session.audit[-1].ok, session.audit[-1].detail
    assert captured["currency"] == "TL" and captured["dataset"] == "tuketici_kredileri"
    result = session.facts["analysis"]["anomaly:konut_tl"]
    assert result["period_start"] == "2021-01"       # full history, not the 24-month window


def test_a_weekly_column_is_analysed_at_the_monthly_grain():
    needs_lakehouse()
    plan = Plan(intent="series_analysis", start="2023-01-01", end="2025-12-01", steps=[
        Step(op="fetch_series", key="5690", source="weekly", as_name="haftalik"),
        Step(op="analyze", method="anomaly", column="haftalik"),
    ])
    session = Executor(Session()).run(plan)
    assert session.audit[-1].ok, session.audit[-1].detail
    result = session.facts["analysis"]["anomaly:haftalik"]
    periods = [a["period"] for a in result["anomalies"]]
    assert len(periods) == len(set(periods))
    assert result["n_points"] < 100                  # months, not ~300 Fridays


def test_analysis_fact_keys_include_the_second_column_and_source_map_details_them():
    needs_lakehouse()
    from backend.agent.verifier import source_map
    plan = Plan(intent="series_analysis", start="2021-01-01", end="2025-12-01", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin",
             dataset="tuketici_kredileri", as_name="konut"),
        Step(op="fetch_series", key="TP.KTF12", source="macro", as_name="faiz"),
        Step(op="analyze", method="changepoint", column="faiz"),
        Step(op="analyze", method="causality", column="konut", against="faiz"),
    ])
    session = Executor(Session()).run(plan)
    assert set(session.facts["analysis"]) == {"changepoint:faiz", "causality:konut~faiz"}
    sources = source_map(session)
    by_kind = {s["label"]: s for s in sources.values() if s["kind"] == "analysis"}
    change = next(s for s in by_kind.values() if "changepoint" in s["detail"])
    assert "2023-07" in change["detail"] and change["inputs"] == ["K2"]
    cause = next(s for s in by_kind.values() if "causality" in s["detail"])
    assert "faiz->konut p=" in cause["detail"] and cause["inputs"] == ["K1", "K2"]
    assert session.facts["analysis"]["causality:konut~faiz"]["kaynak"] == cause["tag"]


def test_deterministic_summary_states_analysis_results():
    from backend.agent.composer import deterministic_summary
    session = Session()
    session.artifact = synthetic("x")
    session.facts["analysis"] = {"anomaly:x": {"description": "x: 2 aykiri ay bulundu", "kaynak": "H1"}}
    assert "x: 2 aykiri ay bulundu [H1]" in deterministic_summary(session, "q")


def test_quotable_numbers_trims_long_anomaly_lists_and_drops_the_citation():
    from backend.agent.verifier import quotable_numbers
    session = Session()
    session.artifact = synthetic("x")
    session.facts["analysis"] = {"anomaly:x": {
        "citation": {"table": "t"}, "inputs": ["x"],
        "anomalies": [{"period": f"2021-{m:02d}", "z_score": float(m)} for m in range(1, 11)]}}
    facts = quotable_numbers(session)
    quoted = facts["analysis"]["anomaly:x"]
    assert "citation" not in quoted and len(quoted["anomalies"]) == 6
    assert quoted["anomalies"][0]["z_score"] == 10.0 and quoted["n_anomalies_shown"] == 6


def test_a_requested_analysis_that_did_not_run_is_a_caveat():
    session = Session()
    session.artifact = synthetic("x")
    session.facts["wants_analysis"] = ["anomaly"]
    report = verify(session)
    assert any("istenen analiz calismadi: anomaly" in c for c in report["caveats"])
    session.facts["analysis"] = {"anomaly:x": {"inputs": ["x"]}}
    assert not any("calismadi" in c for c in verify(session)["caveats"])


def test_a_metadata_turn_verifies_without_a_table():
    session = Session()
    session.facts["intent"] = "metadata"
    session.facts["discovery"] = [{"candidates": [{"key": "k"}]}]
    report = verify(session)
    assert report["passed"]
    assert not any("no columns" in c for c in report["caveats"])


def test_discovered_keys_carry_their_period_span():
    from backend.agent.verifier import quotable_numbers
    session = Session()
    session.facts["discovery"] = [{"candidates": [
        {"key": "k", "name": "n", "source": "bulletin", "dataset": "d", "unit": "milyon TL",
         "temporal_semantics": "stock", "currencies": ["TL", "FX", "total"],
         "first_period": "2021-01-01", "last_period": "2026-07-01", "n_periods": 67, "score": 9.0}]}]
    keys = quotable_numbers(session)["discovered_keys"]
    assert keys[0]["n_periods"] == 67 and keys[0]["currencies"] == ["TL", "FX", "total"]
    assert "score" not in keys[0]


def test_deterministic_series_plan_uses_concept_discovery(monkeypatch):
    from backend.agent import pipeline
    called = {}

    def fake(question, limit=8, **kwargs):
        called["question"] = question
        return {"candidates": [{"key": "TP.KTF12", "source": "macro", "dataset": None}]}
    monkeypatch.setattr(pipeline, "discover_concepts", fake)
    plan = pipeline.deterministic_series_plan("faiz", route("faiz", client=None))
    assert called["question"] == "faiz" and plan.steps[0].key == "TP.KTF12"


# --- footnotes: the one lakehouse fact that is text ---------------------------

def test_a_footnote_question_routes_to_a_footnotes_step():
    from backend.agent.pipeline import apply_analysis
    decided = route("Sektorel kredi dagilimi tablosunun dipnotu ne diyor?", client=None)
    assert decided.wants_footnotes
    plan = apply_analysis(template_plan("metadata", "q"), decided, Session())
    assert plan.steps[-1].op == "footnotes"


def test_footnotes_reach_the_facts_with_a_runnable_citation():
    needs_lakehouse()
    from backend.agent.verifier import quotable_numbers, source_map
    plan = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="imalat_sanayi", source="bulletin", dataset="sektorel_kredi_dagilimi",
             as_name="nakdi"),
        Step(op="footnotes"),
    ])
    session = Executor(Session()).run(plan)
    ok = [a for a in session.audit if a.op == "footnotes"][0]
    assert ok.ok, ok.detail
    found = session.facts["footnotes"][0]
    assert found["dataset"] == "sektorel_kredi_dagilimi" and found["n_notes"] >= 1
    assert "Bankalara Kullandırılan Krediler" in found["notes"][0]["footnote"]
    sources = source_map(session)
    tag = found["kaynak"]
    assert sources[tag]["kind"] == "footnotes" and run_sql(sources[tag]["sql"])["n_rows"] == found["n_notes"]
    assert quotable_numbers(session)["footnotes"][0]["notes"][0]["footnote"].startswith("*")


def test_footnotes_without_a_dataset_or_bulletin_column_fails_as_one_step():
    session = Executor(Session()).run(Plan(intent="series_analysis", steps=[Step(op="footnotes")]))
    assert not session.audit[0].ok and "needs a dataset" in session.audit[0].detail


def test_an_analyze_step_written_with_columns_is_mapped_onto_column_and_against():
    """Measured on every live analysis question: the model writes the chart
    field `columns` on an analyze step. Same intent, wrong slot."""
    plan = Plan.model_validate({"intent": "series_analysis", "steps": [
        {"op": "analyze", "method": "decompose", "columns": ["konut", "kfe"]},
        {"op": "analyze", "method": "anomaly", "columns": ["faiz"]},
    ]})
    assert [(s.column, s.against) for s in plan.steps] == [("konut", "kfe"), ("faiz", None)]
    assert "dropped" not in (plan.reasoning or "")


def test_a_sign_written_in_the_prose_does_not_make_a_number_unsupported():
    facts = {"real_pct": -80.21, "by_year": [{"real_pct": -52.2}]}
    assert unsupported_numbers("reel stok %80,21 daraldi; 2022: -%52,2", facts) == []
    assert unsupported_numbers("reel stok %75,5 daraldi", facts) == [75.5]


# --- currency is a dimension, not a search term -------------------------------

@pytest.mark.parametrize("concept, currency, rest", [
    ("Yabancı Para (YP) Mevduat", "FX", "Mevduat"),
    ("TL Mevduat stokunu", "TL", "Mevduat stokunu"),
    ("USD/TRY kuru", None, "USD/TRY kuru"),
    ("konut kredisi milyon TL", None, "konut kredisi milyon TL"),   # a unit, not a slice
    ("TP.KTF12 faizi", None, "TP.KTF12 faizi"),                       # a series code prefix
    ("TL ve YP mevduat", None, "TL ve YP mevduat"),                   # both: ambiguous, left alone
])
def test_currency_words_are_peeled_off_the_concept(concept, currency, rest):
    from backend.tools.lakehouse import extract_currency
    assert extract_currency(concept) == (currency, rest)


def test_a_currency_slice_filters_to_series_that_publish_it_and_tags_them():
    """"YP mevduat" is the FX slice of the deposit line. Searching the words
    found the FX net-position table instead, whose name contains them."""
    needs_lakehouse()
    top = discover("Yabancı Para (YP) Mevduat", limit=4)["candidates"]
    assert top[0]["key"] == "mevduat_katilim_fonu" and top[0]["currency"] == "FX"
    # Every line that publishes a currency split carries the slice; a series
    # with no such dimension (an EVDS rate) may still rank, untagged.
    assert all(c["currency"] == "FX" for c in top if c.get("currencies"))
    assert not any(c["key"].startswith("yabanci_para") for c in top)


def test_a_line_whose_own_name_holds_the_currency_words_is_not_sliced():
    needs_lakehouse()
    found = discover("yabancı para net genel pozisyonu", limit=2)
    assert found["currency"] is None
    assert found["candidates"][0]["key"] == "yabanci_para_net_genel_pozisyonu"


def test_the_same_key_survives_the_merge_once_per_slice():
    needs_lakehouse()
    from backend.tools.lakehouse import discover_concepts
    found = discover_concepts("YP mevduat ve TL mevduat stoku", limit=8)
    slices = {(c["key"], c["currency"]) for c in found["candidates"] if c["key"] == "mevduat_katilim_fonu"}
    assert slices == {("mevduat_katilim_fonu", "FX"), ("mevduat_katilim_fonu", "TL")}


def test_tl_deposit_rates_are_not_demoted_as_provinces():
    """".TRY." is the lira; the province pattern demoted every TL deposit rate."""
    needs_lakehouse()
    assert discover("mevduat faizleri", limit=1)["candidates"][0]["key"] == "TP.TRY.MT06"


def _discovery(*candidates, by_concept=None):
    return {"candidates": list(candidates),
            "by_concept": by_concept or [[(c["source"], c["key"], c.get("currency"))] for c in candidates]}


def test_apply_dimensions_splits_an_unsliced_fetch_into_the_slices_the_question_named():
    from backend.agent.pipeline import apply_dimensions
    plan = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="mevduat_katilim_fonu", source="bulletin", dataset="bilanco", as_name="mevduat"),
        Step(op="fetch_series", key="TP.DK.USD.A.YTL", source="macro", as_name="kur")])
    found = _discovery(
        {"source": "bulletin", "key": "mevduat_katilim_fonu", "dataset": "bilanco", "currency": "FX"},
        {"source": "bulletin", "key": "mevduat_katilim_fonu", "dataset": "bilanco", "currency": "TL"})
    plan = apply_dimensions(plan, found)
    fetched = [(s.key, s.currency, s.as_name) for s in plan.steps if s.op == "fetch_series"]
    assert fetched == [("mevduat_katilim_fonu", "FX", "mevduat_fx"),
                       ("mevduat_katilim_fonu", "TL", "mevduat_tl"),
                       ("TP.DK.USD.A.YTL", None, "kur")]


def test_apply_dimensions_respects_a_slice_the_plan_already_names_and_fetches_a_missing_key():
    from backend.agent.pipeline import apply_dimensions
    plan = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="krediler", source="bulletin", dataset="krediler", currency="TL", as_name="tl_kredi")])
    found = _discovery(
        {"source": "bulletin", "key": "krediler", "dataset": "krediler", "currency": "TL"},
        {"source": "bulletin", "key": "mevduat_katilim_fonu", "dataset": "bilanco", "currency": "FX"})
    plan = apply_dimensions(plan, found)
    fetched = [(s.key, s.currency, s.as_name) for s in plan.steps if s.op == "fetch_series"]
    assert fetched == [("krediler", "TL", "tl_kredi"), ("mevduat_katilim_fonu", "FX", "mevduat_katilim_fonu_fx")]
    assert "dimension" in plan.reasoning


def test_apply_dimensions_leaves_a_plan_alone_when_no_slice_was_named():
    from backend.agent.pipeline import apply_dimensions
    plan = Plan(intent="series_analysis", steps=[Step(op="fetch_series", key="krediler", source="bulletin")])
    same = apply_dimensions(plan, _discovery({"source": "bulletin", "key": "krediler", "dataset": "krediler"}))
    assert [s.currency for s in same.steps] == [None]


# --- the valuation guard: an FX stock in TL moves with the rate by construction ---

def test_valuation_guard_adds_the_tl_share_and_the_dollar_stock():
    from backend.agent.pipeline import apply_valuation_guard
    plan = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="mevduat_katilim_fonu", source="bulletin", dataset="bilanco",
             currency="FX", as_name="mevduat_fx"),
        Step(op="fetch_series", key="TP.DK.USD.A.YTL", source="macro", as_name="kur"),
        Step(op="analyze", method="anomaly", column="kur")])
    plan = apply_valuation_guard(plan, Session())
    ops = [(s.op, s.operation or s.currency, s.as_name) for s in plan.steps]
    assert ops == [("fetch_series", "FX", "mevduat_fx"),
                   ("fetch_series", None, "kur"),
                   ("fetch_series", "TL", "mevduat_tl"),
                   ("fetch_series", "total", "mevduat_total"),
                   ("transform", "ratio", "mevduat_tl_payi"),
                   ("transform", "in_usd", "mevduat_fx_usd"),
                   ("analyze", None, None)]
    share = plan.steps[4]
    assert (share.column, share.other_column) == ("mevduat_tl", "mevduat_total")
    usd = plan.steps[5]
    assert (usd.column, usd.other_column) == ("mevduat_fx", "kur")


def test_valuation_guard_is_silent_without_an_exchange_rate_or_a_slice():
    from backend.agent.pipeline import apply_valuation_guard
    no_rate = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="mevduat_katilim_fonu", source="bulletin", currency="FX")])
    assert len(apply_valuation_guard(no_rate, Session()).steps) == 1
    no_slice = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="mevduat_katilim_fonu", source="bulletin"),
        Step(op="fetch_series", key="TP.DK.USD.A.YTL", source="macro")])
    assert len(apply_valuation_guard(no_slice, Session()).steps) == 2


def test_the_deposit_question_builds_the_share_and_dollar_columns_with_no_model():
    """The question that produced "the data holds no TL/FX split": it does,
    and the model-free path now fetches both slices, the TL share and the
    FX stock in dollars over the window the question named (2021 sonu)."""
    needs_lakehouse()
    from backend.agent.pipeline import run_turn
    result = run_turn("2021 sonundan 2024 sonuna kadar BDDK bültenindeki Yabancı Para (YP) Mevduat "
                      "ve TL Mevduat stokunu, EVDS'deki USD/TRY kuru ve TCMB Politika Faizi ile eşleştir.",
                      client=None)
    columns = result["table"]["columns"]
    assert {"mevduat_katilim_fonu_fx", "mevduat_katilim_fonu_tl", "mevduat_katilim_fonu_total",
            "mevduat_katilim_fonu_tl_payi", "mevduat_katilim_fonu_fx_usd"} <= set(columns)
    rows = result["table"]["rows"]
    assert rows[0]["period"].startswith("2021-12") and rows[-1]["period"].startswith("2024-12")
    assert rows[0]["mevduat_katilim_fonu_tl_payi"] == pytest.approx(35.46, abs=0.05)
    assert rows[-1]["mevduat_katilim_fonu_tl_payi"] == pytest.approx(65.11, abs=0.05)
    assert rows[-1]["mevduat_katilim_fonu_fx_usd"] == pytest.approx(188_980, rel=0.01)
    assert result["table"]["units"]["mevduat_katilim_fonu_fx_usd"] == "milyon USD"
    assert not any("mixed monetary" in c for c in result["verification"]["caveats"])
    assert "mekanik" in result["summary"]


# --- numbers are formatted in Python, never rescaled by the model -----------------

@pytest.mark.parametrize("value, unit, expected", [
    (3423006, "milyon TL", "3,42 trilyon TL"),
    (678970, "milyon TL", "678,97 milyar TL"),
    (40418, "milyon TL", "40,42 milyar TL"),
    (1234.5, "bin TL", "1,23 milyon TL"),
    (34.9, "TL", "34,90 TL"),
    (188980, "milyon USD", "188,98 milyar USD"),
    (49.83, "%", "%49,83"),
    (1139.3, "endeks", "1.139,30 endeks"),
    (123456, "adet", "123.456 adet"),
])
def test_format_quantity_writes_a_human_scale_in_turkish_notation(value, unit, expected):
    from backend.agent.formatting import format_quantity
    assert format_quantity(value, unit) == expected


def test_quotable_numbers_hand_the_composer_finished_text_not_raw_values():
    from backend.agent.verifier import quotable_numbers
    session = Session()
    session.artifact = synthetic("dep", n=3, start=3_423_006.0, step=1_000_000.0)
    facts = quotable_numbers(session)["series"]["dep"]
    assert facts["ilk"] == "2021-01: 3,42 trilyon TL" and facts["son"] == "2021-03: 5,42 trilyon TL"
    assert facts["degisim"] == "%+58,4"
    assert "first_value" not in facts and "last_value" not in facts


def test_a_correctly_copied_scaled_figure_is_supported_and_a_rescaled_one_is_not():
    from backend.agent.verifier import quotable_numbers
    session = Session()
    session.artifact = synthetic("dep", n=3, start=3_423_006.0, step=1_000_000.0)
    facts = quotable_numbers(session)
    assert unsupported_numbers("Mevduat 3,42 trilyon TL'den 5,42 trilyon TL'ye cikti", facts) == []
    assert unsupported_numbers("Mevduat 3.423.006 milyon TL idi", facts) == [3423006.0]


def test_an_exchange_rate_changes_in_percent_not_points_and_is_not_a_monetary_unit():
    artifact = synthetic("kur", n=2, start=13.53, step=21.37, unit="TL", semantics="rate")
    stats = artifact.summary()["kur"]
    assert "change_pct" in stats and "change_points" not in stats
    artifact.add_column("dep", artifact.frame["kur"] * 1e6, ColumnLineage(
        column="dep", label="Dep", source="bulletin", unit="milyon TL", temporal_semantics="stock",
        citation={"table": "t"}))
    session = Session()
    session.artifact = artifact
    checks = {c["check"]: c for c in verify(session)["checks"]}
    assert checks["monetary_columns_share_one_unit"]["passed"]


# --- dates are parsed in Python -----------------------------------------------------

@pytest.mark.parametrize("question, expected", [
    ("2021 sonundan 2024 sonuna kadar", ("2021-12-01", "2024-12-01")),
    ("2022 basindan 2023 ortasina", ("2022-01-01", "2023-06-01")),
    ("2021 yılının sonundan itibaren", ("2021-12-01", None)),
    ("2024 sonuna kadar", (None, "2024-12-01")),
    ("2023 yılının ilk yarısı", ("2023-01-01", "2023-06-01")),
    ("2021 yılı sonunda", ("2021-12-01", "2021-12-01")),
])
def test_qualified_years_resolve_to_months(question, expected):
    assert extract_window(question) == expected


# --- FinTurk (il-bazli) through the executor ----------------------------------

def test_fetch_series_loads_a_finturk_metric_for_one_province():
    needs_lakehouse()
    plan = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="konut_kredisi", source="finturk",
             dataset="bireysel_bankacilik", province="İSTANBUL", as_name="konut_istanbul"),
    ])
    session = Executor(Session()).run(plan)
    assert session.audit[-1].ok, session.audit[-1].detail
    lineage = session.artifact.lineage["konut_istanbul"]
    assert lineage.source == "finturk" and lineage.unit == "bin TL"
    assert lineage.citation["filters"]["province"] == "İSTANBUL"
    assert len(session.artifact.frame) == 22          # 2021-Q1..2026-Q2


def test_two_provinces_of_the_same_finturk_key_do_not_collide_without_as_name():
    """Measured live: "Ankara ve İstanbul'daki tasarruf mevduatını karşılaştır"
    planned two fetch_series steps for one finturk key with neither `as_name`
    set, both auto-named "tasarruf_mevduati" -- `AnalysisArtifact.add_column`
    replaces a same-named column outright, so the second step (Ankara)
    silently overwrote the first (İstanbul) within the same turn, and the
    composer, seeing only one column left, told the user İstanbul had no data
    at all. Two different series that would collide on the same auto-name are
    disambiguated by province instead."""
    needs_lakehouse()
    plan = Plan(intent="series_analysis", start="2021-03-01", end="2026-06-01", steps=[
        Step(op="fetch_series", key="tasarruf_mevduati", source="finturk",
             dataset="mevduat", province="İSTANBUL"),
        Step(op="fetch_series", key="tasarruf_mevduati", source="finturk",
             dataset="mevduat", province="ANKARA"),
    ])
    session = Executor(Session()).run(plan)
    assert all(a.ok for a in session.audit), [a.detail for a in session.audit if not a.ok]
    columns = session.artifact.column_names()
    assert len(columns) == 2, columns
    provinces = {session.artifact.lineage[c].citation["filters"]["province"] for c in columns}
    assert provinces == {"İSTANBUL", "ANKARA"}


def test_a_deliberate_as_name_still_overwrites_a_stale_column_from_an_earlier_turn():
    """The disambiguation above must not interfere with the existing, opposite
    repair: a fresh question's plan naming a column explicitly (`as_name`)
    still replaces whatever an earlier turn left under that name -- see
    `test_a_fresh_question_never_plans_over_a_previous_questions_column`,
    which pins this at the `make_plan` level; this pins it at the executor's
    own collision check, scoped to only the current turn's auto-named
    columns."""
    needs_lakehouse()
    session = Session()
    session.start_turn("ilk soru")
    stale = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="konut_kredisi", source="finturk",
             dataset="bireysel_bankacilik", province="İSTANBUL", as_name="konut"),
    ])
    Executor(session).run(stale)
    assert session.artifact.lineage["konut"].source == "finturk"

    session.start_turn("ikinci soru")
    fresh = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin",
             dataset="tuketici_kredileri", as_name="konut"),
    ])
    session = Executor(session).run(fresh)
    assert session.artifact.lineage["konut"].source == "bulletin"
    assert "konut_2" not in session.artifact.column_names()


def test_fetch_series_resolves_a_finturk_province_case_and_dotless_i_safely():
    """Python's plain `.upper()` turns 'istanbul' into 'ISTANBUL', not the DB's
    'İSTANBUL' -- a model or user typing the ASCII-only spelling would
    otherwise get a silent 'no rows' failure for one of the most-asked
    provinces. See tools.series._finturk, which resolves through
    core.labels.slugify instead of a naive case-fold."""
    needs_lakehouse()
    for spelling in ("Istanbul", "istanbul", "İstanbul", "ISTANBUL"):
        plan = Plan(intent="series_analysis", steps=[
            Step(op="fetch_series", key="konut_kredisi", source="finturk",
                 dataset="bireysel_bankacilik", province=spelling, as_name="konut"),
        ])
        session = Executor(Session()).run(plan)
        assert session.audit[-1].ok, f"{spelling!r}: {session.audit[-1].detail}"
        assert session.artifact.lineage["konut"].citation["filters"]["province"] == "İSTANBUL"


def test_fetch_series_sums_provinces_for_a_finturk_metric_without_one_and_its_sql_reproduces_it():
    """There is no published Türkiye row in FinTurk, so the national figure is
    a sum over provinces -- and the citation's SQL must say so and reproduce
    the column, or the [K] tag is decorative."""
    needs_lakehouse()
    from backend.agent.verifier import source_map
    plan = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="konut_kredisi", source="finturk",
             dataset="bireysel_bankacilik", as_name="konut_turkiye"),
    ])
    session = Executor(Session()).run(plan)
    assert session.audit[-1].ok, session.audit[-1].detail
    lineage = session.artifact.lineage["konut_turkiye"]
    assert "Türkiye" in lineage.label and "province" not in lineage.citation["filters"]
    assert lineage.citation["aggregate"]
    session.focus()
    sql = next(s["sql"] for s in source_map(session).values() if s.get("column") == "konut_turkiye")
    assert "GROUP BY period" in sql and "YURT DIŞI" in sql
    with duckdb.connect(str(DUCKDB_PATH), read_only=True) as con:
        reproduced = con.execute(sql).df().set_index("period")["value"]
    reproduced.index = pd.to_datetime(reproduced.index)
    pd.testing.assert_series_equal(reproduced.sort_index(), session.artifact.frame["konut_turkiye"],
                                   check_names=False, check_freq=False, check_dtype=False)


def test_source_resolution_corrects_a_bulletin_guess_with_province_to_finturk():
    """The regression this closes: the model wrote source="bulletin" with a
    real bulletin entity_key ("mevduat_katilim_fonu", a valid national balance
    sheet line -- so `load_series` raised nothing) but also filled
    province="ANKARA", a field only finturk supports. Before this fix,
    `_fetch` fetched the bulletin entity directly, and the Turkiye-wide
    bulletin total reached the artifact captioned as the Ankara answer.
    Naming a province must always resolve to finturk, the only source with a
    province column, regardless of what `source` the plan wrote."""
    needs_lakehouse()
    plan = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="toplam_mevduat", source="bulletin",
             dataset="mevduat_turler", province="ANKARA", as_name="toplam_mevduat"),
    ])
    session = Executor(Session()).run(plan)
    assert session.audit[-1].ok, session.audit[-1].detail
    lineage = session.artifact.lineage["toplam_mevduat"]
    assert lineage.source == "finturk"
    assert lineage.citation["filters"]["province"] == "ANKARA"


@pytest.mark.parametrize("guessed_source", ["bulletin", "macro", None])
def test_source_resolution_ignores_any_incompatible_guess_for_a_province_query(guessed_source):
    """Whatever source the model guessed -- bulletin, macro, or none at all --
    naming a province must land on finturk. Fallback discovery must not stay
    confined to the model's wrong guess either: that is the mechanism that let
    the bug through when the first fetch *did* raise (a bad dataset guess) --
    the ValueError fallback searched the wrong source only and never got a
    chance to look at finturk."""
    needs_lakehouse()
    plan = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="toplam_mevduat", source=guessed_source,
             dataset="mevduat_turler" if guessed_source == "bulletin" else None,
             province="ANKARA", as_name="toplam_mevduat"),
    ])
    session = Executor(Session()).run(plan)
    assert session.audit[-1].ok, session.audit[-1].detail
    lineage = session.artifact.lineage["toplam_mevduat"]
    assert lineage.source == "finturk"
    assert lineage.citation["filters"]["province"] == "ANKARA"


def test_source_resolution_leaves_a_provinceless_bulletin_query_unchanged():
    """No narrowing field set -- an explicit, valid source must pass through
    untouched rather than being second-guessed."""
    needs_lakehouse()
    plan = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin",
             dataset="tuketici_kredileri", as_name="konut"),
    ])
    session = Executor(Session()).run(plan)
    assert session.audit[-1].ok, session.audit[-1].detail
    assert session.artifact.lineage["konut"].source == "bulletin"


def test_a_province_query_with_no_compatible_candidate_fails_the_step_explicitly():
    """When no finturk candidate matches at all, the step must fail loudly --
    never silently fall back to a broader, non-province source."""
    needs_lakehouse()
    plan = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="zzzzz_qqqqq_nonexistent_metric_xyz", source="bulletin",
             province="ANKARA", as_name="x"),
    ])
    session = Executor(Session()).run(plan)
    assert not session.audit[-1].ok
    assert "finturk" in session.audit[-1].detail


def test_validate_series_matches_request_refuses_a_national_total_for_a_province_step():
    """Backstop unit test: even a series that reached this point without
    raising must still be province-level if the step asked for one -- a
    Turkiye-wide finturk sum (province=None) can never stand in for a named
    province, regardless of how it got here."""
    from backend.agent.executor import _validate_series_matches_request

    step = Step(op="fetch_series", key="toplam_mevduat", source="finturk", province="ANKARA")
    national = SeriesResult(
        values=pd.Series([1.0], index=pd.DatetimeIndex(["2021-03-01"])),
        source="finturk", key="toplam_mevduat", name="Toplam Mevduat (Türkiye)",
        unit="bin TL", temporal_semantics="stock", value_column="value",
        dataset="mevduat", currency=None, metric="toplam_mevduat", province=None)
    with pytest.raises(ValueError, match="not province-level"):
        _validate_series_matches_request(step, national)


def test_validate_series_matches_request_refuses_a_mismatched_province():
    from backend.agent.executor import _validate_series_matches_request

    step = Step(op="fetch_series", key="toplam_mevduat", source="finturk", province="ANKARA")
    izmir = SeriesResult(
        values=pd.Series([1.0], index=pd.DatetimeIndex(["2021-03-01"])),
        source="finturk", key="toplam_mevduat", name="Toplam Mevduat (İZMİR)",
        unit="bin TL", temporal_semantics="stock", value_column="value",
        dataset="mevduat", currency=None, metric="toplam_mevduat", province="İZMİR")
    with pytest.raises(ValueError, match="mismatched province"):
        _validate_series_matches_request(step, izmir)


def test_discover_prefers_the_bulletin_line_unless_a_province_is_named():
    """FinTurk republishes many bulletin concepts quarterly by province. A plain
    "konut kredisi" is the monthly national line; "İstanbul'da konut kredisi"
    is the FinTurk row, and nothing else can answer it."""
    needs_lakehouse()
    plain = discover("konut kredisi", limit=5)["candidates"]
    assert plain[0]["source"] != "finturk"
    by_province = discover("İstanbul'da konut kredisi", limit=5)["candidates"]
    assert by_province[0]["source"] == "finturk", [c["key"] for c in by_province]
    by_grain = discover("il bazında konut kredisi", limit=5)
    assert by_grain["sources"] == ["finturk"]
    assert all(c["source"] == "finturk" for c in by_grain["candidates"])


def test_discover_concepts_does_not_split_the_locative_suffix_off_a_province():
    """Measured live: "Ankara'da toplam mevduat hacmi ne kadar?" -- one clause,
    no comma -- split into "Ankara'" and "toplam mevduat hacmi ne kadar" under
    the old CLAUSE_SPLIT, because Turkish attaches the locative suffix to a
    place name with an apostrophe and no space, and a bare `\\bda\\b` cannot
    tell that apart from the standalone conjunction "da" (an apostrophe is
    already a non-word character and satisfies `\\b` on its own). Severed from
    its clause, the province name never reached finturk's scoring, and the
    national bulletin total ("Mevduat (Katılım Fonu)") answered in its place --
    this is the plan `deterministic_series_plan` fell back to live, and the
    composer captioned it as Ankara's regardless. The standalone conjunction
    case ("konut kredisi de yuksek mi") must still split."""
    needs_lakehouse()
    kept = discover_concepts("Ankara'da toplam mevduat hacmi ne kadar?", limit=8)
    assert kept["n_concepts"] == 1, kept["concepts"]
    assert kept["candidates"] and kept["candidates"][0]["source"] == "finturk", \
        [c["key"] for c in kept["candidates"]]
    assert kept["candidates"][0]["key"] == "toplam_mevduat"

    conjunction = discover_concepts("konut kredisi de faiz oranlarini goster", limit=8)
    assert conjunction["n_concepts"] == 2, conjunction["concepts"]


def test_deterministic_series_plan_names_the_province_for_a_locative_question():
    """The fallback path a validation-failing model plan lands on -- proven
    live to be reached often enough to matter -- must resolve the same
    province-named question a working model plan would."""
    needs_lakehouse()
    from backend.agent import pipeline

    plan = pipeline.deterministic_series_plan(
        "Ankara'da toplam mevduat hacmi ne kadar?", route_result=route(
            "Ankara'da toplam mevduat hacmi ne kadar?", client=None))
    fetches = [s for s in plan.steps if s.op == "fetch_series"]
    assert any(s.source == "finturk" and s.province == "ANKARA" for s in fetches), fetches


# --- causality routing: the branch's failure modes, guarded here --------------

def _synthetic_rate_and_loan(seed: int, n: int = 100):
    """rate[t-2] contributes to loan[t], so rate should Granger-predict loan."""
    import numpy as np
    rng = np.random.default_rng(seed)
    index = pd.date_range("2018-01-01", periods=n, freq="MS")
    rate = rng.normal(size=n)
    loan = rng.normal(scale=0.3, size=n)
    for t in range(2, n):
        loan[t] += 0.8 * rate[t - 2]

    artifact = AnalysisArtifact()
    artifact.add_column("rate", pd.Series(rate, index=index),
                        ColumnLineage(column="rate", label="Rate", source="macro",
                                      unit="%", temporal_semantics="rate", key="rate"))
    artifact.add_column("loan", pd.Series(loan, index=index),
                        ColumnLineage(column="loan", label="Loan", source="bulletin",
                                      unit="milyon TL", temporal_semantics="stock", key="loan"))
    return artifact


def test_executor_runs_causality_on_artifact_columns_and_names_the_direction():
    plan = Plan(intent="series_analysis", steps=[
        Step(op="analyze", method="causality", column="loan", against="rate"),
    ])
    session = Executor(Session(artifact=_synthetic_rate_and_loan(123))).run(plan)
    assert session.audit[0].ok, session.audit[0].detail
    result = session.facts["analysis"]["causality:loan~rate"]
    assert result["cause"] == "rate" and result["effect"] == "loan"
    assert result["directions"]["rate->loan"]["predictive"]
    assert result["verdict"] in ("predictor->target", "both")
    assert result["lead_lag"]["strongest"]["lag"] == 2


def test_deterministic_summary_states_a_causality_result():
    from backend.agent.composer import deterministic_summary
    plan = Plan(intent="series_analysis", steps=[
        Step(op="analyze", method="causality", column="loan", against="rate"),
    ])
    session = Executor(Session(artifact=_synthetic_rate_and_loan(321))).run(plan)
    session.focus()
    summary = deterministic_summary(session, "Faiz kredileri etkiliyor mu?")
    assert "Granger testi" in summary and "nedensellik kaniti degildir" in summary


@pytest.mark.parametrize("question", [
    "Konut kredisi faizi konut kredisi hacmini etkiliyor mu?",
    "Faiz ile kredi arasinda neden-sonuc iliskisi var mi?",
    "Faizin krediyi etkilediğini söyleyebilir miyiz?",
    "Politika faizi konut kredileri icin oncu gosterge mi?",
])
def test_the_router_reads_the_causality_phrasings_the_model_used_to_miss(question):
    """Measured live: these phrasings fetched both series and only charted
    them. The words are the signal; `apply_analysis` then guarantees the step."""
    assert route(question, client=None).wants_analysis == ["causality"]


@pytest.mark.parametrize("question", [
    "Konut kredisi ile altın arasında nedensellik iddia etme, sadece karşılaştır.",
    "Bu iki serinin stok/akım farkını açıkla; aralarında nedensellik iddia etme.",
    "Nedensellik iddia edilemez, sadece aynı yönde mi hareket ettiklerine bak.",
])
def test_the_router_does_not_plan_causality_when_the_question_forbids_it(question):
    """Measured live: "...aralarında nedensellik iddia etme" still triggered a
    causality step, because `causality_strong` is a bare keyword match on
    "nedensellik" with no regard for the sentence forbidding the claim it
    names. The tool refused for lack of data (>=24 observations) and the
    composer honoured the instruction in prose, so no wrong number reached
    the user this time -- but the step should never have been planned."""
    assert route(question, client=None).wants_analysis == []


class _StubPlanClient:
    """A fake KloudeksClient that returns one fixed plan, for testing the
    repairs make_plan applies to whatever the model handed back."""

    def __init__(self, plan: Plan):
        self._plan = plan

    def structured(self, messages, schema, max_tokens=1400, **kwargs):
        return self._plan


def test_make_plan_falls_back_when_the_model_only_discovers_and_stops():
    """Measured live: a model handed a fresh question sometimes emits a
    single `discover` step and stops, rather than committing to the fetch it
    already found candidates for -- a plan that is *valid* (discover needs
    only `query`) but produces an empty table, which is a worse failure than
    no fallback at all. make_plan treats it the same as an unreachable model,
    and the analysis guarantee then applies on top of the recovered plan."""
    needs_lakehouse()
    from backend.agent.pipeline import apply_analysis, make_plan
    question = "Konut kredisi faizi konut kredisi hacmini etkiliyor mu?"
    dead_end = Plan(intent="series_analysis", steps=[Step(op="discover", query="konut kredisi hacmi")])
    route_result = route(question, has_artifact=False, client=None)
    plan = make_plan(question, Session(), route_result, _StubPlanClient(dead_end))
    assert any(step.op == "fetch_series" for step in plan.steps)
    assert "replaced" in plan.reasoning
    # The recovered plan gets the same analysis guarantee as any other. For
    # this phrasing discovery ranks only rate series for the one clause (the
    # loan book is not in its top eight -- "hacmi" is a word no row carries),
    # so the pair cannot be formed and the caveat path reports it instead of
    # a test between two interest rates.
    plan = apply_analysis(plan, route_result, Session())
    fetched = [s for s in plan.steps if s.op == "fetch_series"]
    has_pair = len(fetched) >= 2
    assert any(s.op == "analyze" and s.method == "causality" for s in plan.steps) == has_pair
    if not has_pair:
        session = Session()
        session.facts["wants_analysis"] = ["causality"]
        session.artifact = synthetic("faiz")
        assert any("istenen analiz calismadi: causality" in c for c in verify(session)["caveats"])


def test_make_plan_keeps_a_discover_only_plan_for_a_followup_with_an_existing_table():
    """The dead-end repair is for a *fresh* question with nothing to show for
    it -- a follow-up that already has a table is not a dead end just because
    this particular step only re-discovers a key."""
    needs_lakehouse()
    from backend.agent.pipeline import make_plan
    dead_end = Plan(intent="followup", steps=[Step(op="discover", query="ek bir seri")])
    session = Session(artifact=synthetic("konut"))
    route_result = route("ek bir seri bul", has_artifact=True, client=None)
    plan = make_plan("ek bir seri bul", session, route_result, _StubPlanClient(dead_end))
    assert plan.steps == dead_end.steps


def test_an_analyze_step_without_a_column_takes_the_last_fetched_one():
    """Small models reliably forget `column` right after fetching the very
    series they mean; the plan fills it from the most recent producing step."""
    plan = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin", as_name="konut"),
        Step(op="analyze", method="anomaly"),
    ])
    assert plan.steps[-1].column == "konut"
    keyed = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="TP.KTF12", source="macro"),
        Step(op="analyze", method="changepoint"),
    ])
    assert keyed.steps[-1].column == "TP.KTF12"


def test_an_analyze_step_with_no_column_and_nothing_before_it_is_dropped_not_fatal():
    plan = Plan(intent="series_analysis", steps=[
        Step(op="analyze", method="anomaly"),
        Step(op="discover", query="konut"),
    ])
    assert [s.op for s in plan.steps] == ["discover"]
    assert "dropped invalid step" in plan.reasoning


def test_a_changepoint_result_marks_its_breaks_on_the_chart():
    needs_lakehouse()
    plan = Plan(intent="series_analysis", start="2021-01-01", end="2025-12-01", steps=[
        Step(op="fetch_series", key="TP.KTF12", source="macro", as_name="faiz"),
        Step(op="analyze", method="changepoint", column="faiz"),
        Step(op="chart"),
    ])
    session = Executor(Session()).run(plan)
    assert all(a.ok for a in session.audit), [a.detail for a in session.audit if not a.ok]
    result = session.facts["analysis"]["changepoint:faiz"]
    assert result["kind"] == "level" and "2023-07" in result["breakpoints"]
    assert result["breaks"][0]["shift_unit"] == "puan" and "2023-07" in result["description"]
    assert result["period_start"] == "2021-01"        # full history, not the plan window
    shapes = session.facts["figure"]["layout"]["shapes"]
    assert any(s["x0"].startswith("2023-07") for s in shapes)
    assert "break marker" in session.audit[-1].detail


# --- a province is a dimension, like a currency -------------------------------

@pytest.mark.parametrize("question, expected", [
    ("2021'den itibaren", ("2021-01-01", None)),
    ("2024'e kadar", (None, "2024-12-01")),
    ("2021'den 2024'e kadar", ("2021-01-01", "2024-12-01")),
    ("2021 sonundan itibaren", ("2021-12-01", None)),
    ("2023 yilinda", ("2023-01-01", "2023-12-01")),
])
def test_extract_window_reads_the_case_suffix_on_a_bare_year(question, expected):
    """"2021'den itibaren" once read as the calendar year 2021 and returned
    twelve months for an open-ended question."""
    assert extract_window(question) == expected


def test_discover_peels_the_province_off_the_concept_and_tags_the_finturk_row():
    needs_lakehouse()
    from backend.tools.lakehouse import extract_province
    assert extract_province("İstanbul'daki takipteki alacaklar oranı") == ("İSTANBUL", "takipteki alacaklar oranı")
    assert extract_province("İzmirdeki mevduat") == ("İZMİR", "mevduat")
    assert extract_province("İstanbul ve Ankara mevduat")[0] is None      # two provinces: not a slice
    found = discover("İstanbul'daki takipteki alacaklar oranı", limit=3)
    assert found["province"] == "İSTANBUL" and "istanbul" not in " ".join(found["terms_used"])
    top = found["candidates"][0]
    assert top["source"] == "finturk" and top["province"] == "İSTANBUL"
    assert all(c.get("province") is None for c in found["candidates"] if c["source"] != "finturk")


def test_a_finturk_ratio_is_never_summed_across_provinces():
    """81 provincial NPL ratios added up read "%266" once. The national ratio
    lives in the monthly bulletin's rasyolar table, not in this product."""
    needs_lakehouse()
    with pytest.raises(ValueError, match="ratio and cannot be summed"):
        load_series("takipteki_alacaklar_toplam_nakdi_kredi_orani", source="finturk", dataset="oranlar",
                    currency=None)
    named = load_series("takipteki_alacaklar_toplam_nakdi_kredi_orani", source="finturk", dataset="oranlar",
                        currency=None, province="İstanbul")
    assert named.unit == "%" and named.province == "İSTANBUL" and float(named.values.max()) < 100


def test_apply_dimensions_puts_the_named_province_on_a_finturk_fetch():
    from backend.agent.pipeline import apply_dimensions
    discovery = {"candidates": [
        {"source": "finturk", "key": "konut_kredisi", "dataset": "bireysel_bankacilik", "province": "ANKARA"},
        {"source": "bulletin", "key": "tuketici_kredileri_konut", "dataset": "tuketici_kredileri"},
    ], "by_concept": []}
    plan = Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", key="konut_kredisi", source="finturk", dataset="bireysel_bankacilik", as_name="konut_il"),
        Step(op="fetch_series", key="tuketici_kredileri_konut", source="bulletin", as_name="konut_tr"),
    ])
    plan = apply_dimensions(plan, discovery)
    assert plan.steps[0].province == "ANKARA" and plan.steps[1].province is None


def test_a_province_question_runs_deterministically_against_the_bulletin_ratio():
    """The FinTurk NPL ratio for one il beside the monthly bulletin's national
    one: both in %, so no unit trap, and the il filter must reach the fetch."""
    needs_lakehouse()
    from backend.agent.pipeline import run_turn
    question = ("İstanbul'daki takipteki alacaklar oranı ile BDDK aylık bültenindeki Türkiye geneli "
                "takipteki alacaklar oranını 2021'den itibaren karşılaştır.")
    result = run_turn(question, client=None, compose_answer=False)
    fetched = {a["arguments"].get("key"): a["arguments"] for a in result["audit"] if a["op"] == "fetch_series"}
    assert fetched["takipteki_alacaklar_toplam_nakdi_kredi_orani"]["province"] == "İSTANBUL"
    assert "takipteki_alacaklar_brut_toplam_nakdi_krediler" in fetched
    assert set(result["table"]["units"].values()) == {"%"}
    assert result["route"]["start"] == "2021-01-01" and result["route"]["end"] is None
    assert not any("mixed monetary units" in c for c in result["verification"]["caveats"])


# --- presentation-only follow-ups: "bunun grafiğini çiz", "tablo yap" ---------
#
# Measured live (2026-09-21): a table question followed by "bunun grafiğini
# çiz" produced no chart at all, and "bu tabloyu bozmadan grafiğini çiz"
# fetched TP.HPBITABLO1.2 (M1 money supply -- the stem of "tabloyu" is a
# substring of that code) and stretched the 60-row table to 67. Four rules
# combined: the router did not read a suffixed "tablonun"/"tabloya" or a bare
# "grafik çiz" as a follow-up, discovery searched the presentation words, the
# deterministic fallback skipped the follow-up window, and
# `apply_presentation` stripped the chart step because the plan fetched
# nothing -- even when the model had returned exactly `[{"op": "chart"}]`.

@pytest.mark.parametrize("question, presentation_only", [
    ("bunun grafiğini çiz", True),
    ("grafik çiz", True),
    ("tablo yap", True),
    ("yeni tablo yap", True),
    ("bunu grafik olarak göster", True),
    ("bu tablonun grafiğini çizer misin", True),
    ("şimdi tablo halinde ver", True),
    ("çubuk grafik yap", True),
    ("konut kredilerini grafik olarak çiz", False),     # names a series: a real question
    ("2021-2025 arası konut kredisi ve faiz oranını göster", False),
    ("Enflasyon nasıl değişti", False),
    ("tabloyu temizle", False),                         # not a presentation request
])
def test_the_router_reads_a_bare_presentation_request_as_a_followup(question, presentation_only):
    from backend.agent.router import is_presentation_only
    assert is_presentation_only(question) is presentation_only
    with_table = route(question, has_artifact=True, client=None)
    assert with_table.presentation_only is presentation_only
    if presentation_only:
        assert with_table.is_followup and with_table.intent == "followup"
    # Without a table there is nothing to re-present: the ordinary path runs.
    assert route(question, has_artifact=False, client=None).presentation_only is False


@pytest.mark.parametrize("question", [
    "aynı tabloya enflasyonu ekle", "bu tablonun grafiğini çiz", "mevcut tabloya KFE ekle",
    "tablodaki konut sütununu enflasyondan arındır",
])
def test_followup_phrasing_survives_turkish_case_suffixes(question):
    assert route(question, has_artifact=True, client=None).is_followup


def test_listele_asks_for_a_table_not_for_metadata():
    decided = route("konut kredilerini listele", client=None)
    assert decided.intent == "series_analysis" and decided.wants_table


def test_a_chart_over_an_existing_table_survives_apply_presentation():
    """The gate stops a chart nobody asked for -- not one the user just asked
    for by name over the table on screen. The model's own step, with the
    columns it named, is the one kept."""
    from backend.agent.pipeline import apply_presentation

    session = Session()
    session.artifact = synthetic("konut")
    asked = route("bunun grafiğini çiz", has_artifact=True, client=None)
    plan = apply_presentation(Plan(intent="followup", steps=[Step(op="chart", columns=["konut"])]),
                              asked, "q", session)
    assert [s.op for s in plan.steps] == ["chart"] and plan.steps[0].columns == ["konut"]

    # A chart-less plan over the same table gains one...
    plan = apply_presentation(Plan(intent="followup", steps=[]), asked, "q", session)
    assert [s.op for s in plan.steps] == ["chart"]
    # ...but a clear_table has nothing left to draw, and an empty session neither.
    plan = apply_presentation(Plan(intent="followup", steps=[Step(op="clear_table")]), asked, "q", session)
    assert [s.op for s in plan.steps] == ["clear_table"]
    plan = apply_presentation(Plan(intent="followup", steps=[]), asked, "q", Session())
    assert plan.steps == []


def test_a_followup_plan_may_be_empty():
    """'tablo yap' over a table runs no step and re-presents it."""
    assert Plan(intent="followup", steps=[]).steps == []
    with pytest.raises(ValueError):
        Plan(intent="series_analysis", steps=[])


def test_a_bare_chart_request_charts_the_table_on_screen_and_nothing_else():
    """End to end, no model: the second turn draws the first turn's columns
    over the first turn's window, and fetches nothing."""
    from backend.agent.pipeline import run_turn
    needs_lakehouse()
    session = Session()
    first = run_turn("2021-2025 arası konut kredisi ve faiz oranını göster", session,
                     client=None, compose_answer=False)
    assert first["figure"] is None and len(first["table"]["rows"]) == 60
    shown = first["table"]["columns"]

    second = run_turn("bunun grafiğini çiz", session, client=None, compose_answer=False)
    assert second["route"]["is_followup"] and second["presentation"]["chart"]
    assert [s["op"] for s in second["plan"]["steps"]] == ["chart"]
    assert session.artifact.column_names() == shown            # nothing fetched
    assert len(second["table"]["rows"]) == 60                  # window kept
    figure = second["figure"]
    assert figure is not None and len(figure["data"]) == len(shown)
    assert all(trace["x"][0] == "2021-01-01" and trace["x"][-1] == "2025-12-01" for trace in figure["data"])

    third = run_turn("tablo yap", session, client=None, compose_answer=False)
    assert third["plan"]["steps"] == [] and third["table"]["columns"] == shown
    assert third["presentation"] == {"table": True, "chart": False}


def test_discovery_never_matches_a_presentation_word():
    """'tabloyu' must not reach TP.HPBITABLO1 through its five-letter stem."""
    from backend.tools.lakehouse import discover_concepts
    needs_lakehouse()
    for question in ("bu tabloyu bozmadan grafiğini çiz", "tablo yap", "grafiğini çizer misin"):
        assert discover_concepts(question)["candidates"] == [], question


def test_an_inflected_alias_still_fires():
    """The demo's turn 2 says 'enflasyonDAN' and its deflator must be a candidate."""
    from backend.tools.lakehouse import discover_concepts
    needs_lakehouse()
    keys = [c["key"] for c in discover_concepts(
        "Konut kredisi ve faiz oranları tablosunu bozmadan sadece konut kredisi tutarlarını "
        "enflasyondan arındırır mısın?", limit=12)["candidates"]]
    assert "TP.GENENDEKS.T1" in keys
    assert discover_concepts("enflasyondan arındır")["candidates"][0]["key"] == "TP.GENENDEKS.T1"


def test_a_fresh_question_that_finds_nothing_does_not_chart_the_old_table():
    from backend.agent.pipeline import apply_presentation
    session = Session()
    session.artifact = synthetic("konut")
    fresh = route("zzzqqq serisinin grafiğini çiz", has_artifact=True, client=None)
    assert fresh.wants_chart and not fresh.is_followup
    plan = apply_presentation(template_plan("metadata", "q"), fresh, "q", session)
    assert [s.op for s in plan.steps] == ["discover"]


def _three_unit_session(third_unit="endeks", third_semantics="index"):
    session = Session()
    session.artifact = synthetic("konut", unit="milyon TL")
    faiz = synthetic("faiz", unit="%", semantics="rate")
    session.artifact.add_column("faiz", faiz.frame["faiz"], faiz.lineage["faiz"])
    session.start_turn("kfe ekle ve grafigini ciz")
    kfe = synthetic("kfe", unit=third_unit, semantics=third_semantics)
    session.artifact.add_column("kfe", kfe.frame["kfe"], kfe.lineage["kfe"])
    session.touch_column("kfe")
    session.facts["is_followup"] = True
    session.visible_columns = ["konut", "faiz"]
    return session


def test_a_third_unit_is_rebased_to_100_so_every_column_stays_on_the_chart():
    """TL + % + index is the demo's turn 3. The deck's own chart shows it by
    re-basing the non-percentage series to first month = 100: nothing is
    dropped, the table keeps its units, the answer says what was re-based."""
    session = _three_unit_session()
    Executor(session).run(Plan(intent="followup", steps=[Step(op="chart")]))
    chart, figure = session.facts["chart"], session.facts["figure"]
    assert set(chart["columns"]) == {"konut", "faiz", "kfe"} and not chart.get("dropped")
    by_name = {t["name"]: t for t in figure["data"]}
    assert by_name["Konut (2021-01=100)"]["y"][0] == 100 and by_name["Kfe (2021-01=100)"]["y"][0] == 100
    assert by_name["Konut (2021-01=100)"].get("yaxis", "y") == "y"        # index left, % right
    assert by_name["Faiz"]["yaxis"] == "y2" and by_name["Faiz"]["y"][0] == 100.0  # untouched
    assert figure["layout"]["yaxis"]["title"]["text"] == "endeks (2021-01=100)"
    assert session.artifact.lineage["konut"].unit == "milyon TL"          # the table is not re-based
    session.focus(keep_previous=True)
    assert any("2021-01=100 bazina" in c for c in verify(session)["caveats"])


def test_when_rebasing_cannot_reach_two_units_the_turns_own_column_is_kept_and_the_rest_named():
    """% beside puan beside TL: the two percentage-like units cannot be
    re-based and cannot share an axis, so one still has to go -- the column
    this turn added stays, and what fell off is a caveat."""
    session = _three_unit_session(third_unit="puan", third_semantics="rate")
    Executor(session).run(Plan(intent="followup", steps=[Step(op="chart")]))
    chart = session.facts["chart"]
    assert "kfe" in chart["columns"] and chart["dropped"]
    session.focus(keep_previous=True)
    assert any("Grafikte gosterilmeyen" in c for c in verify(session)["caveats"])


def test_cubuk_grafik_draws_bars():
    session = Session()
    session.artifact = synthetic("konut")
    session.start_turn("bunu çubuk grafik olarak çiz")
    Executor(session).run(Plan(intent="followup", steps=[Step(op="chart")]))
    assert all(trace["type"] == "bar" for trace in session.facts["figure"]["data"])
    assert session.facts["chart"]["kind"] == "bar"


# --- pie charts, explicit clears, and a fresh question that finds nothing ----

def _two_stocks(unit="milyon TL", second_unit=None, n=24):
    session = Session()
    session.artifact = synthetic("tl", n=n, start=300, step=10, unit=unit)
    fx = synthetic("fx", n=n, start=100, step=2, unit=second_unit or unit)
    session.artifact.add_column("fx", fx.frame["fx"], fx.lineage["fx"])
    return session


def _run_chart(session, question, base_period=None):
    session.start_turn(question)
    session.facts["is_followup"] = True
    session.visible_columns = session.artifact.column_names()
    Executor(session).run(Plan(intent="followup", steps=[Step(op="chart", base_period=base_period)]))
    return session.facts["figure"], session.facts["chart"]


def test_a_pie_is_a_snapshot_at_the_last_complete_period_with_shares_computed_in_python():
    figure, chart = _run_chart(_two_stocks(), "bunun pasta grafiğini çiz")
    assert figure["data"][0]["type"] == "pie" and chart["kind"] == "pie"
    assert chart["period"] == "2022-12"                      # 24 months from 2021-01
    tl, fx = 300 + 10 * 23, 100 + 2 * 23
    assert chart["shares"] == {"Tl": f"%{100 * tl / (tl + fx):.1f}".replace(".", ","),
                               "Fx": f"%{100 * fx / (tl + fx):.1f}".replace(".", ",")}


def test_a_pie_at_a_named_month_and_a_month_the_table_lacks():
    figure, chart = _run_chart(_two_stocks(), "2021 Haziran için pasta", base_period="2021-06-01")
    assert figure["data"][0]["type"] == "pie" and chart["period"] == "2021-06"
    figure, chart = _run_chart(_two_stocks(), "pasta", base_period="2030-01-01")
    assert figure["data"][0]["type"] == "scatter" and chart["kind"] == "line"
    assert "2030-01" in chart["note"]


@pytest.mark.parametrize("session, why", [
    (_two_stocks(second_unit="%"), "ayni birimde"),           # mixed units
    (_two_stocks(unit="%"), "oran/endeks"),                   # rates are not parts of a whole
])
def test_a_pie_that_would_lie_becomes_a_line_chart_with_the_reason_as_a_caveat(session, why):
    figure, chart = _run_chart(session, "bunun pasta grafiğini çiz")
    assert figure["data"][0]["type"] == "scatter" and chart["kind"] == "line"
    assert why in chart["note"]
    session.focus(keep_previous=True)
    assert any("Pasta grafigi cizilemedi" in c for c in verify(session)["caveats"])


def test_a_pie_needs_two_columns_and_no_negative_values():
    from backend.tools.charts import PieError, build_chart
    one = synthetic("x")
    with pytest.raises(PieError, match="iki sutun"):
        build_chart(one, kind="pie")
    session = _two_stocks()
    session.artifact.frame.loc[session.artifact.frame.index[-1], "fx"] = -5.0
    with pytest.raises(PieError, match="negatif"):
        build_chart(session.artifact, kind="pie")
    # A NaN in the last month moves the snapshot back to the last complete one.
    session = _two_stocks()
    session.artifact.frame.loc[session.artifact.frame.index[-1], "fx"] = float("nan")
    figure, chart = _run_chart(session, "pasta")
    assert chart["period"] == "2022-11"


@pytest.mark.parametrize("question, month", [
    ("2024-06 için pasta grafiği yap", "2024-06-01"),
    ("2024 Haziran için pasta grafiği yap", "2024-06-01"),
    ("Haziran 2024 pastası", "2024-06-01"),
    ("2021-2025 arası konut kredisi", None),          # two dates: not a single month
    ("pasta grafiği çiz", None),
])
def test_a_single_named_month_is_read_for_a_snapshot(question, month):
    from backend.agent.router import extract_single_month, is_presentation_only
    assert extract_single_month(question) == month
    if month:
        assert is_presentation_only(question)


def test_pasta_is_a_chart_word_only_as_a_chart():
    assert route("bunun pasta grafiğini çiz", client=None).wants_chart
    assert route("2024 Haziran için pasta yap", client=None).wants_chart
    assert not route("kredi pastasından en büyük payı hangi sektör aldı", client=None).wants_chart


@pytest.mark.parametrize("question, clear, only", [
    ("tabloyu temizle", True, True),
    ("baştan başla", True, True),
    ("her şeyi sil", True, True),
    ("tabloyu temizle ve toplam mevduatı göster", True, False),
    ("npl sütununu sil", False, False),
    ("tablodaki npl sütununu sil", False, False),
])
def test_an_explicit_clear_is_deterministic_and_narrow(question, clear, only):
    from backend.agent.pipeline import make_plan
    decided = route(question, has_artifact=True, client=None)
    assert decided.wants_clear is clear
    session = Session()
    session.artifact = synthetic("konut")
    plan = make_plan(question, session, decided, client=None)
    ops = [s.op for s in plan.steps]
    if clear:
        assert ops[0] == "clear_table"
        assert (ops == ["clear_table"]) is only
    else:
        assert "clear_table" not in ops


def test_a_cleared_table_is_reported_as_cleared_not_as_missing_data():
    from backend.agent.composer import deterministic_summary
    session = Session()
    session.artifact = synthetic("konut")
    session.start_turn("tabloyu temizle")
    Executor(session).run(Plan(intent="followup", steps=[Step(op="clear_table")]))
    session.focus(keep_previous=True)
    assert verify(session)["passed"]
    assert deterministic_summary(session, "tabloyu temizle").startswith("Tablo temizlendi")


def test_a_fresh_question_that_produces_nothing_shows_no_table_but_keeps_it_for_the_next_followup():
    session = Session()
    session.artifact = synthetic("konut")
    session.visible_columns = ["konut"]
    session.start_turn("zzz serisini göster")
    assert session.focus(keep_previous=False) == []
    assert session.view().is_empty() and session.shown_empty
    assert session.visible_columns == ["konut"]              # remembered, not shown
    session.start_turn("bunun grafiğini çiz")
    assert session.focus(keep_previous=True) == ["konut"]
    assert not session.view().is_empty()


def test_a_fresh_question_that_produces_nothing_does_not_window_the_shared_table():
    session = Session()
    session.artifact = synthetic("konut", n=60)
    session.start_turn("2024 zzz serisini göster")
    Executor(session).run(Plan(intent="series_analysis", start="2024-01-01", end="2024-12-01",
                               steps=[Step(op="discover", query="zzz")]))
    assert len(session.artifact.frame) == 60