"""Offline enforcement contracts plus opt-in, real-model paraphrase evaluation.

KKB_LIVE_SEMANTICS=1 pytest -q tests/test_semantics.py -k live
The live cases are held out of the interpreter prompt and contain no mocked
semantic answers. Offline tests separately exercise consumption of that answer.
"""
import json
import logging
import os

import pandas as pd
import pytest

from backend.agent import pipeline
from backend.agent.composer import deterministic_summary
from backend.agent.executor import Executor
from backend.agent.planner import Plan, Step
from backend.agent.router import Route
from backend.agent.semantics import QuerySemantics, SemanticInterpretation, SEMANTICS_SYSTEM, parse_query_semantics
from backend.agent.state import AnalysisArtifact, ColumnLineage, Session
from backend.agent.verifier import verify
from backend.llm import KloudeksClient, LLMError
from backend.tools.series import SeriesResult


OUTPUT_CASES = [
    ("aylık bakiye farklarını göster", "absolute_change"),
    ("her ay oluşan net değişimi getir", "absolute_change"),
    ("show month-to-month balance changes", "absolute_change"),
    ("mevduat bakiyelerini göster", "level"),
    ("aylık yüzde değişimi", "percent_change"),
    ("o ay yatırılan yeni mevduat", "flow"),
    ("stok veri olmamalı, aylık değişimi göster", "absolute_change"),
    ("levels değil, değişimi istiyorum", "absolute_change"),
    ("show the outstanding balance", "level"),
    ("month-over-month percentage change", "percent_change"),
    ("gross inflows during each month", "flow"),
    ("monthly new deposits", "flow"),
]
FOLLOWUP_CASES = [
    "Bunun yanına ilgili faiz serisini de koy.",
    "Include the matching rate without changing the current period.",
]


def test_semantic_schema_requires_explicit_model_decisions_without_data_identities():
    schema = QuerySemantics.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"]) == {
        "interaction", "requested_output", "requested_basis", "frequency", "currencies",
        "preserve_existing_window",
    }
    first, second = QuerySemantics(), QuerySemantics()
    first.currencies.append("TL")
    assert second.currencies == []


class SemanticPlanClient:
    """Fixed structured responses, independent of wording; records all calls."""
    def __init__(self, semantics, plan=None):
        self.semantics = semantics
        self.plan = plan or Plan(intent="series_analysis", steps=[])
        self.calls = []

    def structured(self, messages, schema, **kwargs):
        self.calls.append((messages, schema))
        if schema is SemanticInterpretation:
            return SemanticInterpretation(output_requirement="Interpreted output constraint",
                                          semantics=self.semantics.model_dump())
        if schema is Plan:
            return self.plan.model_copy(deep=True)
        return schema(intent="series_analysis", reason="test classifier")


@pytest.fixture
def source(monkeypatch):
    values = pd.Series([100., 120., 150., 180.], index=pd.date_range("2020-12-01", periods=4, freq="MS"))
    candidate = dict(source="bulletin", dataset="sample", key="balance", name="Balance",
                     unit="bin TL", temporal_semantics="stock")
    found = {"candidates": [candidate], "by_concept": []}
    monkeypatch.setattr(pipeline, "discover_concepts", lambda *a, **k: found)
    calls = []

    def fetch(key, **kw):
        calls.append(kw)
        return SeriesResult(values.loc[kw.get("start"):kw.get("end")], "bulletin", key,
                            candidate["name"], candidate["unit"], candidate["temporal_semantics"],
                            "value", dataset="sample", currency="total")
    monkeypatch.setattr("backend.agent.executor.fetch_series", fetch)
    plan = Plan(intent="series_analysis", start="2021-01-01", end="2021-03-01", steps=[
        Step(op="fetch_series", source="bulletin", dataset="sample", key="balance", as_name="balance")])
    return candidate, found, plan, calls


@pytest.mark.parametrize("question,expected", OUTPUT_CASES)
def test_structured_intent_reaches_enforcement(question, expected, source, caplog):
    _, _, plan, _ = source
    client = SemanticPlanClient(QuerySemantics(requested_output=expected), plan)
    with caplog.at_level(logging.INFO, logger="kkb.agent"):
        result = pipeline.run_turn(question, client=client, compose_answer=False)
    semantic_call = next(m for m, schema in client.calls if schema is SemanticInterpretation)
    assert json.loads(semantic_call[-1]["content"])["question"] == question
    assert question not in SEMANTICS_SYSTEM  # held-out wording, not a prompt lookup table
    assert result["semantics"]["requested_output"] == expected
    operations = [s.get("operation") for s in result["plan"]["steps"]]
    assert ("net_change" in operations) == (expected == "absolute_change")
    assert ("change" in operations) == (expected == "percent_change")
    assert result["semantic_status"] == ("unsupported" if expected == "flow" else "enforced")
    semantic_logs = [r.message for r in caplog.records if r.message.startswith("semantic ->")]
    assert len(semantic_logs) == 1
    assert caplog.text.index("semantic ->") < caplog.text.index("plan ->")
    plan_context = next(m for m, schema in client.calls if schema is Plan)
    assert expected in str(plan_context)


@pytest.mark.parametrize("operation,expected", [("absolute_change", [20., 30., 30.]),
                                               ("percent_change", [20., 25., 20.])])
def test_differences_fetch_lag_before_clipping_display_window(operation, expected, source):
    _, _, plan, calls = source
    result = pipeline.run_turn("Balance movements", client=SemanticPlanClient(
        QuerySemantics(requested_output=operation), plan), compose_answer=False)
    assert calls[0]["start"] == "2020-12-01"
    column, = result["table"]["columns"]
    assert [r[column] for r in result["table"]["rows"]] == pytest.approx(expected)
    assert result["table"]["rows"][0]["period"] == "2021-01-01"
    assert result["verification"]["passed"]


@pytest.mark.parametrize("wrong_difference", [None, "net_change", "change"])
def test_flow_from_stock_is_explicitly_unsupported_and_never_charted(wrong_difference, source):
    _, _, plan, _ = source
    if wrong_difference:
        plan.steps.append(Step(op="transform", operation=wrong_difference, column="balance", as_name="new_deposits"))
    result = pipeline.run_turn("Yeni mevduatı grafik olarak göster", client=SemanticPlanClient(
        QuerySemantics(requested_output="flow"), plan), compose_answer=False)
    assert not any(s.get("operation") in ("change", "net_change") for s in result["plan"]["steps"])
    assert result["table"]["columns"] == []
    assert result["figure"] is None
    assert not result["verification"]["passed"]
    assert any("brüt akış değildir" in c for c in result["verification"]["caveats"])
    assert "brüt akış değildir" in deterministic_summary(result["session"], "")


def test_published_flow_is_preserved(source):
    candidate, _, plan, _ = source
    candidate["temporal_semantics"] = "flow"
    result = pipeline.run_turn("Gross receipts", client=SemanticPlanClient(
        QuerySemantics(requested_output="flow"), plan), compose_answer=False)
    assert result["table"]["columns"] == ["balance"]
    assert result["verification"]["passed"]
    assert not any(s.get("operation") for s in result["plan"]["steps"])


@pytest.mark.parametrize("basis,unit", [("stock", "bin TL"), ("flow", "USD"), ("rate", "%"),
                                       ("index", "endeks"), ("ratio", "%"), ("cumulative_ytd", "bin TL")])
def test_percentage_change_compatibility_uses_metadata(basis, unit, source):
    candidate, found, plan, _ = source
    candidate.update(temporal_semantics=basis, unit=unit)
    session = Session()
    semantics = QuerySemantics(requested_output="percent_change")
    pipeline.apply_output_semantics(plan, semantics, session, found)
    pipeline.apply_output_semantics(plan, semantics, session, found)
    operations = [s.operation for s in plan.steps if s.op == "transform"]
    assert operations == ([] if basis == "cumulative_ytd" else ["change"])
    assert session.facts["semantic_status"] == ("unsupported" if basis == "cumulative_ytd" else "enforced")


@pytest.mark.parametrize("requested,expected", [("level", None), ("percent_change", "change"),
                                               ("absolute_change", "net_change")])
def test_corrects_conflicting_difference_and_is_idempotent(requested, expected, source):
    _, found, plan, _ = source
    plan.steps.append(Step(op="transform", operation="net_change" if requested != "absolute_change" else "change",
                           column="balance", as_name="delta"))
    session = Session()
    semantics = QuerySemantics(requested_output=requested)
    pipeline.apply_output_semantics(plan, semantics, session, found)
    pipeline.apply_output_semantics(plan, semantics, session, found)
    assert [s.operation for s in plan.steps if s.op == "transform"] == ([expected] if expected else [])
    Executor(session).run(plan)
    session.focus()
    assert session.view().column_names() == (["delta"] if expected else ["balance"])
    assert verify(session)["passed"]


def existing_session():
    artifact = AnalysisArtifact()
    artifact.add_column("previous", pd.Series([1., 2., 3.], index=pd.date_range("2021-01-01", periods=3, freq="MS")),
                        ColumnLineage(column="previous", label="Deposits", source="bulletin", unit="bin TL",
                                      temporal_semantics="stock", citation={"table": "bulletin_observations"}))
    return Session(artifact=artifact, visible_columns=["previous"])


@pytest.mark.parametrize("question", FOLLOWUP_CASES)
def test_structured_followup_overrides_legacy_router_and_keeps_window(question, source, monkeypatch):
    _, _, plan, calls = source
    plan.start, plan.end = "2020-01-01", "2025-12-01"  # deliberately wrong model window
    monkeypatch.setattr(pipeline, "route", lambda *a, **k: Route(intent="series_analysis"))
    semantics = QuerySemantics(interaction="extend_previous", preserve_existing_window=True)
    result = pipeline.run_turn(question, existing_session(), client=SemanticPlanClient(semantics, plan), compose_answer=False)
    assert result["route"]["is_followup"]
    assert result["table"]["columns"] == ["previous", "balance"]
    assert len(result["table"]["rows"]) == 3
    assert (calls[0]["start"], calls[0]["end"]) == ("2021-01-01", "2021-03-01")


def test_semantic_new_analysis_overrides_regex_followup(source, monkeypatch):
    _, _, plan, _ = source
    monkeypatch.setattr(pipeline, "route", lambda *a, **k: Route(intent="followup", is_followup=True))
    result = pipeline.run_turn("Independent analysis", existing_session(), client=SemanticPlanClient(
        QuerySemantics(), plan), compose_answer=False)
    assert not result["route"]["is_followup"]
    assert result["table"]["columns"] == ["balance"]


@pytest.mark.parametrize("interaction", ["compare_previous", "presentation_only"])
def test_other_structured_interactions_preserve_existing_artifact(interaction, source, monkeypatch):
    _, _, plan, _ = source
    monkeypatch.setattr(pipeline, "route", lambda *a, **k: Route(intent="series_analysis"))
    result = pipeline.run_turn("Use this analysis", existing_session(), client=SemanticPlanClient(
        QuerySemantics(interaction=interaction, preserve_existing_window=True), plan), compose_answer=False)
    assert result["route"]["is_followup"]
    assert result["route"]["presentation_only"] == (interaction == "presentation_only")
    assert "previous" in result["table"]["columns"]
    assert bool(result["plan"]["steps"]) == (interaction == "compare_previous")


@pytest.mark.parametrize("failure", [None, LLMError("offline"), {"requested_output": "invented"}])
def test_unavailable_or_invalid_parse_does_not_guess_stock_changes(failure):
    class BrokenClient:
        def structured(self, *a, **k):
            if isinstance(failure, Exception):
                raise failure
            return failure
    client = BrokenClient() if failure is not None else None
    session = existing_session()
    decision = Route(intent="followup", is_followup=True)
    result = pipeline.interpret_query("stok olmasın; o ay yatırılan yeni mevduat", session, decision, client)
    assert result.requested_output == "unspecified"
    assert result.interaction == "extend_previous"
    assert result.preserve_existing_window
    assert session.facts["semantic_source"] == "fallback"


def test_semantics_reaches_discovery_as_structured_constraints(source, monkeypatch):
    _, found, plan, _ = source
    seen = {}

    def discover(question, **kwargs):
        seen.update(kwargs)
        return found
    monkeypatch.setattr(pipeline, "discover_concepts", discover)
    pipeline.run_turn("Rate from the chosen basis", client=SemanticPlanClient(
        QuerySemantics(requested_basis="flow", frequency="weekly", currencies=["TL"]), plan),
        compose_answer=False)
    assert seen["requested_basis"] == "flow"
    assert seen["frequency"] == "weekly"


def test_empty_semantic_object_is_unavailable_instead_of_silent_defaults():
    class IncompleteClient:
        def structured(self, *a, **k):
            return {"output_requirement": "A balance movement", "semantics": {}}
    assert parse_query_semantics("Any question", IncompleteClient()) is None


@pytest.fixture(scope="module")
def live_client():
    if os.environ.get("KKB_LIVE_SEMANTICS") != "1":
        pytest.skip("opt-in real-model semantic evaluation")
    with KloudeksClient(timeout=45) as client:
        yield client


@pytest.mark.parametrize("question,expected", OUTPUT_CASES)
def test_live_output_paraphrases(question, expected, live_client):
    result = parse_query_semantics(question, live_client, context={"has_artifact": False})
    assert result is not None
    assert result.requested_output == expected


@pytest.mark.parametrize("question", FOLLOWUP_CASES)
def test_live_followup_paraphrases(question, live_client):
    result = parse_query_semantics(question, live_client, context={
        "has_artifact": True, "columns": {"deposits": {"label": "Mevduat", "semantics": "stock"}},
        "window": ["2021-01-01", "2025-12-01"],
    })
    assert result is not None
    assert result.interaction == "extend_previous"
    assert result.preserve_existing_window
