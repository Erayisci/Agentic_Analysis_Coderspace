"""Integrity checks executed on every build.

Each check appends a row to a data-quality report; any FAILED row aborts the
build so bad data never reaches the analytics layer or the agent.
"""
import pandas as pd

from ..domain.canonical import (
    BDDK_CHILDREN,
    BDDK_DETAIL_OF,
    BDDK_TOP_LEVEL,
    BDDK_TOTAL_CODE,
)
from ..core.config import TOLERANCE_BDDK, TOLERANCE_TBB
from ..core.errors import ValidationError

# The raw BDDK files contain a few tiny publication artifacts (parent/child
# gaps up to 10,000 bin TL, at most 0.013% of the parent). Identities are
# therefore enforced with a relative ceiling; every nonzero gap above the
# absolute tolerance is still surfaced in the report detail for transparency.
RELATIVE_TOLERANCE = 0.0005  # 0.05% of the parent value


def _identity_violations(parent_vals, summed_vals, absolute_tolerance):
    """Count violations of parent == sum(children) under abs+relative tolerance.

    Returns (hard_violations, noted_discrepancies, worst_relative_pct):
    hard violations break the build; noted discrepancies are within the
    relative ceiling but larger than the absolute tolerance (reported, kept).
    """
    gap = (parent_vals - summed_vals).abs()
    ceiling = parent_vals.abs() * RELATIVE_TOLERANCE
    above_absolute = gap > absolute_tolerance
    hard = int((above_absolute & (gap > ceiling)).sum().sum())
    noted = int((above_absolute & (gap <= ceiling)).sum().sum())
    with_parent = gap.where(parent_vals.abs() > 0) / parent_vals.abs() * 100
    worst = float(with_parent.max().max()) if noted or hard else 0.0
    return hard, noted, worst


def _wide(observations: pd.DataFrame) -> pd.DataFrame:
    """Pivot long observations to (period, sector_code) x metric for fast checks."""
    return observations.pivot_table(
        index=["period", "sector_code"], columns="metric", values="value", aggfunc="first"
    )


def validate_bddk(observations: pd.DataFrame) -> list:
    report = []
    wide = _wide(observations).reset_index()

    # 1) Row identity: total cash = current cash + follow-up, every sector, every month.
    gap = (wide["bddk_total_cash"] - wide["bddk_cash_current"] - wide["bddk_follow_up"]).abs()
    bad = int((gap > TOLERANCE_BDDK).sum())
    report.append(("BDDK", "total_cash = cash + follow_up (all rows)", bad == 0, f"{bad} violations"))

    # 2) TOPLAM row equals the sum of top-level sectors for every metric and month.
    metrics = [c for c in wide.columns if c.startswith("bddk_")]
    top_codes = [f"{c:02d}" for c in BDDK_TOP_LEVEL]
    total = wide[wide.sector_code == f"{BDDK_TOTAL_CODE:02d}"].set_index("period")[metrics]
    top_sum = wide[wide.sector_code.isin(top_codes)].groupby("period")[metrics].sum()
    hard, noted, worst = _identity_violations(total, top_sum, TOLERANCE_BDDK)
    report.append(("BDDK", "TOPLAM = sum of top-level sectors", hard == 0,
                   f"{hard} violations, {noted} noted discrepancies (worst {worst:.4f}%)"))

    # 3) Every parent equals the sum of its additive children.
    for parent, children in BDDK_CHILDREN.items():
        parent_vals = wide[wide.sector_code == f"{parent:02d}"].set_index("period")[metrics]
        child_vals = (
            wide[wide.sector_code.isin([f"{c:02d}" for c in children])]
            .groupby("period")[metrics]
            .sum()
        )
        hard, noted, worst = _identity_violations(parent_vals, child_vals, TOLERANCE_BDDK)
        report.append(("BDDK", f"sector {parent:02d} = sum({children})", hard == 0,
                       f"{hard} violations, {noted} noted discrepancies (worst {worst:.4f}%)"))

    # 4) Detail rows are subsets, NOT additive: adding 46 to 44's children must break the sum.
    for detail, of in BDDK_DETAIL_OF.items():
        detail_vals = wide[wide.sector_code == f"{detail:02d}"]["bddk_total_cash"]
        nonzero_months = int((detail_vals.abs() > TOLERANCE_BDDK).sum())
        report.append(
            ("BDDK", f"sector {detail:02d} is a non-additive detail of {of:02d}",
             nonzero_months > 0, f"nonzero in {nonzero_months} months (kept out of parent sums)")
        )
    return report


def validate_tbb(observations: pd.DataFrame) -> list:
    report = []
    wide = _wide(observations).reset_index()

    # 1) Row identity: gross = cash + liquidation, every sector, every month.
    gap = (wide["tbb_gross"] - wide["tbb_cash"] - wide["tbb_liquidation"]).abs()
    bad = int((gap > TOLERANCE_TBB).sum())
    report.append(("TBB_RM", "gross = cash + liquidation (all rows)", bad == 0, f"{bad} violations"))

    # 2) Toplam equals the sum of the 31 main sectors.
    dims = observations[["sector_code", "parent_code"]].drop_duplicates()
    main_codes = set(dims[dims.parent_code.isna()].sector_code) - {"TOTAL"}
    metrics = [c for c in wide.columns if c.startswith("tbb_")]
    total = wide[wide.sector_code == "TOTAL"].set_index("period")[metrics]
    main_sum = wide[wide.sector_code.isin(main_codes)].groupby("period")[metrics].sum()
    gap = (total - main_sum).abs()
    bad = int((gap > TOLERANCE_TBB).sum().sum())
    report.append(("TBB_RM", "Toplam = sum of 31 main sectors", bad == 0, f"{bad} violations"))

    # 3) Where a main sector has sub-sectors, they sum to the parent.
    with_children = dims[dims.parent_code.notna()].groupby("parent_code")["sector_code"].apply(list)
    for parent, children in with_children.items():
        parent_vals = wide[wide.sector_code == parent].set_index("period")[metrics]
        child_vals = wide[wide.sector_code.isin(children)].groupby("period")[metrics].sum()
        gap = (parent_vals - child_vals).abs()
        bad = int((gap > TOLERANCE_TBB).sum().sum())
        report.append(("TBB_RM", f"{parent} = sum of {len(children)} sub-sectors", bad == 0, f"{bad} violations"))
    return report


def run_all_validations(bddk_obs: pd.DataFrame, tbb_obs: pd.DataFrame) -> pd.DataFrame:
    rows = validate_bddk(bddk_obs) + validate_tbb(tbb_obs)
    report = pd.DataFrame(rows, columns=["source", "check", "passed", "detail"])
    failed = report[~report.passed]
    if not failed.empty:
        raise ValidationError(
            "Data integrity checks FAILED:\n" + failed.to_string(index=False)
        )
    return report
