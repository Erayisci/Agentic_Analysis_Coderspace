"""Registry of the seven BDDK FinTurk (il-bazli / geographic distribution) tables.

FinTurk is a third BDDK product, distinct from both bulletins it sits beside on
bddk.org.tr's "Veriler" page: it is QUARTERLY (Mar/Jun/Sep/Dec), not monthly or
weekly, and it is broken down by PROVINCE (81 iller + "YURT DIŞI"), not by
balance-sheet line or loan product. The same JSON envelope shape as the monthly
bulletin -- `Json.colModels`, `colNames`, `data.rows[].cell` -- comes back from
`BultenFinturk/tr/Home/VeriGetir`, measured live.

Unlike the monthly/weekly bulletins, the endpoint's PascalCase field ids
(`colModels[i]['name']`, e.g. `AltinDepoGercek`) do not derive predictably from
the Turkish label BDDK shows for the same column (`colNames[i]`, "Altın
Mevduatı-Gerçek Kişi") -- measured across all seven tables, the two vocabularies
diverge in non-obvious ways (`SubeSayisi` vs. the dropdown's "Şubeler" title,
`AltinKredi` singular vs. "Altın Kredileri" plural). So this registry does NOT
hardcode either column-name list: `parsing.bddk_finturk` reads both straight out
of each archived response and uses the Turkish `colNames` label as the metric's
display name, exactly as `parsing.bddk_bulletin` prefers `colModels[i]['label']`
over the field id. A hardcoded list here would drift the first time it disagreed
with a live response and nothing would notice.

Every table's response carries the same five leading columns before its own
measures: `EftKodu` (the requested `taraf` code, echoed back), `Yil`, `Ay`
(always 3/6/9/12, the quarter-end month), `Sehir`, `Grup` (the taraf's Turkish
name). `taraf` is a bank-group split like the monthly bulletin's, but wider:
10001 is the whole sector and 10002..10007 break it down by ownership/type
(Mevduat, Kalkınma ve Yatırım, Katılım, Yabancı, Kamu, Yerli Özel) rather than
by an individual bank -- there is no bank-level breakdown here.

No table's column carries a stated arithmetic formula (unlike the monthly and
weekly bulletins' row labels), so there is nothing here resembling
`WEEKLY_FORMULA_OVERRIDES` or `KNOWN_LIFECYCLES` -- no published identity to
check, and every province has published every quarter in the corpus window
measured so far, so no lifecycle registry either. Table 5 ("Oranlar") is
plainly computed FROM tables 1-3 (its own column names name the ratio), but
that is not machine-checkable without re-deriving BDDK's exact denominator
convention, so it is left as data rather than turned into an unverified check.
"""
from typing import Dict, NamedTuple, Tuple


class FinturkTable(NamedTuple):
    """One FinTurk table ("Bilgi" in the UI's dropdown).

    number  the `tabloNo` the endpoint expects, and the dropdown's own value.
    slug    used for the raw-JSON directory and `dataset` in the parsed frame.
    title   the dropdown's own label, verbatim.
    unit    stated by the dropdown label's own parenthetical for every column
            except table 6, which mixes counts and per-capita TL amounts --
            see `METRIC_UNIT_OVERRIDES`.
    """

    number: int
    slug: str
    title: str
    unit: str


TABLES: Tuple[FinturkTable, ...] = (
    FinturkTable(1, "krediler", "Krediler", "bin TL"),
    FinturkTable(2, "mevduat", "Mevduat", "bin TL"),
    FinturkTable(3, "bireysel_bankacilik", "Bireysel Bankacılık", "bin TL"),
    FinturkTable(4, "sektorel_krediler", "Seçilmiş Sektörel Krediler", "bin TL"),
    FinturkTable(5, "oranlar", "Oranlar", "%"),
    FinturkTable(6, "subeler_ve_nufus", "Şubeler (Adet) ve Nüfusa Göre Dağılım (TL)", "bin TL"),
    FinturkTable(7, "altin", "Altın Kredileri ve Altın Mevduatı", "bin TL"),
)

BY_NUMBER: Dict[int, FinturkTable] = {t.number: t for t in TABLES}
BY_SLUG: Dict[str, FinturkTable] = {t.slug: t for t in TABLES}

# Table 6's own unit label ("Adet ve Nüfusa Göre Dağılım (TL)") is compound --
# exactly the trap `parsing.evds.resolve_unit` was built for on the macro side.
# Keyed by the Turkish colName label, measured against a live response, because
# that is what the parser actually has (see the module docstring on why the
# PascalCase field id is not used as a key anywhere in this registry).
METRIC_UNIT_OVERRIDES: Dict[str, str] = {
    "Yurtiçi Şube Sayısı": "adet",
    "Şubeye Düşen Nüfus": "kişi",
    "Kişi Başı Nakdi Kredi": "TL",
    "Kişi Başı Takipteki Alacak": "TL",
    "Kişi Başı Tasarruf Mevduatı": "TL",
    "Kişi Başı Toplam Mevduat": "TL",
}

# taraf (bank-group breakdown), the same convention the monthly bulletin pins
# to 10001 (the whole sector). FinTurk exposes the wider set as first-class
# `Grup` values rather than a fixed request parameter, so every group is
# fetched rather than filtered down to one -- an agent asking about a specific
# ownership group (e.g. "kamu bankaları") needs it as data, not as a filter
# the ingestion already discarded.
TARAF_GROUPS: Dict[int, str] = {
    10001: "SEKTÖR",
    10002: "MEVDUAT",
    10003: "KALKINMA VE YATIRIM",
    10004: "KATILIM",
    10005: "YABANCI",
    10006: "KAMU",
    10007: "YERLİ ÖZEL",
}

# "HEPSİ" ("all") in the province multi-select returns every province as its
# own row plus "YURT DIŞI" (customers booked abroad) -- not a national total
# row. There is no published Türkiye-wide aggregate in this product; summing
# the provinces is the only way to one, and that is left to the query, not
# invented here.
ALL_PROVINCES = "HEPSİ"
