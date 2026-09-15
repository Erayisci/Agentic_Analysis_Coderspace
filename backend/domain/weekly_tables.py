"""Registry of the nine BDDK weekly-bulletin tables.

The weekly bulletin is a different product from the monthly one, not a finer
grain of it: nine tables against seventeen, 201 line items against ~450, and a
Friday observation date rather than a month end. What it buys is timeliness --
the weekly figure for a month is published before the monthly bulletin is.

Two things about the source shape the whole path and are worth stating here,
because they are the opposite of the monthly bulletin's:

`Gelişmiş Gösterim` returns a DATE RANGE in one response.
    The monthly endpoint answers one (table, month) at a time, so the archive is
    one file per table per month. Here a single POST returns every week between
    two dates for every requested item, so the whole 2021..2026 corpus is nine
    requests -- one per table -- and the archive is nine files. A refresh
    re-fetches all nine rather than appending, which is cheap enough to be the
    simplest correct thing.

BDDK publishes the item lifecycle instead of leaving it to be measured.
    Each item in the picker carries its row code and, when it has been retired,
    `Sonlandırılma Tarihi= 10-09-2022`. The monthly path had to discover its
    `KNOWN_LIFECYCLES` by measuring coverage; here the registry is published, so
    `parsing.bddk_weekly` reads it and the continuity check compares the data
    against BDDK's own statement rather than against ours. 22 of the 201 items
    are retired, and two whole tables were replaced wholesale: 291 at 2022-09-10
    and 297 at 2023-01-09.

Item keys are BDDK's own numeric ids, not the label.
    Table 297 retired `Bilanço İçi Yabancı Para Pozisyonu (2-3)` and introduced
    an item with the SAME label on the same date. Keying on the label -- which
    is what the monthly path does, for want of anything better -- would splice
    two different definitions into one series across that boundary. The weekly
    endpoint hands us a stable id per item, so the key is the id and the label
    is only a name.
"""
from typing import Dict, NamedTuple, Optional, Tuple


class WeeklyTable(NamedTuple):
    """One weekly-bulletin table.

    table_id   BDDK's own id, the `Kalemler-<id>` block in the picker and the
               `tabloId` the report groups its columns under.
    items_seen item count observed at 2026-09-04, retired items included. A
               mismatch is reported rather than fatal: BDDK adds line items.
    semantics  every weekly table is a period-end level on the observation date.
               Nothing here is cumulative -- the income statement, which is the
               monthly bulletin's one cumulative table, has no weekly edition.
    """

    table_id: int
    slug: str
    title: str
    items_seen: int
    semantics: str = "stock"


TABLES: Tuple[WeeklyTable, ...] = (
    WeeklyTable(289, "krediler", "Krediler", 24),
    WeeklyTable(290, "takipteki_alacaklar", "Takipteki Alacaklar", 13),
    WeeklyTable(291, "menkul_degerler", "Menkul Değerler", 22),
    WeeklyTable(292, "mevduat", "Mevduat", 12),
    WeeklyTable(293, "diger_bilanco_kalemleri", "Diğer Bilanço Kalemleri", 16),
    WeeklyTable(294, "bilanco_disi_islemler", "Bilanço Dışı İşlemler", 4),
    WeeklyTable(295, "saklanan_menkul_degerler_nominal",
                "Bankalarda Saklanan Menkul Değerler - 1", 45),
    WeeklyTable(296, "saklanan_menkul_degerler_piyasa",
                "Bankalarda Saklanan Menkul Değerler - 2", 45),
    WeeklyTable(297, "yabanci_para_pozisyonu", "Yabancı Para Pozisyonu", 20),
)

BY_ID: Dict[int, WeeklyTable] = {t.table_id: t for t in TABLES}
BY_SLUG: Dict[str, WeeklyTable] = {t.slug: t for t in TABLES}

# Which entity a row of each table describes, mirroring
# `bulletin_tables.ENTITY_TYPE` so the two fact tables share one vocabulary.
ENTITY_TYPE: Dict[int, str] = {
    289: "loan_product",
    290: "loan_product",
    291: "security_type",
    292: "deposit_type",
    293: "balance_sheet_item",
    294: "off_balance_item",
    295: "security_type",
    296: "security_type",
    297: "fx_position_item",
}

# The three columns every table publishes, as the picker names them and as the
# result header spells them. `Toplam` is TP+YP, so summing all three triple
# counts -- the same trap as `bulletin_observations.currency`.
#
# The mapping is onto THAT table's vocabulary, not a new one: the weekly and
# monthly corpora describe the same banking sector, and an agent that learned
# `currency='FX'` from one must not miss the other because it spells the same
# split `yp`. TP (Türk parası) is TL and YP (yabancı para) is FX.
CURRENCY_COLUMNS: Tuple[str, ...] = ("TP", "YP", "Toplam")
CURRENCY_KEYS: Dict[str, str] = {"TP": "TL", "YP": "FX", "TOPLAM": "total", "Toplam": "total"}

# taraf 10001 is the whole banking sector, the same code the monthly downloader
# pins, so the two corpora describe the same population.
TARAF = 10001

# Stated by the report itself ('Birim: Milyon TL') and verified per response by
# the parser, never defaulted -- see `parsing.bddk_weekly.stated_unit`.
UNIT = "milyon TL"

# Labels whose published formula does not address the rows it means.
#
# 60 of the 201 weekly labels state an arithmetic identity and 59 of them hold
# exactly, in every week and every currency column. The one below does not, and
# the reason is visible in the table: BDDK inserted a row above it and did not
# renumber the labels beneath, so `Bankalardan Alacaklar (5+6)` now points at
# itself and the row under it. Measured against the archive, the identity it
# means -- rows 6 and 7, 'a) Yurtiçi Bankalar' + 'b) Yurtdışı Bankalar' -- holds
# in all 296 weeks with no exception:
#
#     2026-09-04  1.811.667,51698 == 829.589,27079 + 982.078,24619
#
# The published text is not edited away: it stays in `entity_name`, which is the
# label verbatim. This registry only says which positions the row's arithmetic
# actually addresses, so parentage and the identity check work on the rows BDDK
# means rather than the ones its stale numbering names.
#
#   (table slug, item id): the formula that holds
WEEKLY_FORMULA_OVERRIDES: Dict[Tuple[str, int], str] = {
    ("diger_bilanco_kalemleri", 5745): "(6+7)",
}

# Active items that legitimately do not span 2021-01-08..2026-09-04.
#
# BDDK publishes the END of an item's life (`Sonlandırılma Tarihi`) but not its
# start, so introductions are measured, exactly as the monthly corpus's
# `KNOWN_LIFECYCLES` are. Three items start late and each start is a known
# event, not a data problem:
#
#   2022-02-18  Kur Korumalı Mevduat -- the scheme itself began in December 2021
#               and the weekly bulletin added the line two months later.
#   2022-09-23  the two overdraft-account (KMH) memo lines, added when BDDK
#               split overdrafts out of consumer and commercial credit.
#
#   (table slug, entity key): (first period or None, last period or None)
KNOWN_WEEKLY_LIFECYCLES: Dict[Tuple[str, str], Tuple[Optional[str], Optional[str]]] = {
    ("mevduat", "5872"): ("2022-02-18", None),
    ("krediler", "5882"): ("2022-09-23", None),
    ("krediler", "5883"): ("2022-09-23", None),
}

# Weeks a table published nothing for a set of items.
#
# Table 297 was re-issued on 2023-01-09: ten items were retired and ten took
# their place. The replacements are backfilled to 2021 -- they are the same
# numbers, measured -- but the backfill misses one week. 2022-01-07 exists in
# the SUPERSEDED definition (items 5850..5859) and not in the current one, so
# the week is published; it is the new items that do not reach it.
#
# Registered rather than patched: splicing the old item's value into the new
# item's series would invent a figure BDDK never published under that id.
#
#   (table slug): weeks with no observation for that table's current items
KNOWN_WEEKLY_GAPS: Dict[str, Tuple[str, ...]] = {
    "yabanci_para_pozisyonu": ("2022-01-07",),
}

# Series the weekly and the monthly bulletin both publish, used to check one
# corpus against the other.
#
# The two are different releases of the same banking sector, so the same
# quantity appears in both -- but the weekly one is a flash figure published
# days after the observation date and the monthly one is the revised figure.
# They therefore agree closely without agreeing exactly, which is what makes
# this a useful check: an ingestion error moves a figure by orders of magnitude,
# a revision moves it by hundredths of a percent.
#
# Alignment is only possible where a weekly observation date IS a month end.
# The weekly bulletin observes on Fridays (on the last business day when Friday
# is a holiday), so that happens 10 times in the 296 weeks of this corpus -- few,
# but they are exact comparisons rather than interpolated ones.
#
# Measured over those 10 dates, the five pairs below diverge by a median 0.0009
# to 0.064% and a maximum of 0.19%. The ceiling is 0.5%, about 2.6x the largest
# observed gap.
#
# One pair was measured and deliberately LEFT OUT. Weekly
# 'Takipteki Alacaklar / a) Konut' against monthly 'Takipteki Konut Kredileri'
# diverges by a median 1.19% and a maximum 3.90% -- twenty times the others, in
# every one of the 10 months. That is a scope difference between two definitions
# of non-performing housing credit, not a revision, so it would make the check
# blind rather than strict. It belongs in a reconciliation monitor, not here.
#
#   (weekly slug, weekly item id, monthly dataset, monthly entity key, monthly metric)
WEEKLY_MONTHLY_PAIRS: Tuple[Tuple[str, str, str, str, str], ...] = (
    ("krediler", "5687", "krediler", "toplam_krediler", "toplam"),
    ("krediler", "5690", "tuketici_kredileri", "tuketici_kredileri_konut", "balance"),
    ("krediler", "5691", "tuketici_kredileri", "tuketici_kredileri_tasit", "balance"),
    ("takipteki_alacaklar", "5709", "bilanco", "takipteki_alacaklar", "balance"),
    ("mevduat", "5730", "bilanco", "mevduat_katilim_fonu", "balance"),
)

WEEKLY_MONTHLY_TOLERANCE_PCT = 0.5
