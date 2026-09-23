"""Pipeline regressions: faulty model plans, real archive values and recorded PDF."""
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from backend.agent import pipeline
from backend.agent.planner import Plan, Step
from backend.agent.semantics import QuerySemantics, SemanticInterpretation, explicit_balance_movement
from backend.agent.state import Session
from backend.agent.verifier import quotable_numbers
from backend.eval.run_eval import load_scenarios, score_scenario
from backend.ingestion import external as ingest
from backend.ingestion.external import documents
from backend.lakehouse import external_store as store
from backend.tools import series, web_url
from backend.tools.lakehouse import concept_identity, discover_concepts


GENERIC = ("BDDK Aylık Bülten verilerini kullanarak 202101–202512 döneminde Türk parası (TP) ve "
           "yabancı para (YP) mevduatların gelişimini ve vade yapısını incelemek üzere aylık bir veri seti oluştur.")
FOLLOWUP = ("TP Mevduatları ayrıca açılış vadelerine göre 3 aya kadar ve 3 aydan fazla olacak şekilde "
            "iki grupta topla ve tüm serileri aynı aylık tarih ekseninde hizala.")
PAGE = "https://www.borsaistanbul.com/veriler/kiymetli-madenler-ve-kiymetli-taslar-piyasasi/piyasa-verileri"
PDF = "https://www.borsaistanbul.com/dosyalar/kmtp/veriler/kmp_au.pdf"
MIXED = ("2026-01-01–2026-02-28 döneminde hanehalkının bankalara olan konut kredisi borcu büyürken "
         "Borsa İstanbul'da işlem gören altın miktarı da aynı yönde mi hareket etti? "
         "Konut kredisi ay sonu bakiyesi için sistemdeki BDDK aylık verisini kullan; "
         "TL ve yabancı para toplamını milyon TL olarak al. Altında yalnız TL işlemlerini değil, "
         "tüm para birimlerinin toplam kg miktarını karşılaştır. İlgili belgeyi bu sayfadaki bağlantılar "
         f"arasından kendin bul: {PAGE} "
         "Tek bir tabloda iki ayın ham değerlerini ve her iki serinin Ocak=100 endeksini göster. "
         "Bu iki ölçünün stok/akım farkını açıkla; aralarında nedensellik iddia etme.")
TP = "tp_mevduat_katilim_fonlari_yurt_ici_yerlesik"
YP = "doviz_tevdiat_hesabi_katilim_fonlari_yurt_ici_yerlesik"
# "3 aya kadar" is the first three (demand deposits included), "3 aydan fazla" the last three.
BUCKETS = ["vadesiz", "bir_aya_kadar", "bir_ay_uc_ay", "uc_ay_alti_ay", "alti_ay_bir_yil", "bir_yil"]
SHORT, LONG = BUCKETS[:3], BUCKETS[3:]


class Model:
    def __init__(self, plan, **semantics):
        self.plan = plan
        self.semantics = QuerySemantics(**semantics)

    def structured(self, messages, schema, **kwargs):
        if schema is SemanticInterpretation:
            return SemanticInterpretation(output_requirement="Requested measure and period",
                                          semantics=self.semantics.model_dump())
        if schema is Plan:
            return self.plan.model_copy(deep=True)
        return schema(intent="series_analysis", reason="model route")

    def chat(self, *args, **kwargs):
        raise AssertionError("Grouped explanations must be rendered from actual lineage")


def totals():
    # Both aliases intentionally point at the same wrong total.
    return [Step(op="fetch_series", source="bulletin", dataset="mevduat_vade", key="toplam_mevduat",
                 metric="toplam", as_name=name) for name in ("tp_toplam", "yp_toplam")]


def groups():
    return [Step(op="fetch_series", source="bulletin", dataset="mevduat_vade", key="toplam_mevduat",
                 metric=b, as_name="tp_" + b) for b in BUCKETS] + [
        Step(op="transform", operation="sum_columns", columns=["tp_" + b for b in SHORT], as_name="short_term"),
        Step(op="transform", operation="sum_columns", columns=["tp_" + b for b in LONG], as_name="long_term")]


def test_generic_currency_roles_repair_real_identities_and_values():
    found = discover_concepts(GENERIC, limit=12)
    assert {c["key"] for c in found["currency_roles"].values()} == {TP, YP}
    result = pipeline.run_turn(GENERIC, client=Model(Plan(intent="series_analysis", steps=totals()),
                               requested_output="level", currencies=["TL", "FX"]), compose_answer=False)
    assert result["table"]["columns"] == ["tp_toplam", "yp_toplam"]
    assert len(result["table"]["rows"]) == 60
    identities = result["session"].facts["currency_role_identities"]
    assert identities["TL"] != identities["FX"]
    for alias, key in [("tp_toplam", TP), ("yp_toplam", YP)]:
        gold = series.load_series(key, dataset="mevduat_vade", metric="toplam", start="2021-01-01", end="2025-12-01")
        pd.testing.assert_series_equal(result["session"].view().frame[alias], gold.values, check_names=False)
    assert [r["tp_toplam"] for r in result["table"]["rows"][:3]] == [1492502., 1525291., 1586313.]
    assert [r["yp_toplam"] for r in result["table"]["rows"][:3]] == [1438605., 1439464., 1545497.]
    assert len(result["plan"]["steps"]) == 2


def test_currency_row_repairs_preserve_unrelated_measures_from_same_source():
    found = discover_concepts(GENERIC, limit=12)
    loan = dict(source="bulletin", dataset="tuketici_kredileri", key="tuketici_kredileri_konut",
                currency="TL", name="Konut kredileri")
    found["candidates"].append(loan)
    found["by_concept"].append([concept_identity(loan)])
    plan = Plan(intent="series_analysis", steps=totals() + [Step(op="fetch_series",
        source=loan["source"], dataset=loan["dataset"], key=loan["key"], as_name="tp_konut")])
    pipeline.apply_currency_roles(plan, found, Session(), False)
    pipeline.apply_dimensions(plan, found)
    housing = next(s for s in plan.steps if s.as_name == "tp_konut")
    assert housing.key == loan["key"] and housing.dataset == loan["dataset"]
    assert housing.currency == "TL"
    assert {s.key for s in plan.steps[:2]} == {TP, YP}


def test_eval_accepts_yaml_dates_and_reports_deterministic_plan_origin():
    result = pipeline.run_turn(GENERIC, client=None, compose_answer=False)
    assert {s["key"] for s in result["plan"]["steps"] if s["op"] == "fetch_series"} == {TP, YP}
    assert len(result["table"]["columns"]) == 2
    scenario = dict(id="window", expect_window=[date(2021, 1, 1), date(2025, 12, 1)])
    checks = score_scenario(scenario, result, 0)["checks"]
    assert checks["window"]
    assert not checks["plan_validity"]


def test_explicit_stock_rejection_is_enforced_without_a_model():
    q = next(s["question"] for s in load_scenarios() if s["id"] == "mentor_tp_yp_maturity")
    result = pipeline.run_turn(q, client=None, compose_answer=False)
    assert result["semantics"]["requested_output"] == "absolute_change"
    assert result["session"].facts["semantic_source"] == "fallback_explicit_movement"
    assert all(result["session"].view().lineage[c].temporal_semantics == "net_change"
               for c in result["table"]["columns"])
    assert score_scenario(dict(id="mentor", expect_semantics=["net_change"]), result, 0)["checks"]["semantics"]
    assert not explicit_balance_movement(q + " Brüt yeni mevduat girişi istiyorum.")


def test_maturity_followup_preserves_totals_window_and_exact_group_membership():
    session = Session()
    pipeline.run_turn(GENERIC, session, client=Model(Plan(intent="series_analysis", steps=totals()),
                      requested_output="level", currencies=["TL", "FX"]), compose_answer=False)
    previous = session.view().frame.copy()
    plan = Plan(intent="followup", start="2025-01-01", steps=totals() + groups())
    result = pipeline.run_turn(FOLLOWUP, session, client=Model(plan, requested_output="level",
        currencies=["TL"], interaction="extend_previous", preserve_existing_window=True))
    assert result["table"]["columns"] == ["tp_toplam", "yp_toplam", "short_term", "long_term"]
    pd.testing.assert_frame_equal(session.view().frame[previous.columns], previous)
    assert len(result["table"]["rows"]) == 60
    fetches = [s for s in result["plan"]["steps"] if s["op"] == "fetch_series"]
    assert len(fetches) == 6 and {s["key"] for s in fetches} == {TP}
    for name, buckets in [("short_term", SHORT), ("long_term", LONG)]:
        gold = sum(series.load_series(TP, dataset="mevduat_vade", metric=b,
                   start="2021-01-01", end="2025-12-01").values for b in buckets)
        pd.testing.assert_series_equal(session.view().frame[name], gold, check_names=False)
        assert [i["metric"] for i in quotable_numbers(session)["group_definitions"][name]["inputs"]] == buckets
    # The two groups partition the line: their sum is the published total.
    frame = session.view().frame
    pd.testing.assert_series_equal(frame["short_term"] + frame["long_term"], frame["tp_toplam"],
                                   check_names=False)


def test_followup_removes_published_total_from_its_component_sum():
    session = Session()
    pipeline.run_turn(GENERIC, session, client=Model(Plan(intent="series_analysis", steps=totals()),
                      requested_output="level", currencies=["TL", "FX"]), compose_answer=False)
    previous = session.view().frame.copy()
    steps = groups()
    steps[-1].columns.append("tp_toplam")  # The real live planner double-counted this total.
    result = pipeline.run_turn(FOLLOWUP, session, client=Model(
        Plan(intent="followup", steps=steps), requested_output="level", currencies=["TL"],
        interaction="extend_previous", preserve_existing_window=True), compose_answer=False)
    assert result["table"]["columns"] == ["tp_toplam", "yp_toplam", "short_term", "long_term"]
    pd.testing.assert_frame_equal(session.view().frame[previous.columns], previous)
    long = next(s for s in result["plan"]["steps"] if s.get("as_name") == "long_term")
    assert long["columns"] == ["tp_" + b for b in LONG]
    gold = sum(series.load_series(TP, dataset="mevduat_vade", metric=b,
               start="2021-01-01", end="2025-12-01").values for b in LONG)
    pd.testing.assert_series_equal(session.view().frame["long_term"], gold, check_names=False)


def test_followup_keeps_demand_deposits_in_the_short_group_and_hides_old_bucket_inputs():
    session = Session()
    metrics = ["toplam", *BUCKETS]
    initial = [Step(op="fetch_series", source="bulletin", dataset="mevduat_vade", key=key,
                    metric=metric, as_name=f"{prefix}_{metric}")
               for prefix, key in [("tp", TP), ("yp", YP)] for metric in metrics]
    pipeline.run_turn(GENERIC, session, client=Model(Plan(intent="series_analysis", steps=initial),
                      requested_output="level", currencies=["TL", "FX"]), compose_answer=False)
    before = session.view().frame[["tp_toplam", "yp_toplam"]].copy()
    sums = [Step(op="transform", operation="sum_columns",
                 columns=["tp_vadesiz", "tp_bir_aya_kadar", "tp_bir_ay_uc_ay"], as_name="short_term"),
            Step(op="transform", operation="sum_columns",
                 columns=["tp_uc_ay_alti_ay", "tp_alti_ay_bir_yil", "tp_bir_yil"], as_name="long_term")]
    result = pipeline.run_turn(FOLLOWUP, session, client=Model(Plan(intent="followup", steps=sums),
        requested_output="level", currencies=["TL"], interaction="extend_previous",
        preserve_existing_window=True), compose_answer=False)
    assert result["table"]["columns"] == ["tp_toplam", "yp_toplam", "short_term", "long_term"]
    pd.testing.assert_frame_equal(session.view().frame[before.columns], before)
    short = next(s for s in result["plan"]["steps"] if s.get("as_name") == "short_term")
    assert short["columns"] == ["tp_vadesiz", "tp_bir_aya_kadar", "tp_bir_ay_uc_ay"]
    assert "tp_vadesiz" in session.artifact.frame and "yp_vadesiz" in session.artifact.frame
    gold = sum(series.load_series(TP, dataset="mevduat_vade", metric=b,
               start="2021-01-01", end="2025-12-01").values for b in SHORT)
    pd.testing.assert_series_equal(session.view().frame["short_term"], gold, check_names=False)


def test_followup_repairs_colliding_metric_aliases_from_model_plan():
    session = Session()
    pipeline.run_turn(GENERIC, session, client=Model(Plan(intent="series_analysis", steps=totals()),
                      requested_output="level", currencies=["TL", "FX"]), compose_answer=False)
    previous = session.view().frame.copy()
    steps = [Step(op="fetch_series", source="bulletin", dataset="mevduat_vade", key=TP,
                  metric=metric, as_name="tp_3aya_kadar" if metric in SHORT else "tp_3aydan_fazla")
             for metric in BUCKETS]
    steps.append(Step(op="transform", operation="sum_columns",
                      columns=["tp_3aya_kadar", "tp_3aydan_fazla"], as_name="tp_3aya_kadar"))
    result = pipeline.run_turn(FOLLOWUP, session, client=Model(Plan(intent="followup", steps=steps),
        requested_output="level", currencies=["TL"], interaction="extend_previous",
        preserve_existing_window=True), compose_answer=False)
    assert result["table"]["columns"] == ["tp_toplam", "yp_toplam", "tl_3aya_kadar", "tl_3aydan_fazla"]
    pd.testing.assert_frame_equal(session.view().frame[previous.columns], previous)
    for name, buckets in [("tl_3aya_kadar", SHORT), ("tl_3aydan_fazla", LONG)]:
        gold = sum(series.load_series(TP, dataset="mevduat_vade", metric=b,
                   start="2021-01-01", end="2025-12-01").values for b in buckets)
        pd.testing.assert_series_equal(session.view().frame[name], gold, check_names=False)
    assert len([s for s in result["plan"]["steps"] if s.get("operation") == "sum_columns"]) == 2


def test_complete_split_with_unnamed_sums_gets_group_names_not_input_lists():
    # Measured live: the model fetched all six buckets and summed them
    # correctly but left both sums unnamed, so the executor called the column
    # `tl_vadesiz_plus_tl_1ay_plus_tl_1_3ay`.
    q = next(s["question"] for s in load_scenarios() if s["id"] == "mentor_tp_yp_maturity")
    steps = [Step(op="fetch_series", source="bulletin", dataset="mevduat_vade", key=TP,
                  metric=b, as_name="tl_" + b) for b in BUCKETS]
    steps += [Step(op="transform", operation="sum_columns", columns=["tl_" + b for b in SHORT]),
              Step(op="transform", operation="sum_columns", columns=["tl_" + b for b in LONG])]
    result = pipeline.run_turn(q, client=Model(Plan(intent="series_analysis", steps=steps),
                               requested_output="absolute_change", currencies=["TL"]), compose_answer=False)
    assert result["table"]["columns"] == ["tl_3aya_kadar_net", "tl_3aydan_fazla_net"]
    assert len([s for s in result["plan"]["steps"] if s["op"] == "fetch_series"]) == 6


def test_long_mentor_omitted_differences_and_false_reasoning_are_repaired():
    q = next(s["question"] for s in load_scenarios() if s["id"] == "mentor_tp_yp_maturity")
    result = pipeline.run_turn(q, client=Model(Plan(intent="series_analysis", steps=totals() + groups(),
        reasoning="No transformations needed"), requested_output="absolute_change", currencies=["TL", "FX"]))
    assert len(result["table"]["columns"]) == 4
    assert all(c.endswith("_net") for c in result["table"]["columns"])
    assert len(result["table"]["rows"]) == 60
    assert "net bakiye değişimi" in result["summary"]
    assert "No transformations needed" not in result["plan"]["reasoning"]
    assert result["plan_diagnostics"]["raw_model_reasoning"] == "No transformations needed"
    assert len([s for s in result["plan"]["steps"] if s.get("operation") == "net_change"]) == 4
    assert len([s for s in result["plan"]["steps"] if s["op"] == "fetch_series"]) == 8


def test_flow_like_classification_with_explicit_stock_rejection_gets_net_changes():
    class FlowLikeModel(Model):
        def structured(self, messages, schema, **kwargs):
            answer = super().structured(messages, schema, **kwargs)
            if schema is SemanticInterpretation:
                answer.rejects_stock_levels = True
                answer.requires_gross_transactions = False
            return answer
    result = pipeline.run_turn(GENERIC, client=FlowLikeModel(
        Plan(intent="series_analysis", steps=totals()), requested_output="flow"), compose_answer=False)
    assert result["semantics"]["requested_output"] == "absolute_change"
    assert result["table"]["columns"] == ["tp_toplam_net", "yp_toplam_net"]
    assert result["verification"]["passed"]


def test_new_analysis_cannot_read_an_unfetched_stale_column():
    session = Session()
    pipeline.run_turn(GENERIC, session, client=Model(Plan(intent="series_analysis", steps=totals()),
                      requested_output="level"), compose_answer=False)
    plan = Plan(intent="series_analysis", steps=[Step(op="transform", operation="index_to_base",
                column="yp_toplam", as_name="stale_index")])
    result = pipeline.run_turn("Independent new analysis", session, client=Model(plan), compose_answer=False)
    assert "yp_toplam" not in result["table"]["columns"]
    assert "stale_index" not in result["table"]["columns"]
    assert not result["audit"][-1]["ok"]


# --- turn 2 of the mentor conversation: CPI, aligned, at 202512 prices -------

DEFLATE_Q = ("TCMB EVDS üzerinden 202101–202512 dönemine ait aylık TÜFE Genel Endeks verisini getir, "
             "mevcut veri setiyle tarih bazında hizala ve mevduat tutarlarını 202512 fiyatlarıyla reel "
             "hale getirerek enflasyon etkisinden arındır.")
NET = ["tl_toplam_net", "yp_toplam_net", "tl_3aya_kadar_net", "tl_3aydan_fazla_net"]
CPI = "TP.GENENDEKS.T1"


def _maturity_table() -> Session:
    """Turn 1 the way the live model plans it: a complete split, named net changes."""
    steps = [Step(op="fetch_series", source="bulletin", dataset="mevduat_vade", key=key, metric="toplam", as_name=name)
             for key, name in [(TP, "tl_toplam"), (YP, "yp_toplam")]]
    steps += [Step(op="fetch_series", source="bulletin", dataset="mevduat_vade", key=TP, metric=b, as_name="tl_" + b)
              for b in BUCKETS]
    steps += [Step(op="transform", operation="sum_columns", columns=["tl_" + b for b in SHORT], as_name="tl_3aya_kadar"),
              Step(op="transform", operation="sum_columns", columns=["tl_" + b for b in LONG], as_name="tl_3aydan_fazla")]
    steps += [Step(op="transform", operation="net_change", column=c[:-4], as_name=c) for c in NET]
    session = Session()
    q = next(s["question"] for s in load_scenarios() if s["id"] == "mentor_tp_yp_maturity")
    pipeline.run_turn(q, session, client=Model(Plan(intent="series_analysis", steps=steps),
                      requested_output="absolute_change", currencies=["TL", "FX"]), compose_answer=False)
    assert session.view().column_names() == NET
    return session


def _real_gold(nominal: pd.Series, base: str = "2025-12-01") -> pd.Series:
    """nominal_t * CPI_base / CPI_t, from the lakehouse in SQL."""
    from backend.core.config import DUCKDB_PATH
    with duckdb.connect(str(DUCKDB_PATH), read_only=True) as con:
        cpi = con.execute("SELECT period, value FROM macro_observations WHERE series_code=? ORDER BY period",
                          [CPI]).df()
    cpi = cpi.set_index(pd.to_datetime(cpi["period"]))["value"]
    return nominal * (cpi[pd.Timestamp(base)] / cpi.reindex(nominal.index))


def _followup(plan: Plan):
    return Model(plan, requested_output="absolute_change", interaction="extend_previous", preserve_existing_window=True)


@pytest.mark.parametrize("phrase,expected", [
    ("202512 fiyatlarıyla reel hale getir", "2025-12-01"),
    ("2025-12 fiyatlariyla", "2025-12-01"),
    ("2025 Aralık fiyatlarıyla", "2025-12-01"),
    ("Aralık 2025 sabit fiyatlarıyla", "2025-12-01"),
    ("2024 yılı sonu fiyatlarıyla", "2024-12-01"),
    ("son ay fiyatlarıyla", "end"),
    ("güncel fiyatlarla göster", "end"),
    ("enflasyondan arındır", None),
])
def test_base_prices_phrase_is_read_in_python(phrase, expected):
    from backend.agent.router import extract_base_period, wants_deflation
    assert extract_base_period(phrase) == expected
    assert wants_deflation(phrase)
    assert not wants_deflation("reel faiz oranı")       # a concept, not a request


def test_deflation_question_is_a_followup_that_names_its_base_month():
    from backend.agent.router import route
    r = route(DEFLATE_Q, has_artifact=True)
    assert r.is_followup and r.base_period == "2025-12-01" and r.wants_deflation
    assert (r.start, r.end) == ("2021-01-01", "2025-12-01")


def test_self_overwriting_deflate_from_the_model_gets_its_own_column():
    # Measured live: the model named every deflate output after its input, the
    # nominal column was replaced in place, no column was left to show and
    # the composer said the CPI series did not exist.
    session = _maturity_table()
    plan = Plan(intent="followup", steps=[Step(op="fetch_series", source="macro", key=CPI, as_name="tufe")] + [
        Step(op="transform", operation="deflate", column=c, other_column="tufe", base_period="2025-12", as_name=c)
        for c in NET])
    assert all(s.as_name is None for s in plan.steps if s.operation == "deflate")
    result = pipeline.run_turn(DEFLATE_Q, session, client=_followup(plan), compose_answer=False)
    assert result["table"]["columns"] == ["tufe"] + [c + "_reel" for c in NET]
    assert len(result["table"]["rows"]) == 60 and result["verification"]["passed"]
    frame = session.view().frame
    for c in NET:
        assert session.artifact.lineage[c + "_reel"].transform == f"deflate({c}, by=tufe, base=2025-12)"
        pd.testing.assert_series_equal(frame[c + "_reel"], _real_gold(session.artifact.frame[c]), check_names=False)
    # At the base month real equals nominal; the nominal columns are still there for the audit.
    assert frame.loc["2025-12-01", "tl_toplam_net_reel"] == session.artifact.frame.loc["2025-12-01", "tl_toplam_net"]
    assert all(c in session.artifact.frame for c in NET)


def test_router_base_month_beats_the_models_missing_one():
    session = _maturity_table()
    plan = Plan(intent="followup", steps=[
        Step(op="fetch_series", source="macro", key=CPI, as_name="tufe"),
        Step(op="transform", operation="deflate", column="tl_toplam_net", other_column="tufe", as_name="tl_reel")])
    result = pipeline.run_turn(DEFLATE_Q, session, client=_followup(plan), compose_answer=False)
    step = next(s for s in result["plan"]["steps"] if s.get("operation") == "deflate")
    assert step["base_period"] == "2025-12-01"          # not the window's first month, the executor's default
    pd.testing.assert_series_equal(session.view().frame["tl_reel"],
                                   _real_gold(session.artifact.frame["tl_toplam_net"]), check_names=False)
    assert "tufe" in result["table"]["columns"]          # the index the question asked for stays visible


def test_deflation_is_guaranteed_without_a_model():
    session = _maturity_table()
    result = pipeline.run_turn(DEFLATE_Q, session, client=None, compose_answer=False)
    deflates = [s for s in result["plan"]["steps"] if s.get("operation") == "deflate"]
    assert {s["column"] for s in deflates} == set(NET)
    assert {s["base_period"] for s in deflates} == {"2025-12-01"}
    fetched = [s["key"] for s in result["plan"]["steps"] if s["op"] == "fetch_series"]
    assert fetched == [CPI]                              # "mevcut veri setiyle" must not fetch "Mevcut Durum"
    assert set(result["table"]["columns"]) == {"TP_GENENDEKS_T1"} | {c + "_reel" for c in NET}
    assert result["verification"]["passed"]
    pd.testing.assert_series_equal(session.view().frame["tl_3aya_kadar_net_reel"],
                                   _real_gold(session.artifact.frame["tl_3aya_kadar_net"]), check_names=False)


def test_period_stamps_in_prose_are_not_unsupported_numbers():
    from backend.agent.verifier import unsupported_numbers
    assert unsupported_numbers("202101–202512 dönemi, 202512 fiyatlarıyla; 2023 yılı", {}) == []
    assert unsupported_numbers("tutar 202513 oldu", {}) == [202513.0]


def test_generated_group_title_cannot_claim_unexecuted_bucket():
    steps = totals() + groups()
    steps[-2].title = "bir_yil dahil"
    result = pipeline.run_turn(GENERIC + FOLLOWUP, client=Model(
        Plan(intent="series_analysis", steps=steps), requested_output="level"))
    title = next(s["title"] for s in result["plan"]["steps"] if s.get("as_name") == "short_term")
    assert "bir_yil" not in title
    assert all(metric in title for metric in SHORT)


@pytest.fixture
def external_zone(tmp_path, monkeypatch):
    root = tmp_path / "external"
    monkeypatch.setattr(store, "EXTERNAL_DIR", root)
    monkeypatch.setattr(store, "EXTERNAL_RAW_DIR", root / "_raw")
    monkeypatch.setattr(store, "EXTERNAL_SEED_DIR", root / "_seed")
    monkeypatch.setenv("WEB_ASSET_CACHE_DIR", str(tmp_path / "cache"))
    documents.configure_tools({})
    store.seed()
    database = tmp_path / "external.duckdb"
    with duckdb.connect(str(database)) as con:
        store.create_views(con)
    real_fetch = series.load_series

    def fetch(key, **kwargs):
        if kwargs.get("source") != "external":
            return real_fetch(key, **kwargs)
        with monkeypatch.context() as m:
            m.setattr(series, "DUCKDB_PATH", database)
            return real_fetch(key, **kwargs)
    monkeypatch.setattr("backend.agent.executor.fetch_series", fetch)
    real_ingest = ingest.ingest_url
    monkeypatch.setattr(pipeline, "ingest_url", lambda url, hint=None, client=None:
                        real_ingest(url, hint=hint, verify_against_lakehouse=False))
    pdf = (Path(__file__).parent / "fixtures/external/bist_kmp_au_2026.pdf").read_bytes()
    html = f'<html><table><tr><td><a href="{PDF}">Altın İşlemleri</a></td></tr></table></html>'.encode()
    urls = []

    def serve(url):
        urls.append(url)
        body, kind = (html, "text/html") if url == PAGE else (pdf, "application/pdf")
        assert url in (PAGE, PDF)
        return SimpleNamespace(content=body, headers={"Content-Type": kind}, url=url)
    monkeypatch.setattr(web_url, "_fetch", serve)
    yield database, urls
    documents.configure_tools(None)


def mixed_plan():
    return Plan(intent="series_analysis", steps=[
        Step(op="fetch_series", source="bulletin", dataset="tuketici_kredileri", key="tuketici_kredileri_konut",
             currency="total", as_name="housing"),
        Step(op="fetch_series", source="bulletin", dataset="menkul_degerler", key="altin_tahvili_tl", as_name="altin"),
        Step(op="transform", operation="index_to_base", column="housing", as_name="housing_index"),
        Step(op="transform", operation="index_to_base", column="altin", as_name="gold_index")])


def test_mixed_bist_source_uses_link_pdf_totals_and_no_stale_columns(external_zone):
    session = Session()
    pipeline.run_turn(GENERIC, session, client=Model(Plan(intent="series_analysis", steps=totals()),
                      requested_output="level", currencies=["TL", "FX"]), compose_answer=False)
    result = pipeline.run_turn(MIXED, session, client=Model(mixed_plan(), requested_output="level"))
    assert result["table"]["columns"] == ["housing", "altin", "housing_index", "gold_index"]
    rows = result["table"]["rows"]
    assert len(rows) == 2
    assert [r["housing"] for r in rows] == [691343., 715799.]
    assert [r["altin"] for r in rows] == [33584., 32719.]
    assert [r["housing_index"] for r in rows] == pytest.approx([100., 103.53746], abs=.0001)
    assert [r["gold_index"] for r in rows] == pytest.approx([100., 97.42437], abs=.0001)
    assert session.view().lineage["housing"].temporal_semantics == "stock"
    assert session.view().lineage["altin"].temporal_semantics == "flow"
    assert "zıt yönlerde" in result["summary"] and "nedensellik göstermez" in result["summary"]
    database, urls = external_zone
    assert urls == [PAGE, PDF]
    line = session.view().lineage["altin"]
    assert line.citation["url"] == PDF and "1" in line.citation["location"]
    with duckdb.connect(str(database), read_only=True) as con:
        assert con.execute("SELECT value FROM external_observations WHERE series_key=? ORDER BY period LIMIT 2",
                           [line.key]).fetchall() == [(33584.,), (32719.,)]


def test_failed_external_acquisition_never_substitutes_local_gold(monkeypatch):
    monkeypatch.setattr(pipeline, "ingest_url", lambda url, **kwargs:
        ingest.IngestResult(source_id="missing", url=url, status="error", error="unavailable"))
    result = pipeline.run_turn(MIXED, client=Model(mixed_plan(), requested_output="level"),
                               compose_answer=False, url_reader=lambda url: {"text": "unavailable"})
    assert result["table"]["columns"] == ["housing", "housing_index"]
    assert not any(s.get("key") == "altin_tahvili_tl" for s in result["plan"]["steps"])
    assert any("dış kaynak" in c for c in result["verification"]["caveats"])
    assert not result["verification"]["passed"]
