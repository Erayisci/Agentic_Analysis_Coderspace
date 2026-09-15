"""Acceptance tests for the lakehouse build.

Golden fixtures come from the research handoff (§22) and were independently
verified against the raw June 2026 workbooks. If any of these fail after a
code change or a data refresh, the data layer is not trustworthy.

Run:  pytest tests/ -q          (requires a completed `python -m backend.lakehouse.build`)
"""
import duckdb
import pytest

from backend.core.config import DUCKDB_PATH, SCHEMA_CARD_PATH

JUNE_2026 = "2026-06-01"


@pytest.fixture(scope="module")
def connection():
    if not DUCKDB_PATH.exists():
        pytest.fail("lakehouse.duckdb not found - run `python -m backend.lakehouse.build` first")
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    yield con
    con.close()


def one_value(con, sql, *params):
    return con.execute(sql, params).fetchone()[0]


# ----------------------------------------------------------------------- #
# Golden fixtures: national totals, June 2026                             #
# ----------------------------------------------------------------------- #
BDDK_NATIONAL_JUNE_2026 = {
    "bddk_cash_current": 26_783_571_952,
    "bddk_follow_up": 775_460_760,
    "bddk_total_cash": 27_559_032_712,
    "bddk_short_term_cash": 12_280_716_188,
    "bddk_medium_long_term_cash": 14_502_855_764,
    "bddk_noncash": 22_800_974_187,
}

TBB_NATIONAL_JUNE_2026 = {
    "tbb_cash": 26_986_246_339.266,
    "tbb_liquidation": 924_999_824.016,
    "tbb_gross": 27_911_246_163.282,
}

BDDK_MANUFACTURING_JUNE_2026 = {
    "bddk_cash_current": 6_082_610_641,
    "bddk_follow_up": 133_169_552,
    "bddk_total_cash": 6_215_780_193,
    "bddk_short_term_cash": 2_554_497_103,
    "bddk_medium_long_term_cash": 3_528_113_538,
    "bddk_noncash": 3_580_087_277,
}

TBB_MAPPED_MANUFACTURING_JUNE_2026 = {
    "tbb_cash": 6_135_570_931.987,
    "tbb_liquidation": 158_775_853.695,
    "tbb_gross": 6_294_346_785.682,
}


@pytest.mark.parametrize("metric,expected", BDDK_NATIONAL_JUNE_2026.items())
def test_bddk_national_golden_values(connection, metric, expected):
    value = one_value(
        connection,
        "SELECT value FROM observations WHERE source='BDDK' AND sector_code='70' AND period=? AND metric=?",
        JUNE_2026, metric,
    )
    assert value == pytest.approx(expected, abs=1)


@pytest.mark.parametrize("metric,expected", TBB_NATIONAL_JUNE_2026.items())
def test_tbb_national_golden_values(connection, metric, expected):
    value = one_value(
        connection,
        "SELECT value FROM observations WHERE source='TBB_RM' AND sector_code='TOTAL' AND period=? AND metric=?",
        JUNE_2026, metric,
    )
    assert value == pytest.approx(expected, abs=1)


@pytest.mark.parametrize("metric,expected", BDDK_MANUFACTURING_JUNE_2026.items())
def test_bddk_manufacturing_golden_values(connection, metric, expected):
    value = one_value(
        connection,
        "SELECT value FROM observations WHERE source='BDDK' AND sector_code='09' AND period=? AND metric=?",
        JUNE_2026, metric,
    )
    assert value == pytest.approx(expected, abs=1)


@pytest.mark.parametrize("metric,expected", TBB_MAPPED_MANUFACTURING_JUNE_2026.items())
def test_tbb_mapped_manufacturing_golden_values(connection, metric, expected):
    value = one_value(
        connection,
        """
        SELECT sum(o.value) FROM observations o
        JOIN sector_crosswalk x
          ON x.source='TBB_RM' AND x.source_sector_code=o.sector_code
        WHERE x.canonical_sector='manufacturing' AND o.period=? AND o.metric=?
        """,
        JUNE_2026, metric,
    )
    assert value == pytest.approx(expected, abs=1)


# ----------------------------------------------------------------------- #
# Cross-source divergence golden values (handoff §5)                      #
# ----------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "comparison,expected_pct",
    [("cash", 0.757), ("trouble", 19.284), ("total", 1.278)],
)
def test_national_divergence_june_2026(connection, comparison, expected_pct):
    value = one_value(
        connection,
        "SELECT divergence_pct FROM reconciliation_monitor "
        "WHERE canonical_sector='TOTAL_NATIONAL' AND comparison=? AND period=?",
        comparison, JUNE_2026,
    )
    assert value == pytest.approx(expected_pct, abs=0.001)


def test_national_trouble_divergence_always_in_band(connection):
    """The documented-methodology explanation is only valid while every month
    stays inside the statistical band; the agent relies on this flag."""
    flagged = one_value(
        connection,
        "SELECT count(*) FROM reconciliation_monitor "
        "WHERE canonical_sector='TOTAL_NATIONAL' AND comparison='trouble' AND out_of_band",
    )
    assert flagged == 0


def test_national_trouble_divergence_always_positive(connection):
    negatives = one_value(
        connection,
        "SELECT count(*) FROM reconciliation_monitor "
        "WHERE canonical_sector='TOTAL_NATIONAL' AND comparison='trouble' AND divergence_pct <= 0",
    )
    assert negatives == 0


# ----------------------------------------------------------------------- #
# Hierarchy traps (handoff §7)                                            #
# ----------------------------------------------------------------------- #
def test_manufacturing_children_exclude_transport_split(connection):
    """Sector 09 = 10..22 + 25. Sectors 23/24 are children of 22 and must not
    be counted again (the double-counting trap)."""
    child_sum = one_value(
        connection,
        """
        SELECT sum(value) FROM observations
        WHERE source='BDDK' AND period=? AND metric='bddk_total_cash'
          AND sector_code IN ('10','11','12','13','14','15','16','17','18','19','20','21','22','25')
        """,
        JUNE_2026,
    )
    assert child_sum == pytest.approx(BDDK_MANUFACTURING_JUNE_2026["bddk_total_cash"], abs=10)

    transport_split = one_value(
        connection,
        "SELECT sum(value) FROM observations WHERE source='BDDK' AND period=? "
        "AND metric='bddk_total_cash' AND sector_code IN ('23','24')",
        JUNE_2026,
    )
    twenty_two = one_value(
        connection,
        "SELECT value FROM observations WHERE source='BDDK' AND period=? "
        "AND metric='bddk_total_cash' AND sector_code='22'",
        JUNE_2026,
    )
    assert transport_split == pytest.approx(twenty_two, abs=10)


def test_sector_46_is_nonadditive_detail(connection):
    """Sector 44 = 45+47+48+49; adding detail row 46 must break the identity."""
    relation = one_value(
        connection, "SELECT relation FROM sectors WHERE source='BDDK' AND sector_code='46'"
    )
    assert relation == "detail"
    parent = one_value(
        connection,
        "SELECT value FROM observations WHERE source='BDDK' AND period=? "
        "AND metric='bddk_total_cash' AND sector_code='44'",
        JUNE_2026,
    )
    with_detail = one_value(
        connection,
        "SELECT sum(value) FROM observations WHERE source='BDDK' AND period=? "
        "AND metric='bddk_total_cash' AND sector_code IN ('45','46','47','48','49')",
        JUNE_2026,
    )
    assert with_detail > parent  # double-count if 46 were treated as a sibling


def test_tbb_rank_instability_handled_by_name_keys(connection):
    """TBB sector numbers are size ranks; our keys must be rank-independent.
    Agriculture was not in the same rank position in 2022 as in 2026, yet the
    slug key must return a continuous 54-month series."""
    months = one_value(
        connection,
        "SELECT count(DISTINCT period) FROM observations "
        "WHERE source='TBB_RM' AND sector_code='tarim_avcilik_ormancilik' AND metric='tbb_gross'",
    )
    assert months == 54


# ----------------------------------------------------------------------- #
# Coverage and catalog sanity                                             #
# ----------------------------------------------------------------------- #
def test_period_coverage(connection):
    bddk_months = one_value(connection, "SELECT count(DISTINCT period) FROM observations WHERE source='BDDK'")
    tbb_months = one_value(connection, "SELECT count(DISTINCT period) FROM observations WHERE source='TBB_RM'")
    assert bddk_months == 67  # 2021-01..2026-07
    assert tbb_months == 54   # 2022-01..2026-06


def test_no_generic_npl_metric_exists(connection):
    """Guardrail: troubled-credit concepts stay source-specific forever."""
    count = one_value(connection, "SELECT count(*) FROM metrics WHERE metric ILIKE '%npl%'")
    assert count == 0


def test_the_three_series_indexes_share_one_semantics_vocabulary(connection):
    """`metrics` used to say 'period_end_stock' where the other two say 'stock'.

    The agent learns the vocabulary once from the schema card; a synonym in one
    table means a filter that silently returns nothing.
    """
    words = connection.execute(
        "SELECT DISTINCT temporal_semantics FROM metrics UNION "
        "SELECT DISTINCT temporal_semantics FROM bulletin_metrics UNION "
        "SELECT DISTINCT temporal_semantics FROM macro_series"
    ).df().iloc[:, 0].tolist()
    assert set(words) == {"stock", "flow", "rate", "index", "cumulative_ytd", "ratio"}


def test_the_two_bddk_paths_spell_their_shared_unit_the_same_way(connection):
    """`observations` and `bulletin_observations` read the same BDDK table 05.

    They therefore hold the same unit, and a query filtering on it must reach
    both -- 'bin_TL' in one and 'bin TL' in the other excluded one silently.
    """
    units = connection.execute(
        "SELECT DISTINCT unit FROM observations UNION "
        "SELECT DISTINCT unit FROM bulletin_observations WHERE dataset='sektorel_kredi_dagilimi'"
    ).df().iloc[:, 0].tolist()
    assert units == ["bin TL"]


def test_all_validation_checks_recorded_and_passed(connection):
    total, passed = connection.execute(
        "SELECT count(*), sum(CASE WHEN passed THEN 1 ELSE 0 END) FROM data_quality_report"
    ).fetchone()
    assert total >= 25
    assert passed == total


# --------------------------------------------------------------------- #
# The agent's context window
# --------------------------------------------------------------------- #

def schema_card_queries():
    """Every SQL statement in the card's '## Query patterns' section.

    The card tells a model these 'run as written'. That claim is only worth
    something if it is enforced: a renamed column or a retired entity_key would
    otherwise leave the agent copying SQL that errors, which is worse than
    giving it no examples at all.
    """
    if not SCHEMA_CARD_PATH.exists():
        pytest.fail("schema_card.md not found - run `python -m backend.lakehouse.build` first")
    text = SCHEMA_CARD_PATH.read_text(encoding="utf-8")
    section = text.split("## Query patterns")[1].split("\n## ")[0]
    body = "\n".join(line for line in section.splitlines() if not line.strip().startswith("--"))
    return [statement.strip() for statement in body.split(";") if statement.strip()]


def test_schema_card_documents_the_query_patterns():
    queries = schema_card_queries()
    assert len(queries) == 7, f"expected 7 worked examples, card has {len(queries)}"


@pytest.mark.parametrize("n", range(7))
def test_every_schema_card_query_runs_and_returns_rows(connection, n):
    sql = schema_card_queries()[n]
    frame = connection.execute(sql).df()
    assert not frame.empty, f"card query {n + 1} returned no rows:\n{sql}"


def test_the_bulletin_entity_index_covers_every_published_series(connection):
    """bulletin_entities is the discovery surface: a line item missing from it
    is a line item the agent cannot find, however correct the fact table is."""
    indexed, published = connection.execute(
        "SELECT (SELECT count(*) FROM bulletin_entities), "
        "(SELECT count(*) FROM (SELECT DISTINCT dataset, entity_key FROM bulletin_observations))"
    ).fetchone()
    assert indexed == published == 519


def test_the_entity_index_agrees_with_the_facts_it_indexes(connection):
    """Every indexed attribute is a fact about the series, not a guess."""
    mismatches = connection.execute("""
        SELECT count(*) FROM bulletin_entities e JOIN (
            SELECT dataset, entity_key, min(unit) AS unit, min(entity_name) AS entity_name,
                   min(temporal_semantics) AS semantics, count(DISTINCT period) AS n_periods
            FROM bulletin_observations GROUP BY 1, 2
        ) o USING (dataset, entity_key)
        WHERE e.unit IS DISTINCT FROM o.unit OR e.entity_name IS DISTINCT FROM o.entity_name
           OR e.temporal_semantics IS DISTINCT FROM o.semantics OR e.n_periods <> o.n_periods
    """).fetchone()[0]
    assert mismatches == 0


def test_entity_keys_are_ascii_so_turkish_search_is_case_safe(connection):
    """The card tells the agent to ILIKE on entity_key rather than entity_name.

    That advice only holds while the keys stay ASCII: 'İ'.lower() is not 'i',
    so a Turkish character in a key would make the documented search miss rows.
    """
    non_ascii = connection.execute(
        "SELECT entity_key FROM bulletin_entities WHERE NOT regexp_matches(entity_key, '^[a-z0-9_/]+$')"
    ).df()
    assert non_ascii.empty, non_ascii.entity_key.tolist()
