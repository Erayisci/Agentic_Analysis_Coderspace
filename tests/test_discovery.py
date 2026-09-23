"""Discovery quality as a number, not an anecdote.

`tests/test_agent.py` pins eight individual queries, each added after a specific
ranking failure. That catches a regression on those eight and says nothing about
the ninth phrasing, which is the one demo day will use. This file runs the whole
benchmark in `backend/eval/discovery_cases.yaml` -- 85 phrasings of 20 concepts
the corpus genuinely publishes -- and asserts the aggregate does not slip.

The floors are set just under the measured values so an honest improvement never
fails the build and a regression always does. When a change moves these up,
move the floors up with it: a floor that trails the truth by twenty points
stops being a test.

Measured before this work began: 54.1 / 62.4 / 72.9, and 89.4% in pool.
"""
import pytest

from backend.core.config import DUCKDB_PATH
from backend.eval.run_discovery_eval import load_cases, run
from backend.tools.lakehouse import discover, discover_concepts


def test_structured_basis_and_frequency_reach_each_discovery_clause(monkeypatch):
    from backend.tools import lakehouse as L
    calls = []
    monkeypatch.setattr(L, "discover", lambda query, **kw: calls.append(kw) or {"candidates": []})
    L.discover_concepts("mevduat faizi ve kredi faizi", requested_basis="flow", frequency="weekly")
    assert len(calls) == 2
    assert all(c["rate_basis_constraint"] == "akim" and c["frequency"] == "weekly" for c in calls)


def test_discovery_preserves_parenthesized_series_names_and_all_clauses(monkeypatch):
    from backend.tools import lakehouse as L
    searched = []

    def record(query, **kwargs):
        searched.append(query)
        return {"candidates": []}

    monkeypatch.setattr(L, "discover", record)
    question = ("EVDS, Taşıt Kredisi (TL, Stok, %) verisini ekle; "
                "konut ve mevduat. enflasyon; işsizlik; üretim; "
                "TP için TP Mevduat / Katılım Fonları - Yurt İçi Yerleşik "
                "YP için Döviz Tevdiat Hesabı / Katılım Fonları - Yurt İçi Yerleşik")
    found = L.discover_concepts(question)
    assert "Taşıt Kredisi (TL, Stok, %) verisini ekle" in searched
    assert len(searched) > 6
    assert any("TP Mevduat / Katılım Fonları - Yurt İçi Yerleşik" in c for c in searched)
    assert any("Döviz Tevdiat Hesabı / Katılım Fonları - Yurt İçi Yerleşik" in c for c in searched)
    assert found["concepts"] == searched
    assert found["n_concepts"] == len(searched)


def test_nested_parentheses_do_not_split_conjunctions(monkeypatch):
    from backend.tools import lakehouse as L
    searched = []
    monkeypatch.setattr(L, "discover", lambda query, **kw: searched.append(query) or {"candidates": []})
    L.discover_concepts("Tüketici (Konut ve Taşıt (TL, Stok, %)) ile enflasyon")
    assert searched == ["Tüketici (Konut ve Taşıt (TL, Stok, %))", "enflasyon"]


@pytest.fixture(scope="module")
def summary():
    if not DUCKDB_PATH.exists():
        pytest.skip("run python -m backend.lakehouse.build first")
    return run(load_cases())


def test_discovery_recall_does_not_regress(summary):
    assert summary["recall@1"] >= 0.85, _report(summary)
    assert summary["recall@3"] >= 0.90, _report(summary)
    assert summary["recall@8"] >= 0.95, _report(summary)


def test_every_concept_the_corpus_holds_is_reachable_by_search(summary):
    """The pool is what any reranking can ever choose from, so a concept missing
    from it is a different and worse defect than one ranked badly: no weighting
    change can fix it, only the search text or a new alias can.

    This reached 100% when `search_text` began carrying the context that names a
    row -- the data group's title, the parent line, words for the unit -- and
    when the query stopped being compared to Turkish text in the wrong alphabet.
    """
    missed = [f"{row['family']}: {row['phrasing']!r}"
              for row in summary["rows"] if row["pool_rank"] is None]
    assert not missed, f"unreachable by any search: {missed}"


def test_a_named_source_narrows_the_search_rather_than_diluting_it():
    """"BDDK haftalik bultenine gore toplam krediler" ranked its answer 83rd:
    "haftalik" and "bulten" match no row in any corpus and only dilute the two
    words that do. Measured -- those words appear in ZERO published names, which
    is what makes peeling them off safe."""
    if not DUCKDB_PATH.exists():
        pytest.skip("run python -m backend.lakehouse.build first")
    found = discover("BDDK haftalik bultenine gore toplam krediler", limit=3)
    assert found["sources"] == ["weekly"]
    assert found["candidates"][0]["source"] == "weekly"

    # "TCMB" is NOT a source word here: it occurs inside 17 published names, so
    # treating it as one would strip the subject out of the question.
    assert discover("TCMB fonlama maliyeti", limit=1)["sources"] is None


def test_a_ratio_question_is_not_answered_with_a_balance():
    """Three rows share the words "Takipteki Alacaklar" -- a stock and a
    provision in milyon TL, and the published ratio in %. The word that
    separates them is "orani", and a unit has no spelling a text search
    reaches."""
    if not DUCKDB_PATH.exists():
        pytest.skip("run python -m backend.lakehouse.build first")
    for query in ("takipteki alacaklar orani", "NPL orani", "TGA orani"):
        top = discover(query, limit=1)["candidates"][0]
        assert top["unit"] == "%", f"{query!r} -> {top['key']} ({top['unit']})"

    # ...and the same question without the word still means the balance.
    assert discover("takipteki alacaklar", limit=1)["candidates"][0]["key"] == "takipteki_alacaklar"


def test_each_clause_of_a_question_keeps_a_seat_in_the_merged_list():
    """The reported failure in full.

    Both series this question names are published, and neither reached the
    planner: the NPL ratio was dropped by a per-corpus quota applied at a limit
    too small to hold it, and the commercial-loan rate lost to a louder clause
    because scores from different clauses were compared as if they were
    commensurable. They are not -- each clause is scored against its own terms,
    which is why `by_concept` exists and why the merge now seeds from it.
    """
    if not DUCKDB_PATH.exists():
        pytest.skip("run python -m backend.lakehouse.build first")
    question = (
        "2021-2024 döneminde BDDK bültenindeki Takipteki Alacaklar (TGA / NPL) Oranı ile "
        "EVDS'deki Ağırlıklı Ortalama Ticari Kredi Faizleri arasındaki ilişkiyi incele. "
        "Faizlerin en sert yükseldiği aylarda NPL oranının hemen tepki verip vermediğini "
        "analiz et. Faiz artışı ile batık kredilerin artışı arasında kaç aylık bir gecikme "
        "(lag) anomalisi gözleniyor?")
    found = discover_concepts(question, limit=8)

    firsts = [ranked[0][1] for ranked in found["by_concept"] if ranked]
    assert "takipteki_alacaklar_brut_toplam_nakdi_krediler" in firsts
    assert "TP.KTF17" in firsts

    # `deterministic_series_plan` fetches each clause's first choice, and the
    # model planner chooses from `candidates`; both must be able to see them.
    keys = {c["key"] for c in found["candidates"]}
    assert "takipteki_alacaklar_brut_toplam_nakdi_krediler" in keys
    assert "TP.KTF17" in keys


def test_the_derived_share_does_not_outrank_the_series_it_divides():
    """`DERIVED.IPOTEKLI_PAY.*` is named with both "konut satislari" and
    "toplam konut satislarina" inside it, so the query phrase is a literal
    substring of the ratio's name and the ratio outranked the count. "Ipotekli"
    is a qualifier the question has to ask for, like "takipteki"."""
    if not DUCKDB_PATH.exists():
        pytest.skip("run python -m backend.lakehouse.build first")
    assert discover("konut satislari", limit=1)["candidates"][0]["key"] == "TP.AKONUTSAT1.KTRTOPLAM"
    assert discover("ipotekli konut satislarinin orani", limit=1)["candidates"][0]["key"].startswith("DERIVED.")


def test_house_sales_mean_housing_unless_commercial_premises_are_named():
    """TCMB suffixes these with the property type -- a leading `K` is Konut and
    its absence İş Yeri -- and publishes both under names differing in that one
    word. Until the derived catalogue stopped calling every one of them "konut",
    83 commercial-premises series carried a housing name, character for
    character identical to the housing row beside them."""
    if not DUCKDB_PATH.exists():
        pytest.skip("run python -m backend.lakehouse.build first")
    assert discover("mortgaged sales share", limit=1)["candidates"][0]["key"].endswith("KTRTOPLAM")
    assert "iş yeri" in discover("is yeri satislari", limit=1)["candidates"][0]["name"].lower()


def _report(summary) -> str:
    return (f"recall@1={summary['recall@1']:.1%} @3={summary['recall@3']:.1%} "
            f"@8={summary['recall@8']:.1%} pool={summary['in_pool']:.1%} "
            f"over {summary['n']} phrasings -- run "
            f"`python -m backend.eval.run_discovery_eval --failures` for the detail")


def test_long_mentor_prompt_keeps_trailing_rows_and_maturity_datasets():
    from backend.eval.run_eval import load_scenarios
    from backend.agent.pipeline import make_plan
    from backend.agent.router import route
    from backend.agent.state import Session
    if not DUCKDB_PATH.exists():
        pytest.skip("run python -m backend.lakehouse.build first")
    question = next(s["question"] for s in load_scenarios() if s["id"] == "mentor_tp_yp_maturity")
    found = discover_concepts(question, limit=12)
    assert len(found["concepts"]) > 6
    assert len(found["by_concept"]) == len(found["concepts"])
    assert len(found["candidates"]) <= 12
    expected = {"tp_mevduat_katilim_fonlari_yurt_ici_yerlesik",
                "doviz_tevdiat_hesabi_katilim_fonlari_yurt_ici_yerlesik"}
    assert {hits[0][1] for hits in found["by_concept"][-2:]} == expected
    assert expected <= {c["key"] for c in found["candidates"] if c["dataset"] == "mevduat_vade"}
    # These named rows are distinct keys, not an unsliced balance fetched twice.
    plan = make_plan(question, Session(), route(question), None)
    assert expected <= {s.key for s in plan.steps if s.dataset == "mevduat_vade"}


def test_trailing_exact_names_survive_a_full_candidate_budget(monkeypatch):
    from backend.tools import lakehouse as L

    def fake(query, **kwargs):
        return {"candidates": [{"source": "bulletin", "dataset": "test", "key": query,
                                "name": query, "score": 10, "name_match": 100 if query == "explicit" else 0}]}

    monkeypatch.setattr(L, "discover", fake)
    question = "; ".join([f"concept{i}" for i in range(20)] + ["explicit"])
    result = L.discover_concepts(question, limit=12)
    assert result["n_concepts"] == 21 and len(result["by_concept"]) == 21
    assert result["n_candidates"] == 12
    assert "explicit" in {c["key"] for c in result["candidates"]}
