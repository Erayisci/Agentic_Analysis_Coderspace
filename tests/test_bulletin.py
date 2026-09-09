"""Tests for the generic BDDK bulletin parser.

These read the archived JSON responses directly, so they do not need a build.
The point of most of them is that a *silent* change in the published tables
becomes a loud failure: the corpus is keyed on normalised labels, and three of
the seventeen tables reshuffle their rows mid-history.
"""
import pytest

from backend.domain.bulletin_tables import BY_SLUG, KNOWN_LIFECYCLES, TABLES
from backend.core.config import RAW_BDDK_JSON_DIR
from backend.core.labels import canonical_key, qualified_key, slugify, strip_decorations
from backend.parsing.bddk_bulletin import parse_bulletin_table, split_measure
from backend.validation.continuity import check_continuity

EXPECTED_PERIODS = 67  # 2021-01 .. 2026-07


@pytest.fixture(scope="module")
def bulletin():
    """Every table parsed once; the whole corpus is ~135k rows."""
    return {t.slug: parse_bulletin_table(RAW_BDDK_JSON_DIR, t.slug) for t in TABLES}


# --- label normalisation ---------------------------------------------------

@pytest.mark.parametrize(
    "raw,name,formula,footnote",
    [
        ("Tüketici Kredileri (2+3+4)", "Tüketici Kredileri", "(2+3+4)", ""),
        ("Ortaklık Finansmanı*", "Ortaklık Finansmanı", "", "*"),
        ("Menkul Değerler (2 den 26'ya)", "Menkul Değerler", "(2 den 26'ya)", ""),
        ("Mudi Sayısı - Yurt İçi Yerleşik*", "Mudi Sayısı - Yurt İçi Yerleşik", "", "*"),
        ("Krediler", "Krediler", "", ""),
        # A parenthetical that is part of the name must survive.
        ("Opsiyon İşlemleri (Varlık)", "Opsiyon İşlemleri (Varlık)", "", ""),
    ],
)
def test_strip_decorations(raw, name, formula, footnote):
    assert strip_decorations(raw) == (name, formula, footnote)


def test_footnote_and_formula_do_not_change_the_key():
    """The two decorations BDDK adds mid-history must not invent a new series."""
    assert canonical_key("Ortaklık Finansmanı") == canonical_key("Ortaklık Finansmanı*")
    assert canonical_key("Menkul Değerler (2 den 24'e)") == canonical_key(
        "Menkul Değerler (2 den 26'ya)"
    )


def test_a_genuine_rename_does_change_the_key():
    """Normalisation must not be so aggressive that a real rename slips through."""
    assert canonical_key("Ortaklık Finansmanı") != canonical_key("Mal Karşılığı Vesaikin Finansmanı")


def test_slugify_transliterates_turkish():
    assert slugify("Otel ve Restoranlar (Turizm)") == "otel_ve_restoranlar_turizm"


def test_qualified_key():
    assert qualified_key("tp_mevduat", "a_gercek_kisiler") == "tp_mevduat/a_gercek_kisiler"
    assert qualified_key(None, "krediler") == "krediler"


@pytest.mark.parametrize(
    "field,metric,currency",
    [
        ("Tp", "balance", "TL"),
        ("Yp", "balance", "FX"),
        ("Toplam", "balance", "total"),
        ("KisaTp", "kisa", "TL"),
        ("NakdiKrediToplam", "nakdi_kredi", "total"),
        ("KisaVadeliNakdi", "kisa_vadeli_nakdi", None),
        ("Rasyo", "rasyo", None),
    ],
)
def test_split_measure(field, metric, currency):
    assert split_measure(field) == (metric, currency)


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
    assert BY_SLUG["tuketici_kredileri"].unit == "bin TL"
    assert BY_SLUG["diger_bilgiler"].unit == "Adet"
