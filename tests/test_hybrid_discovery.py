"""Hybrid retrieval (LanceDB + reciprocal rank fusion) and the temporal-grain guard.

Ported from the 2026-09-20 external-zone work; the alias/qualifier ranking tweaks
that work also made are NOT ported (integrate/graphs has its own benchmarked
ranking, backend/eval/discovery_cases.yaml), so their tests are left out.

The regression these pin is a real failure, not a hypothetical: asked for
monthly consumer credit and consumer NPLs, discovery ranked three *sibling*
products (`Takipteki Konut/Taşıt/İhtiyaç Kredileri`) above the row the question
actually named (`Takipteki Tüketici Krd.`), and offered weekly series alongside
monthly ones so the planner divided one grain by the other.

Two causes, both fixed here and both pinned below: the published name was
compared without ASCII folding, so a Turkish diacritic made the discriminating
word score LESS than a generic one; and BDDK's abbreviation `Krd.` shares no
substring with the question's word "kredileri".
"""
import math

import pytest

from backend.agent.state import AnalysisArtifact, ColumnLineage
from backend.core.labels import ascii_fold
from backend.tools import lakehouse as L
from backend.tools import transforms as T
from backend.tools.vector_store import (VectorStore, candidate_text, cosine,
                                        reciprocal_rank_fusion)

THE_FAILING_QUESTION = (
    "2022-2026 yillari arasindaki aylik veriden Turkiye de tuketici kredileri ile "
    "takipteki tuketici kredilerini gosteriniz."
)


# --------------------------------------------------------------------------
# lexical normalisation


def test_ascii_fold_handles_the_turkish_dotted_i():
    """'İ'.lower() is not 'i'. Every name-side match depended on this."""
    assert ascii_fold("Takipteki Tüketici Krd.") == "takipteki tuketici krd."
    assert ascii_fold("TAKİPTEKİ TÜKETİCİ KREDİLERİ") == "takipteki tuketici kredileri"
    assert ascii_fold("İnşaat") == "insaat"
    assert ascii_fold(None) == ""


def test_score_reads_a_turkish_name_and_an_ascii_one_identically():
    """The bug in one assertion: the term is ASCII, the published name is not,
    and `str.lower()` does not bridge the two in Turkish. Scoring the same row
    with its real name and with a pre-folded one must give the same number --
    it used to give less for the real one, and that deficit is exactly what
    let a sibling product outrank it."""
    terms = L._terms("takipteki tuketici kredileri")
    published = {"key": "takipteki_tuketici_krd", "name": "Takipteki Tüketici Krd.",
                 "dataset": "tuketici_kredileri", "source": "bulletin", "unit": "milyon TL"}
    folded = dict(published, name="takipteki tuketici krd.")
    assert L._score(published, terms) == L._score(folded, terms)
    assert L._score(published, terms) > 0


# --------------------------------------------------------------------------
# grain


@pytest.mark.parametrize("question, expected", [
    ("aylik veriden konut kredilerini goster", "monthly"),
    ("haftalik bultene gore toplam krediler", "weekly"),
    ("gunluk kur hareketleri", "daily"),
    ("ceyreklik GSYIH", "quarterly"),
    ("konut kredilerini goster", None),          # silence is not a request
])
def test_query_grain_is_explicit_only(question, expected):
    assert L.query_grain(question) == expected


def test_candidate_grain_comes_from_the_catalogue():
    assert L.candidate_grain({"source": "bulletin"}) == "monthly"
    assert L.candidate_grain({"source": "weekly"}) == "weekly"
    assert L.candidate_grain({"source": "macro", "native_frequency": "business_daily"}) == "daily"
    assert L.candidate_grain({"source": "macro", "native_frequency": "quarterly"}) == "quarterly"


def test_grain_policy_demotes_but_does_not_delete():
    """A weekly series still leads the monthly one; it just cannot win a seat
    it was not asked for."""
    candidates = [{"source": "weekly", "grain": "weekly", "score": 20.0},
                  {"source": "bulletin", "grain": "monthly", "score": 12.0}]
    L._apply_grain_policy(candidates, "monthly")
    assert candidates[0]["score"] == pytest.approx(10.0)
    assert candidates[0]["grain_mismatch"]
    assert candidates[1]["score"] == 12.0, "the requested grain is untouched"


def test_grain_policy_is_a_no_op_when_no_grain_was_requested():
    candidates = [{"source": "weekly", "grain": "weekly", "score": 20.0}]
    L._apply_grain_policy(candidates, None)
    assert candidates[0]["score"] == 20.0


# --------------------------------------------------------------------------
# Reciprocal Rank Fusion


def test_rrf_rewards_appearing_in_both_rankings():
    """The property the fusion is actually for: a series both halves like beats
    one that only a single half found, however confident that half was."""
    lexical = ["both", "lexical_only"]
    vector = ["both", "vector_only"]
    fused = reciprocal_rank_fusion([lexical, vector], k=60)
    assert fused["both"] > fused["lexical_only"]
    assert fused["both"] > fused["vector_only"]


def test_rrf_is_convex_in_rank():
    """1st-and-3rd narrowly beats 2nd-and-2nd, because 1/(k+r) is convex.

    Pinned because it is counter-intuitive and load-bearing: it is what stops
    a merely-topical dense hit that both halves rank mid-table from displacing
    an exact match one half is certain about.
    """
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["c", "b", "a"]], k=60)
    assert fused["a"] == pytest.approx(fused["c"]), "symmetric positions tie"
    assert fused["a"] > fused["b"]
    assert fused["a"] - fused["b"] < 1e-4, "but only just -- the gap is tiny by design"


def test_rrf_uses_rank_not_score():
    """The whole point: an unbounded lexical sum cannot be added to a cosine."""
    fused = reciprocal_rank_fusion([["x", "y"]], k=60)
    assert fused["x"] == pytest.approx(1 / 61)
    assert fused["y"] == pytest.approx(1 / 62)


def test_rrf_of_one_ranking_preserves_that_ranking():
    fused = reciprocal_rank_fusion([["a", "b", "c"]])
    assert sorted(fused, key=lambda item: -fused[item]) == ["a", "b", "c"]


def test_rrf_accepts_a_custom_identity():
    rows = [{"source": "bulletin", "key": "k"}]
    fused = reciprocal_rank_fusion([rows], key=lambda r: (r["source"], r["key"]))
    assert ("bulletin", "k") in fused


# --------------------------------------------------------------------------
# vector store, entirely offline


def _fake_embedder(vocabulary):
    """A deterministic bag-of-words embedder. No network, no model."""
    def embed(texts):
        vectors = []
        for text in texts:
            words = set(ascii_fold(text).replace(".", " ").split())
            vectors.append([1.0 if token in words else 0.0 for token in vocabulary])
        return vectors
    return embed


def test_candidate_text_carries_the_semantic_metadata():
    text = candidate_text({"name": "Takipteki Tüketici Krd.", "dataset": "tuketici_kredileri",
                           "source": "bulletin", "grain": "monthly"})
    assert "Takipteki Tüketici Krd." in text
    assert "tuketici_kredileri" in text and "bulletin" in text and "grain:monthly" in text


def test_cosine_is_zero_for_a_zero_vector_rather_than_nan():
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert not math.isnan(cosine([0.0], [0.0]))


def test_vector_store_round_trips_offline(tmp_path):
    vocabulary = ["takipteki", "tuketici", "krd", "konut", "kredileri", "monthly"]
    candidates = [
        {"key": "takipteki_tuketici_krd", "source": "bulletin", "dataset": "tuketici_kredileri",
         "name": "Takipteki Tüketici Krd.", "grain": "monthly"},
        {"key": "takipteki_konut_kredileri", "source": "bulletin", "dataset": "tuketici_kredileri",
         "name": "Takipteki Konut Kredileri", "grain": "monthly"},
    ]
    store = VectorStore(directory=tmp_path, embedder=_fake_embedder(vocabulary), prefer_lance=False)
    report = store.build(candidates)
    assert report["n_indexed"] == 2

    reopened = VectorStore(directory=tmp_path, embedder=_fake_embedder(vocabulary), prefer_lance=False)
    assert reopened.available(), "an index on disk must be readable without rebuilding"
    assert reopened.size() == 2

    hits = reopened.search("takipteki tuketici krd", limit=2)
    assert hits[0].key == "takipteki_tuketici_krd"
    assert hits[0].similarity > hits[1].similarity
    assert hits[0].identity() == ("bulletin", "tuketici_kredileri", "takipteki_tuketici_krd")


def test_vector_store_is_unavailable_rather_than_broken_when_nothing_is_indexed(tmp_path):
    """Discovery must degrade to lexical ranking, never raise."""
    store = VectorStore(directory=tmp_path, prefer_lance=False)
    assert store.available() is False
    assert store.search("anything") == []
    assert store.backend() is None


def test_vector_store_search_without_an_embedder_returns_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.tools.vector_store.kloudeks_embedder", lambda client=None: None)
    vocabulary = ["a", "b"]
    store = VectorStore(directory=tmp_path, embedder=_fake_embedder(vocabulary), prefer_lance=False)
    store.build([{"key": "k", "source": "bulletin", "dataset": "d", "name": "a", "grain": "monthly"}])
    blind = VectorStore(directory=tmp_path, embedder=None, prefer_lance=False)
    assert blind.available() is True
    assert blind.search("a") == [], "no embedder means no dense half, not an exception"


def test_build_without_an_embedder_is_an_explicit_error(tmp_path, monkeypatch):
    """Building is the one operation that may not degrade silently: an index
    nobody noticed was never written is worse than a refusal."""
    monkeypatch.setattr("backend.tools.vector_store.kloudeks_embedder", lambda client=None: None)
    store = VectorStore(directory=tmp_path, embedder=None, prefer_lance=False)
    with pytest.raises(RuntimeError, match="no embedder"):
        store.build([{"key": "k", "source": "bulletin", "dataset": "d", "name": "n"}])


# --------------------------------------------------------------------------
# the grain guard in the transform and verifier layers


def _column(artifact, name, grain, unit="milyon TL", values=(1.0, 2.0, 3.0)):
    import pandas as pd
    index = pd.date_range("2026-01-01", periods=len(values), freq="MS")
    artifact.add_column(name, pd.Series(values, index=index), ColumnLineage(
        column=name, label=name, source="bulletin", unit=unit,
        temporal_semantics="stock", grain=grain, citation={"table": "t"}))


def test_ratio_refuses_to_divide_across_grains():
    artifact = AnalysisArtifact()
    _column(artifact, "weekly_loans", "weekly")
    _column(artifact, "monthly_npl", "monthly")
    with pytest.raises(ValueError, match="different grains"):
        T.ratio(artifact, "monthly_npl", "weekly_loans")


def test_ratio_allows_one_grain():
    artifact = AnalysisArtifact()
    _column(artifact, "loans", "monthly")
    _column(artifact, "npl", "monthly")
    name = T.ratio(artifact, "npl", "loans")
    assert artifact.lineage[name].grain == "monthly"


def test_verifier_flags_a_cross_grain_derivation():
    from backend.agent.state import Session
    from backend.agent.verifier import verify

    session = Session()
    artifact = session.artifact
    _column(artifact, "weekly_loans", "weekly")
    _column(artifact, "monthly_npl", "monthly")
    # The ratio transform refuses this, so reach the state a differently-routed
    # derivation would leave behind and prove the verifier catches it too.
    import pandas as pd
    artifact.add_column("npl_pct", pd.Series([1.0, 2.0, 3.0], index=artifact.frame.index),
                        ColumnLineage(column="npl_pct", label="npl %", source="derived", unit="%",
                                      temporal_semantics="ratio", grain=None,
                                      derived_from=["monthly_npl", "weekly_loans"], citation={}))
    report = verify(session)
    checks = {c["check"]: c for c in report["checks"]}
    assert checks["derived_columns_share_one_grain"]["passed"] is False
    assert checks["columns_share_one_grain"]["passed"] is False


# --------------------------------------------------------------------------
# the end-to-end ranking regression (needs a built lakehouse)


@pytest.fixture(scope="module")
def ranked():
    from backend.core.config import DUCKDB_PATH
    if not DUCKDB_PATH.exists():
        pytest.skip("lakehouse not built")
    return L.discover(THE_FAILING_QUESTION, limit=12)["candidates"]


def _position(candidates, key):
    for index, candidate in enumerate(candidates):
        if candidate["key"] == key:
            return index
    return None


def test_the_named_series_outranks_its_siblings(ranked):
    """The defect, in one assertion.

    `Takipteki Tüketici Krd.` is the row the question names. It used to score
    11.211 and rank 6th, below `Takipteki Konut Kredileri` at 13.844 -- which
    the question never mentions -- because the correct row spells the word
    "Kredileri" as "Krd." and its 'ü' failed to match an ASCII query term.
    """
    exact = _position(ranked, "takipteki_tuketici_krd")
    sibling = _position(ranked, "takipteki_konut_kredileri")
    assert exact is not None, "the row the question names must be discoverable"
    assert sibling is None or exact < sibling, (
        "a sibling product the question never named outranked the row it did")


def test_a_monthly_question_does_not_surface_weekly_series_at_the_top(ranked):
    """Weekly item 5688 ends 2026-09-04; the monthly bulletin ends 2026-07.
    Pairing them produced a ratio out of two different vintages and scopes."""
    top_five = ranked[:5]
    assert all(c["grain"] == "monthly" for c in top_five), \
        f"non-monthly series in the top 5: {[(c['key'], c['grain']) for c in top_five]}"
    assert "5688" not in [c["key"] for c in top_five]


def test_discover_reports_the_grain_it_enforced():
    from backend.core.config import DUCKDB_PATH
    if not DUCKDB_PATH.exists():
        pytest.skip("lakehouse not built")
    result = L.discover(THE_FAILING_QUESTION, limit=6)
    assert result["requested_grain"] == "monthly"
    assert result["retrieval"]["mode"] in ("lexical", "hybrid_rrf")


def test_discover_fuses_a_real_index_end_to_end(tmp_path, monkeypatch):
    """The hybrid path, through `discover`, with an index on disk.

    Uses a deterministic bag-of-words embedder rather than a model: what is
    being pinned is that a dense hit reaches the fused ranking and carries its
    similarity, not that any particular embedding is good.
    """
    monkeypatch.setenv(L.HYBRID_ENV, "1")
    from backend.core.config import DUCKDB_PATH
    if not DUCKDB_PATH.exists():
        pytest.skip("lakehouse not built")

    vocabulary = ["takipteki", "tuketici", "krd", "bulletin", "monthly",
                  "tuketici_kredileri", "grain:monthly", "konut", "kredileri"]
    embedder = _fake_embedder(vocabulary)
    store = VectorStore(directory=tmp_path, embedder=embedder, prefer_lance=False)
    store.build([
        {"key": "takipteki_tuketici_krd", "source": "bulletin", "dataset": "tuketici_kredileri",
         "name": "Takipteki Tüketici Krd.", "grain": "monthly"},
        {"key": "takipteki_konut_kredileri", "source": "bulletin", "dataset": "tuketici_kredileri",
         "name": "Takipteki Konut Kredileri", "grain": "monthly"},
    ])
    monkeypatch.setattr("backend.tools.lakehouse.default_store", lambda: store)

    result = L.discover(THE_FAILING_QUESTION, limit=12)
    assert result["retrieval"]["mode"] == "hybrid_rrf"
    assert result["retrieval"]["vector"] > 0

    fused = [c for c in result["candidates"] if c.get("rrf_score")]
    assert fused, "the fused ranking must reach the returned candidates"
    exact = _position(result["candidates"], "takipteki_tuketici_krd")
    sibling = _position(result["candidates"], "takipteki_konut_kredileri")
    assert exact is not None and (sibling is None or exact < sibling)


def test_the_dense_half_cannot_overturn_a_lexical_veto(tmp_path, monkeypatch):
    """A non-positive lexical score is a disqualification, not a weak match.

    Dense retrieval knows nothing about grain or qualifiers, so it returns
    those rows on topical similarity alone. A weekly EVDS rate series scoring
    -9.022 was resurrected into the planner's shortlist purely on cosine.
    """
    monkeypatch.setenv(L.HYBRID_ENV, "1")
    from backend.core.config import DUCKDB_PATH
    if not DUCKDB_PATH.exists():
        pytest.skip("lakehouse not built")

    vetoed = {"source": "macro", "dataset": "bie_kt100h", "key": "TP.BKR.TRY.KTFTUK01",
              "name": "Tüketici Kredisi", "grain": "weekly"}
    vocabulary = ["tuketici", "kredisi", "macro", "weekly"]
    store = VectorStore(directory=tmp_path, embedder=_fake_embedder(vocabulary), prefer_lance=False)
    store.build([dict(vetoed)])
    monkeypatch.setattr("backend.tools.lakehouse.default_store", lambda: store)

    result = L.discover(THE_FAILING_QUESTION, limit=12)
    assert all(c["score"] > 0 for c in result["candidates"]), \
        f"a disqualified candidate re-entered: {[(c['key'], c['score']) for c in result['candidates']]}"
    assert "TP.BKR.TRY.KTFTUK01" not in [c["key"] for c in result["candidates"]]


def test_the_planner_context_leads_with_the_two_series_the_question_names():
    """`discover_concepts` is what the planner actually reads, and the clause
    split plus the per-source quota can reorder what `discover` ranked."""
    from backend.core.config import DUCKDB_PATH
    if not DUCKDB_PATH.exists():
        pytest.skip("lakehouse not built")

    candidates = L.discover_concepts(
        THE_FAILING_QUESTION + " Buna ek olarak takibe donusum oranini da hesaplayiniz.",
        limit=12)["candidates"]
    keys = [c["key"] for c in candidates]
    assert "takipteki_tuketici_krd" in keys[:4]
    assert "tuketici_kredileri" in keys[:4]
    # The FX-indexed residual must stay below the real loan book.
    if "tuketici_kredileri_dov_end" in keys:
        assert keys.index("tuketici_kredileri") < keys.index("tuketici_kredileri_dov_end")


def test_dense_results_are_diversified_across_datasets():
    """A dense top-k assumes distinguishable documents; this index has families.

    EVDS publishes `AKONUTSAT2` once per province, and those 81 rows held 93 of
    the dense top-120 for the reference demo question -- crowding out the
    housing-loan RATE, which is lexical rank 1 of 568.
    """
    from backend.core.config import DUCKDB_PATH
    if not DUCKDB_PATH.exists():
        pytest.skip("lakehouse not built")
    store = L.default_store()
    if not store.available():
        pytest.skip("no vector index built")

    raw = store.search("konut kredileri ve konut kredisi faiz oranlari", limit=120)
    if not raw:
        pytest.skip("no embedder configured")
    diversified = L._diversify(raw, 30)
    counts = {}
    for hit in diversified:
        counts[hit.dataset] = counts.get(hit.dataset, 0) + 1
    assert max(counts.values()) <= L.MAX_DENSE_PER_DATASET
    assert len(counts) > 1, "diversification must admit more than one datagroup"


def test_the_reference_demo_rate_survives_the_dense_half():
    """The regression the diversification cap exists for."""
    from backend.core.config import DUCKDB_PATH
    if not DUCKDB_PATH.exists():
        pytest.skip("lakehouse not built")
    # `discover_concepts` is what the planner reads: this branch keeps the
    # per-corpus seat guarantee there, not in the single-concept `discover`.
    keys = [c["key"] for c in L.discover_concepts(
        "konut kredileri ve konut kredisi faiz oranlari", limit=8)["candidates"]]
    assert "TP.KTF12" in keys, keys
    assert "tuketici_kredileri_konut" in keys, keys


def test_the_dense_half_is_opt_in(monkeypatch):
    """Embedding every query through the endpoint cost ~4.5 s per `discover`
    on this machine, so the dense half runs only when a deployment asks."""
    monkeypatch.delenv(L.HYBRID_ENV, raising=False)
    assert L.hybrid_enabled() is False
    monkeypatch.setenv(L.HYBRID_ENV, "1")
    assert L.hybrid_enabled() is True


def test_the_dense_half_contributes_recall_not_order():
    """Pinned as a decision, because it is counter-intuitive and measured.

    Every positive dense weight cost pinned rankings and bought none back --
    at 1.0 it lost five of the eight rankings this repo already had. This
    lexical ranker is not a generic BM25: it encodes aliases, EVDS tiers,
    qualifier penalties and unit traps that a generic embedding cannot see.
    Raising `DENSE_WEIGHT` means re-running that sweep first.
    """
    assert L.DENSE_WEIGHT == 0.0
    assert L.LEXICAL_WEIGHT > 0.0


def test_the_recall_tail_only_fires_when_lexical_came_up_short(tmp_path, monkeypatch):
    """A dense-only row is a safety net, not a default. When the lexical pass
    already filled the list, an unscored row is noise the planner reads past."""
    from backend.core.config import DUCKDB_PATH
    if not DUCKDB_PATH.exists():
        pytest.skip("lakehouse not built")
    store = L.default_store()
    if not store.available():
        pytest.skip("no vector index built")

    rich = L.discover(THE_FAILING_QUESTION, limit=8)
    assert all(c.get("found_by") != "vector_only" for c in rich["candidates"]), \
        "a well-served question must not be padded with unscored dense rows"
    assert all(c["score"] > 0 for c in rich["candidates"])


def test_discover_falls_back_to_lexical_when_the_dense_half_raises(monkeypatch):
    """Discovery is load-bearing for every question: a broken index, a model
    outage or a stale dimension must cost relevance, never the turn."""
    from backend.core.config import DUCKDB_PATH
    if not DUCKDB_PATH.exists():
        pytest.skip("lakehouse not built")

    class Exploding:
        def available(self):
            raise RuntimeError("corrupt index")

    monkeypatch.setattr("backend.tools.lakehouse.default_store", lambda: Exploding())
    result = L.discover(THE_FAILING_QUESTION, limit=6)
    assert result["retrieval"]["mode"] == "lexical"
    assert result["candidates"], "lexical ranking still answers"


def test_discovery_still_finds_the_reference_demo_series():
    """The fix must not cost the scenario the whole system is pinned to."""
    from backend.core.config import DUCKDB_PATH
    if not DUCKDB_PATH.exists():
        pytest.skip("lakehouse not built")
    keys = [c["key"] for c in L.discover_concepts(
        "konut kredileri ve konut kredisi faiz oranlari", limit=12)["candidates"]]
    assert "tuketici_kredileri_konut" in keys
    assert any(key.startswith("TP.KTF") for key in keys)
