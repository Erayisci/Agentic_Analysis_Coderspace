"""Tests for the BDDK weekly-bulletin path.

The parser tests read the committed archive directly and need no build; the
lakehouse tests at the bottom open data/lakehouse.duckdb and pin the corpus
shape and the cross-check against the monthly bulletin.
"""
import datetime as dt

import duckdb
import pandas as pd
import pytest

from backend.core.config import DUCKDB_PATH, RAW_BDDK_WEEKLY_DIR
from backend.core.errors import ValidationError
from backend.domain.weekly_tables import TABLES, UNIT, WEEKLY_FORMULA_OVERRIDES
from backend.parsing.bddk_weekly import (
    parse_date,
    parse_number,
    parse_weekly_archive,
    stated_unit,
)
from backend.validation.weekly import (
    check_superseded_series,
    check_weekly_against_monthly,
    month_end_weeks,
    run_weekly_validations,
)

EXPECTED_WEEKS = 296        # 2021-01-08 .. 2026-09-04
EXPECTED_ITEMS = 201


@pytest.fixture(scope="module")
def archive():
    return parse_weekly_archive(RAW_BDDK_WEEKLY_DIR)


@pytest.fixture(scope="module")
def weekly(archive):
    return archive[0]


@pytest.fixture(scope="module")
def items(archive):
    return archive[1]


# --- primitives -------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("28.128.197,29545", 28128197.29545),
        ("0", 0.0),
        ("-2.683.731,46529", -2683731.46529),
        ("", None),
        ("-", None),
    ],
)
def test_parse_number(text, expected):
    assert parse_number(text) == expected


def test_parse_date_accepts_the_one_digit_day_bddk_writes():
    assert parse_date("4.09.2026") == dt.date(2026, 9, 4)
    assert parse_date("22.01.2021") == dt.date(2021, 1, 22)
    with pytest.raises(ValueError):
        parse_date("2026-09-04")


def test_the_report_states_its_own_unit():
    assert stated_unit("Birim: Milyon TL") == UNIT
    assert stated_unit("Sektör") is None


# --- catalogue --------------------------------------------------------------

def test_every_table_is_registered_and_its_items_are_read(items):
    assert len(items) == EXPECTED_ITEMS
    assert set(items.table_id) == {t.table_id for t in TABLES}
    assert items.item_id.is_unique
    for table in TABLES:
        assert len(items[items.table_id == table.table_id]) == table.items_seen


def test_bddk_publishes_the_retirement_date_so_it_is_not_measured(items):
    """22 items carry a `Sonlandırılma Tarihi`, and two whole tables were re-issued."""
    retired = items[items.retired_on.notna()]
    assert len(retired) == 22
    by_table = retired.groupby("dataset").size().to_dict()
    assert by_table["menkul_degerler"] == 9      # re-issued 2022-09-10
    assert by_table["yabanci_para_pozisyonu"] == 10   # re-issued 2023-01-09
    assert set(retired[retired.dataset == "yabanci_para_pozisyonu"].retired_on) == {
        dt.date(2023, 1, 9)
    }
    # A retired item's row code survives; an active one has none to publish.
    assert retired.row_code.notna().all()
    assert items[items.retired_on.isna()].row_code.isna().all()


def test_informational_rows_are_read_off_the_label(items):
    """31 items say '(Bilgi)' -- they sit OUTSIDE their table's totals."""
    assert int(items.is_informational.sum()) == 31
    flagged = items[items.is_informational]
    assert flagged.entity_name.str.contains(r"\(Bilgi", regex=True).all()
    # The trap this guards: a memo line that looks like an ordinary child row.
    assert "KOBİ Kredileri (Bilgi)" in set(flagged.entity_name)


def test_parents_are_derived_from_the_formulas_the_labels_publish(items):
    """'Tüketici Kredileri (4+5+6)' is the parent of rows 4, 5 and 6."""
    krediler = items[items.dataset == "krediler"].set_index("entity_key")
    assert krediler.loc["5689", "formula"] == "(4+5+6)"
    for child in ("5690", "5691", "5692"):          # a) Konut, b) Taşıt, c) İhtiyaç
        assert krediler.loc[child, "parent_key"] == "5689"
    # The top row is nobody's child.
    assert pd.isna(krediler.loc["5687", "parent_key"])
    # Subtraction is a net position, not a parentage: table 297's '(2-3)'.
    fx = items[items.dataset == "yabanci_para_pozisyonu"].set_index("entity_key")
    assert fx.loc["5884", "formula"] == "(2-3)"
    assert pd.isna(fx.loc["5885", "parent_key"])


def test_the_one_stale_label_is_registered_not_silently_trusted(items):
    """BDDK inserted a row and left 'Bankalardan Alacaklar (5+6)' pointing at itself."""
    assert ("diger_bilanco_kalemleri", 5745) in WEEKLY_FORMULA_OVERRIDES
    row = items[items.item_id == 5745].iloc[0]
    # The published text is never edited away: it stays in the name.
    assert "(5+6)" in row.entity_name
    assert row.formula == "(6+7)"


# --- observations -----------------------------------------------------------

def test_corpus_shape(weekly):
    assert weekly.period.nunique() == EXPECTED_WEEKS
    assert weekly.period.min() == pd.Timestamp("2021-01-08")
    assert weekly.period.max() == pd.Timestamp("2026-09-04")
    assert weekly.dataset.nunique() == len(TABLES)
    assert set(weekly.unit) == {UNIT}
    assert set(weekly.currency) == {"TL", "FX", "total"}
    assert not weekly.duplicated(["period", "entity_key", "currency"]).any()


def test_currency_vocabulary_matches_the_monthly_corpus(weekly):
    """A synonym here would hide the weekly corpus from any currency filter."""
    assert set(weekly.currency) == {"TL", "FX", "total"}
    assert "tp" not in set(weekly.currency) and "yp" not in set(weekly.currency)


def test_total_is_tl_plus_fx(weekly):
    """`Toplam` is not a third currency; summing all three triple counts."""
    loans = weekly[(weekly.dataset == "krediler") & (weekly.entity_key == "5687")]
    wide = loans.pivot_table(index="period", columns="currency", values="value")
    gap = (wide["TL"] + wide["FX"] - wide["total"]).abs()
    assert gap.max() < 0.01


def test_every_label_formula_holds_in_every_week(weekly):
    """59 of the 60 stated identities hold as published; the 60th is registered."""
    for slug, group in weekly.groupby("dataset"):
        run_weekly_validations(group, slug)      # raises if any identity fails


def test_retired_items_publish_nothing_after_the_date_bddk_states(weekly):
    retired = weekly[weekly.retired_on.notna()]
    assert len(retired) > 0
    assert (retired.period <= retired.retired_on).all()


def test_a_retired_item_and_its_replacement_are_one_series(weekly):
    """What licenses reading them as one -- and what would catch a re-definition."""
    fx = weekly[weekly.dataset == "yabanci_para_pozisyonu"]
    results = check_superseded_series(fx, "yabanci_para_pozisyonu")
    assert len(results) == 10
    assert all(r["passed"] for r in results)
    # 104 weeks x 3 currency columns, every one of them in agreement.
    assert "agrees on 312/312" in results[0]["detail"]


def test_changing_a_superseded_value_breaks_the_agreement(weekly):
    """The check must fail when a re-issue changes numbers, not just row ids."""
    fx = weekly[weekly.dataset == "yabanci_para_pozisyonu"].copy()
    target = (fx.entity_key == "5850") & (fx.currency == "total")
    fx.loc[target, "value"] = fx.loc[target, "value"] * 1.05
    results = check_superseded_series(fx, "yabanci_para_pozisyonu")
    failed = [r for r in results if not r["passed"]]
    assert len(failed) == 1 and failed[0]["entity_key"] == "5850"
    assert "CHANGED the definition" in failed[0]["detail"]


def test_the_one_registered_publication_gap_is_the_only_hole(weekly):
    """Table 297's current items are backfilled to 2021 but miss one week."""
    fx = weekly[(weekly.dataset == "yabanci_para_pozisyonu") & weekly.retired_on.isna()]
    covered = set(fx.period.unique())
    missing = set(weekly.period.unique()) - covered
    assert missing == {pd.Timestamp("2022-01-07")}
    # And the superseded definition is what publishes that week.
    superseded = weekly[(weekly.dataset == "yabanci_para_pozisyonu")
                        & weekly.retired_on.notna()]
    assert pd.Timestamp("2022-01-07") in set(superseded.period)


def test_an_unregistered_gap_aborts_the_build(weekly):
    loans = weekly[weekly.dataset == "krediler"]
    punched = loans[~((loans.entity_key == "5687")
                      & (loans.period == pd.Timestamp("2023-06-02")))]
    with pytest.raises(ValidationError, match="unexplained gap"):
        run_weekly_validations(punched, "krediler")


# --- cross-validation against the monthly bulletin --------------------------

def test_only_ten_weeks_of_the_corpus_land_on_a_month_end(weekly):
    aligned = month_end_weeks(weekly.period.unique())
    assert len(aligned) == 10
    assert pd.Timestamp("2021-12-31") in aligned


def test_weekly_and_monthly_agree_where_the_dates_align(weekly):
    from backend.parsing.bddk_bulletin import parse_bulletin_table
    from backend.core.config import RAW_BDDK_JSON_DIR

    monthly = pd.concat(
        [parse_bulletin_table(RAW_BDDK_JSON_DIR, slug)
         for slug in ("krediler", "tuketici_kredileri", "bilanco")],
        ignore_index=True,
    )
    report = check_weekly_against_monthly(weekly, monthly)
    assert len(report) == 5
    assert report.passed.all()


def test_a_thousandfold_unit_error_would_fail_the_cross_check(weekly):
    """The failure this check exists for: the weekly corpus read as bin TL."""
    from backend.parsing.bddk_bulletin import parse_bulletin_table
    from backend.core.config import RAW_BDDK_JSON_DIR

    monthly = parse_bulletin_table(RAW_BDDK_JSON_DIR, "krediler")
    rescaled = weekly.copy()
    rescaled["value"] = rescaled.value * 1000
    with pytest.raises(ValidationError, match="weekly vs monthly"):
        check_weekly_against_monthly(rescaled, monthly)


# --- lakehouse --------------------------------------------------------------

@pytest.fixture(scope="module")
def connection():
    if not DUCKDB_PATH.exists():
        pytest.skip("run python -m backend.lakehouse.build first")
    return duckdb.connect(str(DUCKDB_PATH), read_only=True)


def test_weekly_tables_reached_the_lakehouse(connection):
    weeks, items_seen = connection.execute(
        "SELECT count(DISTINCT period), count(DISTINCT entity_key) FROM weekly_observations"
    ).fetchone()
    assert weeks == EXPECTED_WEEKS
    assert connection.execute("SELECT count(*) FROM weekly_items").fetchone()[0] == EXPECTED_ITEMS
    assert items_seen <= EXPECTED_ITEMS


def test_no_weekly_row_reaches_the_agent_unlabelled(connection):
    unlabelled = connection.execute(
        "SELECT count(*) FROM weekly_observations "
        "WHERE unit IS NULL OR currency IS NULL OR temporal_semantics IS NULL"
    ).fetchone()[0]
    assert unlabelled == 0


def test_the_cross_check_is_persisted_for_the_agent_to_quote(connection):
    rows = connection.execute(
        "SELECT count(*), sum(CASE WHEN passed THEN 1 ELSE 0 END) FROM data_quality_report "
        "WHERE \"check\" LIKE 'weekly vs monthly%'"
    ).fetchone()
    assert rows == (5, 5)
