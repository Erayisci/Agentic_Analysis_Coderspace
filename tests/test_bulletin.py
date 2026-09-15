"""Tests for the generic BDDK bulletin parser.

These read the archived JSON responses directly, so they do not need a build.
The point of most of them is that a *silent* change in the published tables
becomes a loud failure: the corpus is keyed on normalised labels, and three of
the seventeen tables reshuffle their rows mid-history.
"""
import datetime as dt

import pytest

from backend.domain.bulletin_tables import (
    BY_SLUG,
    KNOWN_IDENTITY_EXCEPTIONS,
    KNOWN_LIFECYCLES,
    TABLES,
)
from backend.core.config import RAW_BDDK_JSON_DIR
from backend.core.labels import canonical_key, qualified_key, slugify, strip_decorations
from backend.parsing.bddk_bulletin import parse_bulletin_table, split_measure
from backend.transform.bulletin import decumulate
from backend.validation.continuity import check_continuity, check_identities, expand_formula

EXPECTED_PERIODS = 67  # 2021-01 .. 2026-07


@pytest.fixture(scope="module")
def bulletin():
    """Every table parsed once; the whole corpus is ~135k rows."""
    return {t.slug: parse_bulletin_table(RAW_BDDK_JSON_DIR, t.slug) for t in TABLES}


# --- label normalisation ---------------------------------------------------

@pytest.mark.parametrize(
    "raw,name,formula,footnote,unit",
    [
        ("Tüketici Kredileri (2+3+4)", "Tüketici Kredileri", "(2+3+4)", "", None),
        ("Ortaklık Finansmanı*", "Ortaklık Finansmanı", "", "*", None),
        ("Menkul Değerler (2 den 26'ya)", "Menkul Değerler", "(2 den 26'ya)", "", None),
        ("Mudi Sayısı - Yurt İçi Yerleşik*", "Mudi Sayısı - Yurt İçi Yerleşik", "", "*", None),
        ("Krediler", "Krediler", "", "", None),
        # A parenthetical that is part of the name must survive.
        ("Opsiyon İşlemleri (Varlık)", "Opsiyon İşlemleri (Varlık)", "", "", None),
        # Every range spelling BDDK uses, all measured in the corpus.
        ("Teminat Mektupları (3+..+9)", "Teminat Mektupları", "(3+..+9)", "", None),
        ("Diğer Taahhütler (40+…+51)", "Diğer Taahhütler", "(40+…+51)", "", None),
        ("TOPLAM YÜKÜMLÜLÜKLER  (19+….+35)", "TOPLAM YÜKÜMLÜLÜKLER", "(19+….+35)", "", None),
        ("İmalat Sanayi (10+...+22+25)", "İmalat Sanayi", "(10+...+22+25)", "", None),
        # Chained groups, a bracketed formula, and a footnote on top of one.
        ("Toplam Faiz (Kar Payı) Gelirleri (1+...+14)-(2+3+4+5)",
         "Toplam Faiz (Kar Payı) Gelirleri", "(1+...+14)-(2+3+4+5)", "", None),
        ("VERGİ ÖNCESİ KAR (ZARAR) [(26+34+50)-45]",
         "VERGİ ÖNCESİ KAR (ZARAR)", "[(26+34+50)-45]", "", None),
        ("Teminatına göre Teminat Mektupları (11+...+16)*",
         "Teminatına göre Teminat Mektupları", "(11+...+16)", "*", None),
        # Rows that state their own unit, with and without a formula.
        ("Toplam KOBİ Niteliğindeki Müşteri Sayısı (6+7+8) (Adet)",
         "Toplam KOBİ Niteliğindeki Müşteri Sayısı", "(6+7+8)", "", "adet"),
        ("Çekirdek Sermaye Yeterliliği Rasyosu ((6/7)*100) (YÜZDE)",
         "Çekirdek Sermaye Yeterliliği Rasyosu", "((6/7)*100)", "", "%"),
        ("Takipteki Alacaklar (Brüt) / Toplam Nakdi Krediler (%)",
         "Takipteki Alacaklar (Brüt) / Toplam Nakdi Krediler", "", "", "%"),
        ("Ortalama Tahsilat Süresi (Gün)", "Ortalama Tahsilat Süresi", "", "", "gün"),
    ],
)
def test_strip_decorations(raw, name, formula, footnote, unit):
    assert strip_decorations(raw) == (name, formula, footnote, unit)


@pytest.mark.parametrize(
    "raw",
    [
        # Looks like a formula but is not: letters, or no operator at all.
        "Sermaye Piyasası İşlemleri Karları (Zararları) (Net)",
        "İhraç Edilen Menkul Kıymetler (Net)",
        "Menkul Değerler (Sukuk)",
        "Tüketici Kredileri (Dövize Endeksli)",
    ],
)
def test_a_trailing_parenthetical_that_names_the_row_is_kept(raw):
    """Stripping must stay narrow: only letter-free arithmetic and the six
    measured unit markers come off, or a rename hides inside the key."""
    name, formula, _footnote, unit = strip_decorations(raw)
    assert (name, formula, unit) == (raw, "", None)


def test_footnote_formula_and_unit_do_not_change_the_key():
    """The decorations BDDK adds mid-history must not invent a new series."""
    assert canonical_key("Ortaklık Finansmanı") == canonical_key("Ortaklık Finansmanı*")
    assert canonical_key("Menkul Değerler (2 den 24'e)") == canonical_key(
        "Menkul Değerler (2 den 26'ya)"
    )
    # The row-index form shifts as rows are inserted above it; so does the
    # ellipsis form, and both must leave the key alone.
    assert canonical_key("Kredi Riskine Esas Tutar (11+12+13+27+28)") == canonical_key(
        "Kredi Riskine Esas Tutar (11+12+13+29)"
    )
    assert canonical_key("TOPLAM VARLIKLAR (1+...+11)") == canonical_key(
        "TOPLAM VARLIKLAR (1+…+12)"
    )


def test_a_genuine_rename_does_change_the_key():
    """Normalisation must not be so aggressive that a real rename slips through."""
    assert canonical_key("Ortaklık Finansmanı") != canonical_key("Mal Karşılığı Vesaikin Finansmanı")


def test_slugify_transliterates_turkish():
    assert slugify("Otel ve Restoranlar (Turizm)") == "otel_ve_restoranlar_turizm"


def test_qualified_key():
    assert qualified_key("tp_mevduat", "a_gercek_kisiler") == "tp_mevduat/a_gercek_kisiler"
    assert qualified_key(None, "krediler") == "krediler"


SIMPLE = ["Tp", "Yp", "Toplam"]
TABLE_03 = ["KisaTp", "KisaYp", "KisaToplam", "OrtaUzunTp", "OrtaUzunYp", "OrtaUzunToplam",
            "ToplamTp", "ToplamYp", "Toplam"]
TABLE_09 = ["OnBin", "ElliBin", "IkiyuzelliBin", "Milyon", "Milyonarti", "Toplam"]


@pytest.mark.parametrize(
    "field,columns,metric,currency",
    [
        ("Tp", SIMPLE, "balance", "TL"),
        ("Yp", SIMPLE, "balance", "FX"),
        ("Toplam", SIMPLE, "balance", "total"),
        ("KisaTp", TABLE_03, "kisa", "TL"),
        ("NakdiKrediToplam", None, "nakdi_kredi", "total"),
        ("KisaVadeliNakdi", None, "kisa_vadeli_nakdi", None),
        ("Rasyo", None, "rasyo", None),
        # 'Toplam' only means 'total currency' when currency is the ONLY split.
        # In table 03 it is the total-maturity measure at total currency; in
        # table 09 it is the total across deposit-size brackets, and that table
        # publishes no currency split at all.
        ("Toplam", TABLE_03, "toplam", "total"),
        ("ToplamTp", TABLE_03, "toplam", "TL"),
        ("Toplam", TABLE_09, "toplam", None),
        ("OnBin", TABLE_09, "on_bin", None),
    ],
)
def test_split_measure(field, columns, metric, currency):
    assert split_measure(field, columns) == (metric, currency)


# --- corpus-wide invariants ------------------------------------------------

def test_every_table_parses_and_covers_the_full_history(bulletin):
    for slug, observations in bulletin.items():
        assert observations["period"].nunique() == EXPECTED_PERIODS, slug


def test_entity_keys_are_unique_within_a_period(bulletin):
    """Tables 09, 10 and 11 repeat a label six times a month; the parent
    qualification is what keeps those series apart."""
    for slug, observations in bulletin.items():
        counts = observations.groupby(["period", "entity_key", "metric", "currency"], dropna=False).size()
        assert counts.max() == 1, f"{slug}: duplicated key"


def test_continuity_holds_for_every_table(bulletin):
    """An entity that stops mid-history must be registered as a lifecycle."""
    for slug, observations in bulletin.items():
        unregistered = [r for r in check_continuity(observations, slug) if not r["passed"]]
        assert not unregistered, f"{slug}: {unregistered}"


def test_lifecycle_registry_has_no_dead_entries(bulletin):
    """A registered lifecycle that no longer matches any entity is stale --
    usually the sign that a key changed shape and the registry was not updated."""
    for (slug, entity_key) in KNOWN_LIFECYCLES:
        keys = set(bulletin[slug]["entity_key"])
        assert entity_key in keys, f"{slug}: stale lifecycle entry {entity_key!r}"


def test_row_position_is_not_used_as_an_identifier(bulletin):
    """Table 03 reshuffles at 2022-01: the row at position 15 changes meaning,
    so any series keyed on position would silently splice two line items."""
    krediler = bulletin["krediler"]
    retired = krediler[krediler.entity_key == "mal_karsiligi_vesaikin_finansmani"]
    introduced = krediler[krediler.entity_key == "vadeli_ticaretin_finansmanindan_alacaklar"]
    assert str(retired["period"].max()) == "2021-12-01"
    assert str(introduced["period"].min()) == "2022-01-01"
    assert set(retired["period"]).isdisjoint(set(introduced["period"]))


# --- table-specific facts the demo depends on ------------------------------

def test_housing_loans_are_present_and_continuous(bulletin):
    """The reference demo scenario runs on this series; table 05 does not
    contain it at all."""
    consumer = bulletin["tuketici_kredileri"]
    housing = consumer[consumer.entity_key == "tuketici_kredileri_konut"]
    assert housing["period"].nunique() == EXPECTED_PERIODS
    assert set(housing["currency"]) == {"TL", "FX", "total"}
    assert (housing[housing.currency == "total"]["value"] > 0).all()


def test_deposit_children_are_separated_by_parent(bulletin):
    """'a) Gerçek Kişiler' appears once per deposit type; keying on the bare
    label would collapse six distinct series into one."""
    deposits = bulletin["mevduat_turler"]
    individuals = sorted(
        key for key in deposits["entity_key"].unique() if key.endswith("/a_gercek_kisiler")
    )
    assert len(individuals) >= 5
    assert all("/" in key for key in individuals)


def test_liquidity_table_uses_trailing_section_totals(bulletin):
    """Table 11's bold rows are section totals, not headers, so 'Türev İşlemler'
    on the asset side and on the liability side must not collide."""
    liquidity = bulletin["likidite_durumu"]
    derivatives = sorted(
        key for key in liquidity["entity_key"].unique() if key.endswith("/turev_islemler")
    )
    assert len(derivatives) == 2, derivatives


def test_sector_46_is_derived_as_a_detail_row(bulletin):
    """canonical.BDDK_DETAIL_OF hand-codes that sector 46 is a non-additive
    detail of 45. The bulletin marks it italic, so the parser derives the same
    fact -- if this breaks, the two encodings have diverged."""
    sectoral = bulletin["sektorel_kredi_dagilimi"]
    detail = sectoral[sectoral.entity_name == "Bankalara Kullandırılan Krediler"]
    assert not detail.empty
    assert set(detail["parent_key"]) == {"parasal_kurumlar"}


def test_table_5_matches_the_dedicated_parser(bulletin):
    """The generic parser must reproduce the pinned sectoral corpus exactly."""
    from backend.core.config import RAW_BDDK_DIR
    from backend.core.labels import canonical_key as key_of
    from backend.parsing.bddk_sectoral import parse_bddk_directory

    metric_map = {
        "kisa_vadeli_nakdi": "bddk_short_term_cash",
        "orta_uzun_vadeli_nakdi": "bddk_medium_long_term_cash",
        "nakdi": "bddk_cash_current",
        "takipteki": "bddk_follow_up",
        "toplam_nakdi": "bddk_total_cash",
        "gayri_nakdi": "bddk_noncash",
    }

    old = parse_bddk_directory(RAW_BDDK_DIR)
    old["key"] = old["sector_name"].map(key_of)
    old_series = old.set_index(["period", "key", "metric"])["value"]

    new = bulletin["sektorel_kredi_dagilimi"].copy()
    new["metric"] = new["metric"].map(metric_map)
    assert new["metric"].notna().all()
    new_series = new.rename(columns={"entity_key": "key"}).set_index(
        ["period", "key", "metric"]
    )["value"]

    common = old_series.index.intersection(new_series.index)
    # Only sector 46 differs, because the generic parser qualifies it by parent.
    assert len(common) == len(old_series) - EXPECTED_PERIODS * len(metric_map)
    assert (old_series.loc[common] - new_series.loc[common]).abs().max() == 0


def test_units_are_declared_per_table():
    assert BY_SLUG["rasyolar"].unit is None          # mixes %, Bin TL, Gün, Kişi
    assert BY_SLUG["tuketici_kredileri"].unit == "milyon TL"
    assert BY_SLUG["diger_bilgiler"].unit == "adet"
    # Table 5 is the single table BDDK publishes in thousands.
    assert BY_SLUG["sektorel_kredi_dagilimi"].unit == "bin TL"


def test_the_response_states_its_own_unit_and_table_05_is_the_exception():
    """The unit is read from the caption, never defaulted.

    Fourteen tables say 'milyon TL' and table 5 says 'bin TL'. Taking one
    default for all of them understates every balance-sheet figure by 1000x.
    """
    from backend.parsing.bddk_bulletin import caption_unit

    assert caption_unit("Bilanço (milyon TL), Dönem:2026/6") == "milyon TL"
    assert caption_unit("Sektörel Kredi Dağılımı (bin TL), Dönem:2026/6") == "bin TL"
    assert caption_unit("KOBİ Kredileri  (milyon TL), Dönem:2026/6") == "milyon TL"
    assert caption_unit("Rasyolar, Dönem:2026/6") is None      # ratios state none


def test_the_two_loan_tables_agree_once_their_units_are_applied(bulletin):
    """Total cash loans appear in table 3 (milyon TL) and table 5 (bin TL).

    This is the measurement the declared units have to satisfy: read with one
    unit for both tables the same quantity differs by a factor of 1000.
    """
    loans = bulletin["krediler"]
    sectoral = bulletin["sektorel_kredi_dagilimi"]

    table_03 = loans[
        (loans.entity_key == "toplam_krediler")
        & (loans.metric == "toplam")
        & (loans.currency == "total")
    ].set_index("period")["value"]
    table_05 = sectoral[
        (sectoral.entity_key == "toplam") & (sectoral.metric == "nakdi")
    ].set_index("period")["value"]

    common = table_03.index.intersection(table_05.index)
    assert len(common) == EXPECTED_PERIODS

    # milyon TL * 1000 == bin TL. 64 of the 67 months agree to within the
    # rounding of the coarser table (both sides round, so a milyon TL, i.e.
    # 1000 bin TL); three carry a real BDDK discrepancy between the two tables,
    # the largest 506,150 bin TL at 2024-02, still 0.004% of the figure.
    # Reading both tables in one unit would show a 1000x gap, not a rounding one.
    gap = (table_03.loc[common] * 1000 - table_05.loc[common]).abs()
    assert (gap <= 1000).sum() >= EXPECTED_PERIODS - 3
    assert (gap / (table_05.loc[common])).max() < 1e-4


def test_rows_that_state_their_own_unit_override_the_table(bulletin):
    """Table 15 mixes four units and table 06 counts customers in 'adet'.
    Taking the table's unit for those rows labels a headcount as thousands of TL."""
    ratios = bulletin["rasyolar"]
    assert set(ratios.unit.unique()) == {"%", "bin TL", "kişi", "gün"}

    # Table 06 stacks two half-tables: four loan-balance rows and four
    # customer-count rows, and only the latter say '(Adet)' in their label.
    customers = bulletin["kobi_kredileri"]
    counts = customers[customers.entity_key.str.contains("musteri_sayisi")]
    assert set(counts.unit.unique()) == {"adet"}
    balances = customers[~customers.entity_key.str.contains("musteri_sayisi")]
    assert set(balances.unit.unique()) == {"milyon TL"}


def test_income_statement_is_declared_cumulative(bulletin):
    """The brief warns some BDDK releases are cumulative; measured, table 02 is."""
    assert BY_SLUG["kar_zarar"].semantics == "cumulative_ytd"
    assert all(BY_SLUG[s].semantics == "stock"
               for s in ("bilanco", "tuketici_kredileri", "sektorel_kredi_dagilimi"))

    frame = bulletin["kar_zarar"]
    series = frame[(frame.entity_key == "toplam_faiz_kar_payi_gelirleri")
                   & (frame.currency == "total")].set_index("period").value
    december = series[dt.date(2025, 12, 1)]
    january = series[dt.date(2026, 1, 1)]
    assert january < december * 0.2   # a stock would carry over; a YTD total resets


def test_decumulation_recovers_the_month(bulletin):
    frame = decumulate(bulletin["kar_zarar"])
    series = frame[(frame.entity_key == "toplam_faiz_kar_payi_gelirleri")
                   & (frame.currency == "total")].set_index("period")
    # January's flow is the published figure; later months are the difference.
    assert series.value_flow[dt.date(2026, 1, 1)] == series.value[dt.date(2026, 1, 1)]
    assert series.value_flow[dt.date(2026, 2, 1)] == pytest.approx(
        series.value[dt.date(2026, 2, 1)] - series.value[dt.date(2026, 1, 1)]
    )
    # And the de-cumulated months are of a similar size, unlike the raw series.
    flows = series.value_flow[[dt.date(2025, 11, 1), dt.date(2025, 12, 1),
                               dt.date(2026, 1, 1), dt.date(2026, 2, 1)]]
    assert flows.max() / flows.min() < 1.5


def test_every_label_stated_identity_holds(bulletin):
    """59 of the 60 formulas parsed out of the labels reconcile exactly, in every
    period and currency. The one that does not is registered with its measured
    band, so a NEW divergence still fails the build."""
    unregistered = []
    for slug, frame in bulletin.items():
        for failure in check_identities(frame, slug):
            if not failure["passed"]:
                unregistered.append(failure)
    assert unregistered == []

    capital = check_identities(bulletin["sermaye_yeterliligi"], "sermaye_yeterliligi")
    assert len(capital) == EXPECTED_PERIODS      # the registered exception, every month
    assert all(f["passed"] for f in capital)
    assert ("sermaye_yeterliligi", "kredi_riskine_esas_tutar") in KNOWN_IDENTITY_EXCEPTIONS


@pytest.mark.parametrize(
    "formula,expanded",
    [
        ("(2+3+4)", "2+3+4"),
        ("(3+..+9)", "(3+4+5+6+7+8+9)"),
        ("(40+…+51)", "(40+41+42+43+44+45+46+47+48+49+50+51)"),
        ("(19+….+35)", "(19+20+21+22+23+24+25+26+27+28+29+30+31+32+33+34+35)"),
        ("(10+...+22+25)", "(10+11+12+13+14+15+16+17+18+19+20+21+22+25)"),
        ("[(26+34+50)-45]", "((26+34+50)-45)"),
        ("(15-23)", "(15-23)"),
        ("(2 den 5'ya)", "2+3+4+5"),
        # A ratio definition is not an additive identity and must be skipped.
        ("((6/7)*100)", None),
    ],
)
def test_expand_formula(formula, expanded):
    result = expand_formula(formula)
    if expanded is None:
        assert result is None
    else:
        assert result.replace("(", "").replace(")", "") == expanded.replace("(", "").replace(")", "")


def test_published_methodology_notes_are_captured():
    """BDDK states, per table, rows that sit outside their own totals.

    Dropping `Json.uyari` loses the only statement that 'Bankalara Kullandırılan
    Krediler' is excluded from table 5's arithmetic -- a trap no identity check
    catches, because the published totals reconcile without that row.
    """
    from backend.parsing.bddk_bulletin import parse_bulletin_footnotes
    from backend.core.config import RAW_BDDK_JSON_DIR

    notes = parse_bulletin_footnotes(RAW_BDDK_JSON_DIR)
    assert not notes.footnote.str.contains("<").any()      # HTML is stripped

    by_dataset = notes.groupby("dataset").footnote.apply(" ".join)
    assert "hesaplamalara dahil edilmemiştir" in by_dataset["sektorel_kredi_dagilimi"]
    assert "tek bir m" in by_dataset["kobi_kredileri"]      # one customer counted once

    # The text is period-dependent: table 12's note changed the same month its
    # capital-adequacy formula did, which is why a single note per table is wrong.
    capital = notes[notes.dataset == "sermaye_yeterliligi"].sort_values("first_period")
    assert len(capital) == 2
    assert "Kredi değerleme ayarlamaları" in capital.iloc[0].footnote
    assert str(capital.iloc[1].first_period) == "2021-11-01"


def test_risk_weight_buckets_reconstruct_their_parent(bulletin):
    """The sixteen risk-weight rows of table 12 are checked by nothing else.

    They carry no formula, and their parent's formula addresses rows from the
    other direction, so a dropped or mis-parented bucket would pass every other
    check. The weight is read from the child's own label.
    """
    from backend.validation.continuity import check_risk_weight_identity

    results = check_risk_weight_identity(
        bulletin["sermaye_yeterliligi"], "sermaye_yeterliligi"
    )
    assert len(results) == EXPECTED_PERIODS
    assert all(r["passed"] for r in results)

    # Only table 12 declares weights; the rule must not fire elsewhere.
    for slug in ("bilanco", "likidite_durumu", "mevduat_turler"):
        assert check_risk_weight_identity(bulletin[slug], slug) == []


def test_dropping_a_risk_weight_bucket_is_caught(bulletin):
    """The check has to be tight enough to fail on a real extraction loss."""
    from backend.validation.continuity import check_risk_weight_identity

    frame = bulletin["sermaye_yeterliligi"]
    heaviest = frame[frame.entity_name.str.contains("Risk Ağırlığı %100")]
    damaged = frame.drop(index=heaviest.index)

    failures = [r for r in check_risk_weight_identity(damaged, "sermaye_yeterliligi")
                if not r["passed"]]
    assert len(failures) == EXPECTED_PERIODS
