"""Tests for the TCMB EVDS path.

The registry and parser tests read the committed archive directly and need
no build. The lakehouse tests at the bottom open data/lakehouse.duckdb and
pin the demo scenario: the reference chart in the kick-off deck must be
producible from the lakehouse alone, in SQL, with no gaps.
"""
import datetime as dt

import duckdb
import pytest

from backend.core.config import DUCKDB_PATH, EVDS_CATALOG_DIR, RAW_EVDS_JSON_DIR
from backend.core.errors import ValidationError
from backend.domain.evds_series import BY_CODE, GROUPS
from backend.parsing.evds import CANONICAL_UNITS, load_catalogue, parse_group, parse_tarih, resolve_unit
from backend.transform.macro import align_monthly
from backend.validation.macro import check_unit_resolution

DEMO_SERIES = {
    "TP.KTF12": "housing loan rate",
    "TP.GENENDEKS.T1": "TÜFE 2003=100",
    "TP.KFE.TR": "house price index",
    "TP.AKONUTSAT1.KTRTOPLAM": "total house sales",
    "TP.AKONUTSAT2.KTRTOPLAM": "mortgaged house sales",
    "TP.APIFON4": "TCMB average funding cost",
}


# --- registry and date labels ----------------------------------------------

def test_registry_tier0_names_the_demo_inputs():
    tier0 = {g.code for g in GROUPS if g.tier == 0}
    assert {"bie_kt100h", "bie_tukfiy2003", "bie_kfe", "bie_akonutsat1", "bie_akonutsat2", "bie_apifon"} <= tier0


def test_stock_and_flow_loan_rate_labels_are_published_metadata(catalogue):
    from backend.tools.lakehouse import rate_basis
    for group, basis, frequency in [("bie_kt100h", "akim", "weekly"), ("bie_kt210a", "stok", "monthly")]:
        rows = catalogue[catalogue.datagroup == group]
        vehicle = rows[rows.name_tr.str.startswith("Taşıt Kredisi (")]
        assert len(vehicle) == 1
        assert rate_basis(vehicle.iloc[0].name_tr) == basis
        assert vehicle.iloc[0].native_frequency == frequency
        assert vehicle.iloc[0].temporal_semantics == "rate"
        assert BY_CODE[group].semantics == "rate", "stock loan basis is still an interest RATE"


@pytest.mark.parametrize("basis,key,opposite", [("Stok", "TP.BKR.TRY.17", "akim"),
                                               ("Akım", "TP.KTF11", "stok")])
def test_explicit_vehicle_rate_basis_beats_generic_faiz_alias(basis, key, opposite):
    from backend.tools.lakehouse import discover, discover_concepts, rate_basis
    if not DUCKDB_PATH.exists():
        pytest.skip("run python -m backend.lakehouse.build first")
    question = f"EVDS, Taşıt Kredisi (TL, {basis}, %) faiz oranını ekle"
    for result in [discover(question, limit=4), discover_concepts(question, limit=4)]:
        assert result["candidates"][0]["key"] == key
        assert not any(rate_basis(c["name"]) == opposite for c in result["candidates"])


def test_unspecified_loan_rate_basis_keeps_the_demo_choice():
    from backend.tools.lakehouse import discover
    if not DUCKDB_PATH.exists():
        pytest.skip("run python -m backend.lakehouse.build first")
    assert discover("konut kredisi faiz oranı", limit=1)["candidates"][0]["key"] == "TP.KTF12"


@pytest.mark.parametrize(
    "label, expected, grain",
    [
        ("08-01-2021", dt.date(2021, 1, 8), "day"),
        ("2021-1", dt.date(2021, 1, 1), "month"),
        ("2021-12", dt.date(2021, 12, 1), "month"),
        ("2021-Q1", dt.date(2021, 3, 1), "quarter"),
        ("2021-Q4", dt.date(2021, 12, 1), "quarter"),
    ],
)
def test_parse_tarih(label, expected, grain):
    assert parse_tarih(label) == (expected, grain)


def test_parse_tarih_rejects_unknown_labels():
    with pytest.raises(ValueError):
        parse_tarih("2021/01")


# --- archive ----------------------------------------------------------------

@pytest.fixture(scope="module")
def catalogue():
    return load_catalogue(EVDS_CATALOG_DIR)


def test_catalogue_declares_semantics_for_every_series(catalogue):
    assert catalogue.temporal_semantics.isin(["stock", "flow", "rate", "index"]).all()
    assert catalogue.monthly_rule.isin(["last", "avg", "sum"]).all()
    assert catalogue.series_code.is_unique
    for code in DEMO_SERIES:
        assert code in set(catalogue.series_code), code


def test_override_changes_only_the_named_series(catalogue):
    apifon = catalogue[catalogue.datagroup == "bie_apifon"].set_index("series_code")
    assert apifon.loc["TP.APIFON4", "temporal_semantics"] == "rate"
    assert apifon.loc["TP.APIFON3", "temporal_semantics"] == "stock"


# --- units ------------------------------------------------------------------

@pytest.mark.parametrize(
    "name, published, semantics, overridden, expected",
    [
        # The group states one unit and means it: 30 of 44 groups look like this.
        ("Toplam Konut Satış Sayısı", "Adet", "flow", False, "adet"),
        ("TP Mevduat", "bin TL", "stock", False, "bin TL"),
        # Compound BIRIMI, split by what the series declares itself to be.
        ("A.Toplam Fonlama (A1+A2)", "milyon TL ve yüzde", "stock", False, "milyon TL"),
        ("TCMB Ağırlıklı Ortalama Fonlama Maliyeti", "milyon TL ve yüzde", "rate", True, "%"),
        # BIRIMI names a method, not a unit; the series name carries the real one.
        ("Konut Kredisi (TL, Stok, %)", "Ağırlıklı ortalama", "rate", False, "%"),
        ("Altın - Kapanış Fiyatı - USD/ons", "TL/kg, USD/ons, Euro/ons, TL/gr", "rate", False, "USD/ons"),
        # 'İşlem Hacmi' inside a name must not be read as the 'İşlem' unit.
        ("Altın - İşlem Hacmi - TL/kg", "TL/kg, USD/ons, Euro/ons, TL/gr", "flow", False, "TL/kg"),
        ("Kredi Kartı İşlem Adedi", "İşlem", "flow", False, "adet"),
        # Building permits: one four-way BIRIMI over four differently measured series.
        ("(Toplam) Bir Daireli Binalar (Yapı Sayısı)", "Adet,TL/m2", "flow", False, "adet"),
        ("(Toplam) Bir Daireli Binalar (Yüzölçüm (Metrekare))", "Adet,TL/m2", "flow", False, "m2"),
        ("(Toplam) Bir Daireli Binalar (Değer(TL))", "Adet,TL/m2", "flow", False, "TL"),
        # Spelling variants of one unit, and an index base standing in for one.
        ("TÜFE Genel", "2003=100", "index", False, "endeks"),
        ("Kapasite Kullanım Oranı", "Yüzde", "rate", False, "%"),
        # Nothing usable published: the declaration is the last resort.
        ("TR2B (Malatya, Elazığ, Bingöl)", None, "index", False, "endeks"),
    ],
)
def test_unit_is_resolved_per_series_not_per_group(name, published, semantics, overridden, expected):
    assert resolve_unit(name, published, semantics, overridden) == expected


def test_every_series_resolves_to_a_unit_from_the_vocabulary(catalogue):
    assert catalogue.unit.notna().all()
    published = catalogue[~catalogue.derived.fillna(False)]
    assert set(published.unit) <= set(CANONICAL_UNITS)
    # TCMB's own string is kept, so a relabelling is visible rather than lost.
    assert "unit_source" in catalogue.columns


def test_the_funding_group_is_not_stamped_with_one_unit(catalogue):
    """The measured defect: BIRIMI is per group, and this group mixes two units."""
    apifon = catalogue[catalogue.datagroup == "bie_apifon"].set_index("series_code")
    assert set(apifon.unit_source) == {"milyon TL ve yüzde"}
    assert apifon.loc["TP.APIFON4", "unit"] == "%"
    assert apifon.loc["TP.APIFON3", "unit"] == "milyon TL"


def test_unresolved_units_abort_the_build(catalogue):
    broken = catalogue.copy()
    broken.loc[broken.series_code == "TP.APIFON3", "unit"] = None
    with pytest.raises(ValidationError, match="resolved to no unit"):
        check_unit_resolution(broken)


def test_weekly_rates_align_to_monthly_average(catalogue):
    native = parse_group(RAW_EVDS_JSON_DIR, BY_CODE["bie_kt100h"])
    monthly = align_monthly(native, catalogue)
    konut = monthly[monthly.series_code == "TP.KTF12"].set_index("period")
    jan = native[(native.series_code == "TP.KTF12") & (native.date >= dt.date(2021, 1, 1))
                 & (native.date < dt.date(2021, 2, 1))]
    assert len(jan) == 5  # five Fridays in January 2021
    assert konut.loc[dt.date(2021, 1, 1), "value"] == pytest.approx(jan.value.mean())
    assert konut.loc[dt.date(2021, 1, 1), "n_native_obs"] == 5
    assert konut.loc[dt.date(2021, 1, 1), "monthly_rule"] == "avg"


def test_house_sales_are_monthly_flows_not_ytd():
    native = parse_group(RAW_EVDS_JSON_DIR, BY_CODE["bie_akonutsat2"])
    total = native[native.series_code == "TP.AKONUTSAT2.KTRTOPLAM"].set_index("date").value
    # A year-to-date series would be monotonic within a year; sales are not.
    year = total[(total.index >= dt.date(2021, 1, 1)) & (total.index <= dt.date(2021, 12, 1))]
    assert (year.diff().dropna() < 0).any()
    assert year[dt.date(2021, 1, 1)] == 11560


# --- lakehouse ---------------------------------------------------------------

@pytest.fixture(scope="module")
def connection():
    if not DUCKDB_PATH.exists():
        pytest.skip("lakehouse not built")
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    yield con
    con.close()


def test_demo_series_cover_the_whole_window(connection):
    for code in DEMO_SERIES:
        months = connection.execute(
            "SELECT count(*) FROM macro_observations WHERE series_code = ? "
            "AND period BETWEEN DATE '2021-01-01' AND DATE '2026-06-01'", [code]
        ).fetchone()[0]
        assert months == 66, (code, months)


def test_reference_scenario_table_is_producible_in_sql(connection):
    """The kick-off deck's chart: housing loans, rate, CPI-deflated loans,
    house price index and mortgaged-sale share, monthly 2021-01..2025-12."""
    frame = connection.execute(
        """
        WITH loans AS (
            SELECT period, value AS konut_kredisi
            FROM bulletin_observations
            WHERE dataset = 'tuketici_kredileri' AND entity_key = 'tuketici_kredileri_konut'
              AND currency = 'total'
        ),
        macro AS (
            SELECT period,
                   max(CASE WHEN series_code = 'TP.KTF12' THEN value END)          AS faiz,
                   max(CASE WHEN series_code = 'TP.GENENDEKS.T1' THEN value END)   AS tufe,
                   max(CASE WHEN series_code = 'TP.KFE.TR' THEN value END)         AS kfe,
                   max(CASE WHEN series_code = 'DERIVED.IPOTEKLI_PAY.KTRTOPLAM' THEN value END) AS ipotekli_pay
            FROM macro_observations GROUP BY period
        )
        SELECT l.period, konut_kredisi, faiz, tufe, kfe, ipotekli_pay,
               konut_kredisi / tufe AS konut_kredisi_reel
        FROM loans l JOIN macro m USING (period)
        WHERE l.period BETWEEN DATE '2021-01-01' AND DATE '2025-12-01'
        ORDER BY l.period
        """
    ).df()
    assert len(frame) == 60
    assert frame.notna().all().all()
    # The mortgaged share fell to ~4% in 2023 when rates spiked; rates ran 17-45%.
    assert frame.ipotekli_pay.between(3, 60).all()
    assert frame.faiz.between(10, 60).all()


def test_no_macro_series_reaches_the_agent_unlabelled(connection):
    unlabelled = connection.execute(
        "SELECT count(*) FROM macro_observations o LEFT JOIN macro_series s USING (series_code) "
        "WHERE s.temporal_semantics IS NULL OR s.unit IS NULL OR s.monthly_rule IS NULL"
    ).fetchone()[0]
    assert unlabelled == 0


def test_quarterly_series_sit_on_quarter_end_months(connection):
    months = connection.execute(
        "SELECT DISTINCT month(period) FROM macro_observations WHERE series_code = 'TP.GSYIH20.BY.B1GQ'"
    ).df().iloc[:, 0].tolist()
    assert set(months) <= {3, 6, 9, 12}


def test_fx_month_end_is_kept_alongside_the_average(connection):
    row = connection.execute(
        "SELECT value, value_avg, value_last, n_native_obs, monthly_rule FROM macro_observations "
        "WHERE series_code = 'TP.DK.USD.A.YTL' AND period = DATE '2021-12-01'"
    ).df().iloc[0]
    assert row.monthly_rule == "avg" and row.value == row.value_avg
    assert row.n_native_obs > 20
    # December 2021: the lira spiked past 18 then reversed to 13 after the KKM
    # announcement, so the month-end fix sits well below the monthly mean.
    assert row.value_last < row.value_avg
    assert abs(row.value_last - row.value_avg) / row.value_avg > 0.02


def test_series_index_is_searchable_in_turkish(connection):
    hits = connection.execute(
        "SELECT series_code FROM macro_series WHERE name_tr ILIKE '%konut kredisi%' AND datagroup = 'bie_kt100h'"
    ).df().series_code.tolist()
    assert hits == ["TP.KTF12"]
