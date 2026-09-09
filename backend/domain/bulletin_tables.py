"""Registry of the 17 BDDK monthly-bulletin tables, shared by the downloader
and the parser so neither can drift from the other.

`tabloNo` maps 1:1 onto the bulletin sidebar order. Tables 1, 3, 4, 5, 9 and 15
were verified against their published row labels; the rest were matched by
sidebar position and column shape, so confirm the title the first time you
parse one.
"""
from typing import Dict, NamedTuple, Optional, Tuple


class Table(NamedTuple):
    """One bulletin table.

    row_kind   literal written into column A of the Excel rendering.
    rows_seen  row count observed at 2026-06. A mismatch is reported, never
               fatal: older months legitimately carry fewer rows.
    strict     assert the row count and a contiguous 1..N code sequence. Only
               table 5 is strict -- its layout is pinned by the archived corpus
               and by tests, so drift there must fail loudly.
    unit       unit of the measure columns, or None when the table mixes units
               per row.
    hierarchy  how a detail row finds its parent. `BasitFont` is a presentation
               attribute, so this was measured per table rather than assumed:

               'leading'  a row belongs to the most recent non-italic row above
                          it. Sixteen tables read this way, including the two
                          deposit tables where 'a) Gerçek Kişiler' repeats under
                          every deposit type.
               'trailing' a row belongs to the next bold row below it. Table 11
                          alone is laid out this way -- its bold rows are
                          section totals ('TOPLAM VARLIKLAR') rather than
                          section headers, and 'Türev İşlemler' appears once on
                          the asset side and once on the liability side.

               Only tables 9, 10 and 11 actually repeat a label within a period;
               for the other fourteen the strategy costs nothing and the parser
               asserts key uniqueness regardless.
    """

    number: int
    slug: str
    title: str
    row_kind: str
    rows_seen: int
    strict: bool = False
    unit: Optional[str] = "bin TL"
    hierarchy: str = "leading"


TABLES: Tuple[Table, ...] = (
    Table(1, "bilanco", "Bilanço", "Kalem", 62),
    Table(2, "kar_zarar", "Kar Zarar", "Kalem", 53),
    Table(3, "krediler", "Krediler", "Kredi Türü", 20),
    Table(4, "tuketici_kredileri", "Tüketici Kredileri", "Kalem", 41),
    Table(5, "sektorel_kredi_dagilimi", "Sektörel Kredi Dağılımı", "Sektör", 70, True),
    Table(6, "kobi_kredileri", "KOBİ Kredileri", "Kalem", 8),
    Table(7, "sendikasyon_sekuritizasyon", "Sendikasyon Sekuritizasyon Kredileri", "Kalem", 3),
    Table(8, "menkul_kiymetler", "Menkul Kıymetler", "Kalem", 29),
    Table(9, "mevduat_turler", "Mevduat Türler İtibarıyla", "Kalem", 26),
    Table(10, "mevduat_vade", "Mevduat Vade İtibarıyla", "Kalem", 24),
    Table(11, "likidite_durumu", "Likidite Durumu", "Kalem", 44, hierarchy="trailing"),
    Table(12, "sermaye_yeterliligi", "Sermaye Yeterliliği", "Kalem", 31),
    Table(13, "yabanci_para_pozisyonu", "Yabancı Para Pozisyonu", "Kalem", 11),
    Table(14, "bilanco_disi_islemler", "Bilanço Dışı İşlemler", "Kalem", 52),
    # Table 15 mixes units per row (%, Bin TL, Gün, Kişi), so it claims none.
    Table(15, "rasyolar", "Rasyolar", "Rasyo", 32, unit=None),
    Table(16, "diger_bilgiler", "Diğer Bilgiler", "Kalem", 7, unit="Adet"),
    Table(17, "yurt_disi_sube_rasyolari", "Yurt Dışı Şube Rasyoları", "Rasyo", 3, unit="%"),
)

BY_NUMBER: Dict[int, Table] = {t.number: t for t in TABLES}
BY_SLUG: Dict[str, Table] = {t.slug: t for t in TABLES}

# Which entity a row of each table describes. Table 5's rows are activity
# sectors; the rest are line items of one kind or another. This is what stops a
# balance-sheet line and a credit sector sharing a key space.
ENTITY_TYPE: Dict[int, str] = {
    1: "balance_sheet_item",
    2: "income_statement_item",
    3: "loan_type",
    4: "loan_product",
    5: "sector",
    6: "loan_product",
    7: "loan_product",
    8: "security_type",
    9: "deposit_type",
    10: "maturity_bucket",
    11: "liquidity_item",
    12: "capital_item",
    13: "fx_position_item",
    14: "off_balance_item",
    15: "ratio",
    16: "counter",
    17: "ratio",
}

# Rows that legitimately do not span the whole 2021-01..2026-07 history.
#
# Every one of these was found by measuring the corpus, not by reading a BDDK
# release note, so each is a fact about the published data. An entity whose
# coverage is incomplete AND absent from this registry is a build failure: that
# is the whole point of the check. Periods are inclusive.
#
#   (table slug, entity key): (first period or None, last period or None)
KNOWN_LIFECYCLES: Dict[Tuple[str, str], Tuple[Optional[str], Optional[str]]] = {
    # Table 3 restructured its loan-type breakdown in 2022-01.
    ("krediler", "mal_karsiligi_vesaikin_finansmani"): (None, "2021-12-01"),
    ("krediler", "vadeli_ticaretin_finansmanindan_alacaklar"): ("2022-01-01", None),
    ("krediler", "mali_kesime_banka_disi_kullandirilan_krediler"): ("2022-01-01", None),
    ("krediler", "yurt_disi_yerlesiklere_kullandirilan_krediler"): ("2022-01-01", None),
    # Gold-denominated instruments were added to the securities table in 2022-09.
    ("menkul_kiymetler", "altin_tahvili"): ("2022-09-01", None),
    ("menkul_kiymetler", "altina_dayali_kira_sertifikasi_sukuk"): ("2022-09-01", None),
    # Capital adequacy is the least stable table; it reshuffled three times.
    (
        "sermaye_yeterliligi",
        "d_karsi_taraf_kredi_riski_dahil_kredi_riskine_esas_tutar_icsel_derecelendirme_yaklasimi",
    ): (None, "2021-05-01"),
    ("sermaye_yeterliligi", "e_ayarlama_tutari"): (None, "2021-05-01"),
    # These three sit under the standard-approach counterparty-risk parent, so
    # they are registered under their qualified keys.
    ("sermaye_yeterliligi",
     "c_karsi_taraf_kredi_riski_dahil_kredi_riskine_esas_tutar_standart_yaklasim"
     "/risk_agirligi_25_olan_kalemler_toplami"): ("2021-11-01", None),
    ("sermaye_yeterliligi",
     "c_karsi_taraf_kredi_riski_dahil_kredi_riskine_esas_tutar_standart_yaklasim"
     "/kda_riskine_esas_tutar"): ("2021-11-01", None),
    ("sermaye_yeterliligi",
     "c_karsi_taraf_kredi_riski_dahil_kredi_riskine_esas_tutar_standart_yaklasim"
     "/risk_agirligi_500_olan_kalemler_toplami"): ("2022-06-01", None),
}
