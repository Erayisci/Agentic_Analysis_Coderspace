"""Registry of the TCMB EVDS data groups the lakehouse carries.

EVDS publishes ~680 live data groups. The brief asks for "all series 2021-01..
2026-06", which is not literally feasible (tens of thousands of series), so
the selection rule is written down here and enforced by the downloader:
everything TCMB/TÜİK publish about credit, interest rates, prices, housing and
the macro backdrop the demo scenario needs, in three tiers:

    0  the reference demo scenario cannot run without it
    1  banking core: rate and balance series that pair with the BDDK tables
    2  macro context for the anomaly / causality / change-detection tools

Every group declares how its series behave in time and how they are brought
to the monthly grain the lakehouse analyses at. Both are declarations, never
inferred from the data at query time (Launch.MD §4.2):

    semantics     stock   period-end level (balances, counts)
                  flow    amount within the period (sales, spending)
                  rate    a percentage or price observed at a point in time
                  index   an index number with a stated base
    monthly_rule  last | avg | sum  -- applied when the native frequency is
                  finer than monthly. Coarser series (quarterly) keep their own
                  grain and are stamped on the last month of the quarter.

Series names, units and frequencies are NOT hand-typed: they come from the
archived `serieList` responses, so the registry cannot drift from what TCMB
publishes. A group is pulled whole unless `series` (explicit codes) or
`max_level` (SEVIYE cut-off) narrows it; `overrides` re-declares individual
series that do not share the group's semantics.
"""
from typing import Dict, NamedTuple, Optional, Tuple

SEMANTICS = ("stock", "flow", "rate", "index")
MONTHLY_RULES = ("last", "avg", "sum")


class DataGroup(NamedTuple):
    code: str
    tier: int
    semantics: str
    monthly_rule: str
    series: Optional[Tuple[str, ...]] = None
    max_level: Optional[int] = None
    overrides: Optional[Dict[str, Tuple[str, str]]] = None
    note: str = ""


GROUPS: Tuple[DataGroup, ...] = (
    # ------------------------------------------------------------------ tier 0
    DataGroup("bie_kt100h", 0, "rate", "avg",
              note="Weekly flow loan rates; TP.KTF12 is the housing-loan rate the demo needs."),
    DataGroup("bie_tukfiy2003", 0, "index", "avg",
              note="TÜFE 2003=100, the one continuous CPI base; the deflator."),
    DataGroup("bie_kfe", 0, "index", "avg",
              note="Konut Fiyat Endeksi, national + NUTS-2 regions."),
    DataGroup("bie_akonutsat1", 0, "flow", "sum",
              note="Total house sales, Türkiye + 81 provinces. Monthly flow, not YTD (verified)."),
    DataGroup("bie_akonutsat2", 0, "flow", "sum",
              note="Mortgaged house sales, same breakdown; ipotekli share is derived at build."),
    DataGroup("bie_apifon", 0, "stock", "last",
              overrides={"TP.APIFON4": ("rate", "avg")},
              note="TCMB funding (mn TL) and weighted average funding cost (%), the policy-rate proxy: "
                   "EVDS3 has no dedicated policy-rate group."),
    # ------------------------------------------------------------------ tier 1
    DataGroup("bie_mt100h", 1, "rate", "avg", note="Weekly flow deposit rates by currency, maturity, holder."),
    DataGroup("bie_kt210a", 1, "rate", "avg", note="Monthly stock loan rates (match BDDK stock balances)."),
    DataGroup("bie_mt210ags", 1, "rate", "avg", note="Monthly stock deposit rates."),
    DataGroup("bie_hpbitablo1", 1, "stock", "last", note="Weekly money supply M1/M2/M3."),
    DataGroup("bie_hpbitablo2", 1, "stock", "last", note="Weekly selected balance-sheet items. Starts 2024-06."),
    DataGroup("bie_hpbitablo3", 1, "stock", "last", note="Weekly TL deposits. Starts 2024-06."),
    DataGroup("bie_hpbitablo4", 1, "stock", "last", note="Weekly FX deposits (mn USD). Starts 2024-06."),
    DataGroup("bie_hpbitablo6", 1, "stock", "last",
              note="Weekly selected loan balances incl. housing by maturity. Starts 2024-06; "
                   "reconciles with BDDK tables 03/04 at month end."),
    DataGroup("bie_krehacbs", 1, "stock", "last", note="Monthly loan volume by bank group and borrower sector."),
    DataGroup("bie_pbtop", 1, "stock", "last", note="Monthly banking-sector balance sheet (TCMB view of BDDK table 01)."),
    DataGroup("bie_kkm", 1, "stock", "last", note="Kur Korumalı Mevduat stock; starts 2021-12 when the scheme began."),
    DataGroup("bie_kkhartut", 1, "flow", "sum", note="Weekly card spending amount."),
    DataGroup("bie_kkislade", 1, "flow", "sum", note="Weekly card transaction count."),
    DataGroup("bie_dkdovytl", 1, "rate", "avg",
              series=("TP.DK.USD.A.YTL", "TP.DK.USD.S.YTL", "TP.DK.EUR.A.YTL", "TP.DK.EUR.S.YTL",
                      "TP.DK.EUR.C.YTL", "TP.DK.GBP.A.YTL", "TP.DK.CHF.A.YTL", "TP.DK.JPY.A.YTL"),
              note="Daily TCMB rates; needed to separate FX revaluation from real growth in YP balances. "
                   "Month-end value is kept alongside the average."),
    DataGroup("bie_tufe1yi", 1, "index", "avg", max_level=1, note="Yİ-ÜFE 2003=100, headline + 4 sections."),
    DataGroup("bie_bkea", 1, "rate", "last",
              series=("TP.BKEA.S001", "TP.BKEA.S024", "TP.BKEA.S042", "TP.BKEA.S049", "TP.BKEA.S056",
                      "TP.BKEA.S092", "TP.BKEA.S095", "TP.BKEA.S097", "TP.BKEA.S117", "TP.BKEA.S120"),
              note="Quarterly bank loan tendency survey: credit standards and demand, business and housing."),
    # ------------------------------------------------------------------ tier 2
    DataGroup("bie_tukfiy2025", 2, "index", "avg", max_level=2, note="TÜFE 2025=100, main groups."),
    DataGroup("bie_oktug2025", 2, "index", "avg", max_level=1, note="Core CPI indicators (2025=100)."),
    DataGroup("bie_ykfe", 2, "index", "avg", note="New-house price index."),
    DataGroup("bie_yokfend", 2, "index", "avg", note="Existing-house price index."),
    DataGroup("bie_ykke", 2, "index", "avg", note="New-tenant rent index, national + NUTS-2."),
    DataGroup("bie_akonutsat3", 2, "flow", "sum", note="First-hand house sales by province."),
    DataGroup("bie_akonutsat4", 2, "flow", "sum", note="Second-hand house sales by province."),
    DataGroup("bie_inyprh2", 2, "flow", "sum",
              series=("TP.IN.RH2.APT.TOP.A", "TP.IN.RH2.APT.TOP.B", "TP.IN.RH2.APT.TOP.C", "TP.IN.RH2.APT.TOP.D",
                      "TP.IN.RH2.EV.TOP.A", "TP.IN.RH2.EV.TOP.B", "TP.IN.RH2.EV.TOP.C", "TP.IN.RH2.EV.TOP.D"),
              note="Building permits: housing supply pipeline."),
    DataGroup("bie_mbgven2", 2, "index", "avg", note="Consumer confidence and components."),
    DataGroup("bie_rkgey2", 2, "index", "avg", note="Real-sector confidence."),
    DataGroup("bie_rkgema", 2, "index", "avg", note="Real-sector confidence, seasonally adjusted."),
    DataGroup("bie_kko2", 2, "rate", "avg",
              series=("TP.KKO2.IS.TOP", "TP.KKO2.IS.CDUR", "TP.KKO2.IS.CNDU"),
              note="Manufacturing capacity utilisation."),
    DataGroup("bie_tsanay2021", 2, "index", "avg", max_level=2, note="Industrial production index 2021=100."),
    DataGroup("bie_yisgucu2", 2, "stock", "last",
              overrides={"TP.YISGUCU2.G6": ("rate", "avg"), "TP.YISGUCU2.G7": ("rate", "avg"),
                         "TP.YISGUCU2.G8": ("rate", "avg")},
              note="Labour force: levels in thousand persons, three ratios in %."),
    DataGroup("bie_gsyhhrccar", 2, "flow", "sum", note="Quarterly nominal GDP, expenditure side."),
    DataGroup("bie_gsyhendex", 2, "index", "avg", note="Quarterly chained-volume GDP index."),
    DataGroup("bie_urbek", 2, "rate", "avg",
              series=("TP.BEK.S01.A.U", "TP.BEK.S01.D.U", "TP.BEK.S01.E.U", "TP.BEK.S01.F.U",
                      "TP.BEK.S02.A.U", "TP.BEK.S02.G.U"),
              note="Market participants' inflation and rate expectations (trimmed means)."),
    DataGroup("bie_ackap2", 2, "flow", "sum",
              series=("TP.AC2.TOP.A", "TP.AC2.TOP.S", "TP.AC2.LTD.A", "TP.KAP2.TOP.A", "TP.KAP2.LTD.A"),
              note="Company openings and closures (TOBB)."),
    DataGroup("bie_abres2", 2, "stock", "last", note="Weekly TCMB reserves (mn USD)."),
    DataGroup("bie_bispolfaiz", 2, "rate", "avg", series=("TP.BISPOLFAIZ.TUR",),
              note="BIS monthly policy-rate table, Türkiye row."),
    DataGroup("bie_mkbrgn", 2, "index", "avg",
              series=("TP.MK.F.BILESIK", "TP.MK.F.BILESIK.TUM"),
              note="BIST 100 / XTUMY closes: the brief names XTUMY as a demo-day URL question."),
    DataGroup("bie_altinbistbul", 2, "rate", "avg",
              series=("TP.ALTINPIYASA.KAP02", "TP.ALTINPIYASA.KAP03", "TP.ALTINPIYASA.KAP05",
                      "TP.ALTINPIYASA.AGORT05", "TP.ALTINPIYASA.HACM02"),
              overrides={"TP.ALTINPIYASA.HACM02": ("flow", "sum")},
              note="BIST gold market: the brief names the precious-metals PDF as a demo-day URL question."),
)

BY_CODE: Dict[str, DataGroup] = {g.code: g for g in GROUPS}

# Series the build derives from the raw ones. Kept here so the catalogue lists
# them like any other series; the arithmetic lives in transform.macro.
# The name is a template, not a sentence, because the property type is not
# constant across the series produced. The suffix after `TP.AKONUTSAT{1,2}.`
# is not the region alone: a leading `K` means Konut and its absence means
# İş Yeri, so KTRTOPLAM and TRTOPLAM are housing and commercial-premises sales
# for the same Türkiye. Naming every derived row "konut" published 83 rows of
# commercial-premises data under a housing name, identical in every character
# to the housing row beside it -- and discovery, having nothing to tell them
# apart, answered a housing question with the commercial one.
DERIVED_SERIES = (
    ("DERIVED.IPOTEKLI_PAY", "bie_akonutsat2", "rate",
     "İpotekli {tip} satışlarının toplam {tip} satışlarına oranı (%)",
     "Mortgaged {tip} sales as % of total {tip} sales"),
)

# The property-type segment as TCMB spells it, mapped to the forms the derived
# name needs: (Turkish mid-sentence, English). Read from the source series' own
# name rather than derived from the `K`, for the same reason series names are
# never hand-typed -- TCMB's spelling is the fact.
#
# The Turkish form is spelled out rather than computed, because `str.lower()`
# is wrong for exactly these words: 'İ'.lower() is 'i' followed by a COMBINING
# DOT ABOVE, so "İş Yeri".lower() produces a name no search can match and no
# reader expects.
PROPERTY_TYPE_WORDS = {
    "Konut": ("konut", "house"),
    "İş Yeri": ("iş yeri", "commercial premises"),
}


def _check_registry() -> None:
    codes = [g.code for g in GROUPS]
    assert len(codes) == len(set(codes)), "duplicate data group in EVDS registry"
    for g in GROUPS:
        assert g.semantics in SEMANTICS, g
        assert g.monthly_rule in MONTHLY_RULES, g
        for _code, (sem, rule) in (g.overrides or {}).items():
            assert sem in SEMANTICS and rule in MONTHLY_RULES, g


_check_registry()
