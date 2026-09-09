"""Canonical layer: sector hierarchy, metric catalog and the cross-source sector mapping.

Design rules (from project research, empirically verified against the raw files):
- Metric names are SOURCE-PREFIXED and never merged: BDDK 'Takipteki Krediler' and
  TBB 'Tasfiye Olunacak Krediler' are different concepts (~19% apart every month).
- The TBB sector *number* is a size rank that changes monthly; mapping is keyed on
  sector name slugs, resolved once at build time with uniqueness assertions.
- The BDDK hierarchy is a graph, not a flat list. Two traps are encoded explicitly:
  sectors 23/24 are children of 22 (already inside parent 09), and sector 46 is a
  non-additive detail of 45 (excluded from parent 44's sum).
"""

# --------------------------------------------------------------------------- #
# Metric catalog                                                              #
# --------------------------------------------------------------------------- #
# temporal_semantics: every series is a period-end outstanding balance (stock).
# A month-over-month difference is a NET change (new lending minus repayments,
# plus FX revaluation, write-offs and reclassifications) - never "new lending".
METRIC_CATALOG = [
    # metric_id, source, name_turkish, name_english
    ("bddk_short_term_cash", "BDDK", "Kısa Vadeli Nakdi Krediler", "Short-term cash loans"),
    ("bddk_medium_long_term_cash", "BDDK", "Orta ve Uzun Vadeli Nakdi Krediler", "Medium/long-term cash loans"),
    ("bddk_cash_current", "BDDK", "Nakdi Krediler", "Current cash loans"),
    ("bddk_follow_up", "BDDK", "Takipteki Krediler", "Follow-up (non-performing) loans"),
    ("bddk_total_cash", "BDDK", "Toplam Nakdi Krediler", "Total cash loans (current + follow-up)"),
    ("bddk_noncash", "BDDK", "Gayri Nakdi Krediler", "Non-cash loans"),
    ("tbb_gross", "TBB_RM", "Brüt Krediler", "Gross loans (cash + liquidation)"),
    ("tbb_cash", "TBB_RM", "Nakdi Krediler", "Cash loans (incl. interest accruals)"),
    ("tbb_liquidation", "TBB_RM", "Tasfiye Olunacak Krediler", "Loans to be liquidated"),
]

# --------------------------------------------------------------------------- #
# BDDK sector hierarchy (codes are stable across all months)                  #
# --------------------------------------------------------------------------- #
# parent code -> additive children (parent value == sum of children values)
BDDK_CHILDREN = {
    1: [2, 3, 4],
    6: [7, 8],
    9: [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 25],
    22: [23, 24],                # transport-equipment split, nested inside 09
    28: [29, 30, 31],
    32: [33, 34, 35],
    36: [37, 38, 39, 40, 41, 42, 43],
    44: [45, 47, 48, 49],        # 46 is deliberately NOT a sibling here
    50: [51, 52, 53, 54],
    58: [59, 60, 61, 62],
}

# code -> code it details; NON-additive (a subset shown for information only)
BDDK_DETAIL_OF = {46: 45}

# Top-level sectors: these sum exactly to the TOPLAM row (code 70)
BDDK_TOP_LEVEL = [1, 5, 6, 9, 26, 27, 28, 32, 36, 44, 50, 55, 56, 57, 58, 63, 64, 65, 66, 67, 68, 69]

BDDK_TOTAL_CODE = 70


def build_bddk_sector_table(sector_names: dict) -> list:
    """Rows for the BDDK sector dimension: (code, name, parent, relation, is_leaf)."""
    parent_of = {}
    for parent, children in BDDK_CHILDREN.items():
        for child in children:
            parent_of[child] = (parent, "child")
    for detail, of in BDDK_DETAIL_OF.items():
        parent_of[detail] = (of, "detail")

    rows = []
    for code in range(1, 71):
        if code == BDDK_TOTAL_CODE:
            relation, parent = "total", None
        elif code in parent_of:
            parent, relation = parent_of[code]
        else:
            relation, parent = "top_level", None
        is_leaf = code not in BDDK_CHILDREN and relation != "total"
        rows.append(
            {
                "sector_code": f"{code:02d}",
                "sector_name": sector_names[f"{code:02d}"],
                "parent_code": f"{parent:02d}" if parent else None,
                "relation": relation if relation != "top_level" else "top_level",
                "is_leaf": is_leaf,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Canonical sectors and the BDDK <-> TBB crosswalk                            #
# --------------------------------------------------------------------------- #
# Keyed on: BDDK stable numeric codes, TBB stable name slugs (never TBB ranks).
# relation_type follows the research handoff: exact / aggregate / component /
# approximate / unmapped. mapping_confidence: high / medium.
#
# tbb_members lists the TBB main-sector slugs whose SUM is comparable to the
# BDDK side. Sub-sector-level pairs (housing/auto/other/card) are mapped
# separately below.
CANONICAL_SECTORS = [
    # canonical_id, name_english, bddk_codes, tbb_member_slugs, relation, confidence
    ("agriculture", "Agriculture, hunting, forestry", [1], ["tarim_avcilik_ormancilik"], "exact", "high"),
    ("fishing", "Fishing", [5], ["balikcilik"], "exact", "high"),
    ("mining", "Mining and quarrying", [6],
     ["enerji_ureten_madenlerin_cikarilmasi", "enerji_uretmeyen_madenlerin_cikarilmasi"], "aggregate", "high"),
    ("manufacturing", "Manufacturing", [9],
     ["gida_mesrubat_ve_tutun_san", "tekstil_ve_tekstil_urunleri_san", "deri_ve_deri_urunleri_sanayi",
      "agac_ve_agac_urunleri_san", "kagit_ham_ve_kagit_urnl_basim_san",
      "nukleer_yakit_raf_ve_petr_ur_komur_ur", "kimya_ve_kimya_urunleri_ile_sent_lif_san",
      "kaucuk_ve_plastik_ur_san", "diger_metal_disi_madenler_san",
      "metal_ana_san_ve_islenmis_mad_urt", "makina_ve_techizat_san",
      "elektrikli_ve_optik_aletler_san", "ulasim_araclari_san",
      "baska_yerlerde_siniflandirilmamis_imalat_sanayii"], "aggregate", "high"),
    ("electricity_gas_water", "Electricity, gas and water", [26], ["elektrik_gaz_ve_su_kaynaklari"], "exact", "high"),
    ("construction", "Construction", [27], ["insaat"], "exact", "high"),
    ("trade", "Wholesale and retail trade", [28],
     ["toptan_perakende_tic_komisync_motorlu_arac_servis_hizm"], "exact", "high"),
    ("tourism", "Hotels and restaurants (tourism)", [32], ["otel_ve_restoranlar_turizm"], "exact", "high"),
    ("transport_storage_comm", "Transport, storage and communication", [36],
     ["tasimacilik_depolama_ve_haberlesme"], "exact", "high"),
    ("financial_intermediation", "Financial intermediation", [44], ["finansal_aracilik"], "exact", "high"),
    ("real_estate_business", "Real estate, renting and business activities", [50],
     ["emlak_komisyon_kiralama_ve_isletmecilik_faaliyetleri"], "exact", "high"),
    ("public_admin_defense", "Public administration, defense, social security", [55],
     ["savunma_ve_kamu_yonetimi_zorunlu_sosyal_guvenlik_kurumlari"], "exact", "high"),
    ("education", "Education", [56], ["egitim"], "exact", "high"),
    ("health_social_work", "Health and social work", [57], ["saglik_ve_sosyal_hizmetler"], "exact", "high"),
    ("other_services", "Other community, social and personal services", [58],
     ["diger_toplumsal_sosyal_ve_kisisel_hizmetler"], "approximate", "medium"),
    ("household_employers", "Private households employing staff", [63],
     ["isci_calistiran_ozel_kisiler"], "exact", "high"),
    ("personal_credit", "Personal credit (housing + auto + other + cards)", [65, 66, 67, 68],
     ["bireysel_krediler"], "aggregate", "high"),
    # BDDK-only residual categories - no TBB counterpart
    ("intl_organizations", "International organizations", [64], [], "unmapped", "high"),
    ("other_unclassified", "Other / unclassified", [69], [], "unmapped", "high"),
]

# Manufacturing sub-sector pairs (BDDK child code -> TBB main slug), all high confidence.
MANUFACTURING_PAIRS = {
    10: "gida_mesrubat_ve_tutun_san",
    11: "tekstil_ve_tekstil_urunleri_san",
    12: "deri_ve_deri_urunleri_sanayi",
    13: "agac_ve_agac_urunleri_san",
    14: "kagit_ham_ve_kagit_urnl_basim_san",
    15: "nukleer_yakit_raf_ve_petr_ur_komur_ur",
    16: "kimya_ve_kimya_urunleri_ile_sent_lif_san",
    17: "kaucuk_ve_plastik_ur_san",
    18: "diger_metal_disi_madenler_san",
    19: "metal_ana_san_ve_islenmis_mad_urt",
    20: "makina_ve_techizat_san",
    21: "elektrikli_ve_optik_aletler_san",
    22: "ulasim_araclari_san",
    25: "baska_yerlerde_siniflandirilmamis_imalat_sanayii",
}

# Personal-credit sub-sector pairs (BDDK top-level code -> TBB SUB-sector slug).
PERSONAL_CREDIT_PAIRS = {
    65: "bireysel_kredi_konut",
    66: "bireysel_kredi_otomobil",
    67: "bireysel_kredi_diger",
    68: "kredi_karti",
}

# Comparable metric pairs for cross-source reconciliation. The two members of
# each pair are conceptually analogous but methodologically DIFFERENT series;
# they are compared, never merged.
RECONCILIATION_METRIC_PAIRS = {
    "cash": ("bddk_cash_current", "tbb_cash"),
    "trouble": ("bddk_follow_up", "tbb_liquidation"),
    "total": ("bddk_total_cash", "tbb_gross"),
}


def validate_crosswalk_against_data(tbb_slugs_main: set, tbb_slugs_sub: set) -> None:
    """Fail loudly if any curated TBB slug does not match the parsed data exactly."""
    referenced_main = set()
    for _, _, _, members, _, _ in CANONICAL_SECTORS:
        referenced_main.update(members)
    referenced_main.update(MANUFACTURING_PAIRS.values())
    missing = referenced_main - tbb_slugs_main
    if missing:
        raise ValueError(f"Crosswalk references unknown TBB main-sector slugs: {sorted(missing)}")

    missing_sub = set(PERSONAL_CREDIT_PAIRS.values()) - tbb_slugs_sub
    if missing_sub:
        raise ValueError(f"Crosswalk references unknown TBB sub-sector slugs: {sorted(missing_sub)}")
