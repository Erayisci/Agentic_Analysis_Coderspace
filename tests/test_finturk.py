"""Tests for the BDDK FinTurk (il-bazli) path.

The parser tests read the committed archive directly and need no build; the
lakehouse tests at the bottom open data/lakehouse.duckdb and pin the corpus
shape.
"""
import duckdb
import pandas as pd
import pytest

from backend.core.config import DUCKDB_PATH, RAW_BDDK_FINTURK_JSON_DIR
from backend.domain.finturk_tables import BY_SLUG, TABLES, TARAF_GROUPS
from backend.ingestion.bddk_finturk import quarters_between
from backend.parsing.bddk_finturk import parse_finturk_archive, parse_finturk_response
from backend.transform.finturk import build_finturk_metrics

EXPECTED_QUARTERS = 22        # 2021-Q1 .. 2026-Q2
EXPECTED_PROVINCES = 82       # 81 iller + YURT DIŞI


@pytest.fixture(scope="module")
def archive():
    return parse_finturk_archive(RAW_BDDK_FINTURK_JSON_DIR)


# --- primitives -------------------------------------------------------------

def test_quarters_between_walks_by_three_months():
    assert quarters_between("2021-3", "2021-12") == ["2021-3", "2021-6", "2021-9", "2021-12"]
    assert quarters_between("2025-12", "2026-6") == ["2025-12", "2026-3", "2026-6"]
    assert quarters_between("2021-3", "2021-3") == ["2021-3"]


def test_quarters_between_rejects_a_non_quarter_month():
    with pytest.raises(ValueError, match="donem must look like"):
        quarters_between("2021-1", "2021-12")


def test_quarters_between_rejects_start_after_end():
    with pytest.raises(ValueError, match="is after end"):
        quarters_between("2022-3", "2021-3")


def test_parse_finturk_response_rejects_a_mismatched_period():
    table = BY_SLUG["krediler"]
    response = {
        "success": True,
        "Json": {
            "colNames": ["Eftodu", "Yıl", "Ay", "Şehir", "Grup", "Toplam Nakdi Krediler"],
            "colModels": [
                {"name": "EftKodu"}, {"name": "Yil"}, {"name": "Ay"},
                {"name": "Sehir"}, {"name": "Grup"}, {"name": "ToplamNakdiKrediler"},
            ],
            "data": {"rows": [{"cell": [10001, 2026, 3, "ANKARA", "SEKTÖR", 100]}]},
        },
    }
    with pytest.raises(ValueError, match="expected 2026-6"):
        parse_finturk_response(response, table, "2026-6")


# --- archive shape ------------------------------------------------------

def test_seven_tables_are_registered():
    assert len(TABLES) == 7
    assert len(TARAF_GROUPS) == 7
    assert TARAF_GROUPS[10001] == "SEKTÖR"


def test_archive_covers_the_brief_window(archive):
    assert archive.period.nunique() == EXPECTED_QUARTERS
    assert archive.period.min() == pd.Timestamp("2021-03-01")
    assert archive.period.max() == pd.Timestamp("2026-06-01")
    assert set(archive.dataset.unique()) == set(BY_SLUG)


def test_every_province_is_present_every_quarter(archive):
    counts = archive.groupby("period").province.nunique()
    assert (counts == EXPECTED_PROVINCES).all()


def test_every_taraf_group_is_a_registered_one(archive):
    assert set(archive.taraf_code.unique()) <= set(TARAF_GROUPS)
    for code, name in archive[["taraf_code", "taraf_name"]].drop_duplicates().itertuples(index=False):
        assert TARAF_GROUPS[code] == name


def test_metric_keys_are_ascii_so_turkish_search_is_case_safe(archive):
    assert archive.metric.str.match(r"^[a-z0-9_]+$").all()


def test_no_row_reaches_the_agent_unlabelled(archive):
    unlabelled = archive[archive.unit.isna() | archive.metric.isna() | archive.dataset.isna()]
    assert unlabelled.empty


def test_ratios_table_is_unit_percent(archive):
    ratios = archive[archive.dataset == "oranlar"]
    assert (ratios.unit == "%").all()


def test_branch_count_and_per_capita_overrides_apply(archive):
    branches = archive[(archive.dataset == "subeler_ve_nufus") & (archive.metric_name == "Yurtiçi Şube Sayısı")]
    assert (branches.unit == "adet").all()
    per_capita = archive[(archive.dataset == "subeler_ve_nufus") & (archive.metric_name == "Kişi Başı Nakdi Kredi")]
    assert (per_capita.unit == "TL").all()


# --- finturk_metrics (the discovery index) ----------------------------------

def test_finturk_metrics_has_one_row_per_dataset_metric(archive):
    metrics = build_finturk_metrics(archive)
    assert not metrics.duplicated(["dataset", "metric"]).any()
    assert set(metrics.dataset) == set(BY_SLUG)


def test_finturk_metrics_classifies_ratios(archive):
    metrics = build_finturk_metrics(archive)
    ratios = metrics[metrics.dataset == "oranlar"]
    assert (ratios.temporal_semantics == "ratio").all()
    assert (ratios.unit == "%").all()
    stocks = metrics[metrics.dataset == "krediler"]
    assert (stocks.temporal_semantics == "stock").all()


# --- lakehouse --------------------------------------------------------------

@pytest.fixture(scope="module")
def connection():
    if not DUCKDB_PATH.exists():
        pytest.skip("run python -m backend.lakehouse.build first")
    return duckdb.connect(str(DUCKDB_PATH), read_only=True)


def test_finturk_reached_the_lakehouse(connection):
    quarters, provinces, datasets = connection.execute(
        "SELECT count(DISTINCT period), count(DISTINCT province), count(DISTINCT dataset) "
        "FROM finturk_observations"
    ).fetchone()
    assert quarters == EXPECTED_QUARTERS
    assert provinces == EXPECTED_PROVINCES
    assert datasets == 7


def test_a_known_finturk_figure_matches_the_live_response(connection):
    # Measured live against BultenFinturk/tr/Home/VeriGetir on 2026-09-19.
    value = connection.execute(
        "SELECT value FROM finturk_observations WHERE dataset='krediler' "
        "AND metric='toplam_nakdi_krediler' AND taraf_code=10001 "
        "AND province='İSTANBUL' AND period='2026-03-01'"
    ).fetchone()[0]
    assert value == 8_684_258_391


def test_finturk_observations_and_metrics_are_queryable_via_run_sql(connection):
    """ALLOWED_TABLES must list both, or the agent's SQL escape hatch rejects
    a query that references them even though they exist in the database."""
    from backend.tools.lakehouse import ALLOWED_TABLES

    assert {"finturk_observations", "finturk_metrics"} <= ALLOWED_TABLES
    assert connection.execute("SELECT count(*) FROM finturk_metrics").fetchone()[0] == 76


def test_discover_finds_finturk_explicitly_and_by_default(connection):
    from backend.tools.lakehouse import discover

    explicit = discover("konut kredisi", source="finturk")
    assert explicit["n_candidates"] > 0
    assert all(c["source"] == "finturk" for c in explicit["candidates"])

    # planner.Step.source accepts "finturk" (see test_agent.py for the
    # fetch_series round trip this licenses), so a general question is allowed
    # to surface a FinTurk candidate alongside the other three corpora.
    general = discover("il bazli kredi dagilimi")
    assert "finturk" in {c["source"] for c in general["candidates"]}
