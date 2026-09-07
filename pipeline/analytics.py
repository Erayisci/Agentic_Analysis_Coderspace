"""Derived analytics tables built on top of the validated observations.

Everything the agent should never compute in its own context window is
materialized here: growth rates, stress ratios, and the cross-source
reconciliation monitor with its statistical divergence band.
"""
import pandas as pd

from .canonical import (
    CANONICAL_SECTORS,
    MANUFACTURING_PAIRS,
    PERSONAL_CREDIT_PAIRS,
    RECONCILIATION_METRIC_PAIRS,
)
from .config import (
    TROUBLE_DIVERGENCE_HARD_LOWER_PCT,
    TROUBLE_DIVERGENCE_HARD_UPPER_PCT,
)


def build_growth_table(observations: pd.DataFrame) -> pd.DataFrame:
    """Month-over-month and year-over-year percentage change per series.

    Input: combined long observations of both sources. Stocks only, so these
    are net balance changes (including FX revaluation), not new-lending flows.
    """
    frame = observations.sort_values("period").copy()
    grouped = frame.groupby(["source", "sector_code", "metric"])["value"]
    frame["mom_pct"] = grouped.pct_change(1) * 100
    frame["yoy_pct"] = grouped.pct_change(12) * 100
    result = frame.dropna(subset=["mom_pct", "yoy_pct"], how="all")
    return result[["period", "source", "sector_code", "sector_name", "metric", "value", "mom_pct", "yoy_pct"]]


def build_ratio_table(bddk_obs: pd.DataFrame, tbb_obs: pd.DataFrame) -> pd.DataFrame:
    """Source-specific stress and structure ratios per (period, sector)."""
    frames = []

    bddk = bddk_obs.pivot_table(
        index=["period", "sector_code", "sector_name"], columns="metric", values="value"
    ).reset_index()
    bddk_ratios = pd.DataFrame({
        "period": bddk["period"],
        "source": "BDDK",
        "sector_code": bddk["sector_code"],
        "sector_name": bddk["sector_name"],
        # share of total cash loans that is non-performing
        "follow_up_ratio_pct": 100 * bddk["bddk_follow_up"] / bddk["bddk_total_cash"],
        # short-term share of current cash loans (maturity pressure)
        "short_term_share_pct": 100 * bddk["bddk_short_term_cash"] / bddk["bddk_cash_current"],
        # off-balance exposure relative to cash exposure
        "noncash_to_cash_pct": 100 * bddk["bddk_noncash"] / bddk["bddk_cash_current"],
    })
    frames.append(bddk_ratios)

    tbb = tbb_obs.pivot_table(
        index=["period", "sector_code", "sector_name"], columns="metric", values="value"
    ).reset_index()
    tbb_ratios = pd.DataFrame({
        "period": tbb["period"],
        "source": "TBB_RM",
        "sector_code": tbb["sector_code"],
        "sector_name": tbb["sector_name"],
        # share of gross loans awaiting liquidation (NOT the same concept as
        # the BDDK follow-up ratio; compare, never merge)
        "liquidation_ratio_pct": 100 * tbb["tbb_liquidation"] / tbb["tbb_gross"],
    })
    frames.append(tbb_ratios)

    return pd.concat(frames, ignore_index=True)


def _canonical_side_values(observations: pd.DataFrame, sector_codes: list, metric: str) -> pd.Series:
    """Sum a metric over a list of sector codes, per period."""
    subset = observations[
        observations.sector_code.isin(sector_codes) & (observations.metric == metric)
    ]
    return subset.groupby("period")["value"].sum()


def build_reconciliation_monitor(bddk_obs: pd.DataFrame, tbb_obs: pd.DataFrame) -> pd.DataFrame:
    """Cross-source divergence per canonical sector, month and metric pair.

    divergence_pct = (TBB - BDDK) / BDDK * 100.  A persistent positive gap is
    the DOCUMENTED, EXPECTED behavior (methodology scope differences listed in
    the TBB footnotes). The statistical band exists to detect the day that
    explanation stops holding: any month outside mean +/- 3*sigma (or outside
    the hard sanity bounds for the national trouble pair) sets out_of_band=True,
    and the agent must then report a divergence regime change instead of
    reciting the standard methodology explanation.
    """
    comparisons = []
    targets = [("TOTAL_NATIONAL", [f"{c:02d}" for c in (70,)], ["TOTAL"])]
    for canonical_id, _, bddk_codes, tbb_slugs, relation, confidence in CANONICAL_SECTORS:
        if not tbb_slugs:
            continue
        targets.append((canonical_id, [f"{c:02d}" for c in bddk_codes], tbb_slugs))
    for bddk_code, tbb_slug in MANUFACTURING_PAIRS.items():
        targets.append((f"mfg_{tbb_slug[:24]}", [f"{bddk_code:02d}"], [tbb_slug]))
    for bddk_code, tbb_slug in PERSONAL_CREDIT_PAIRS.items():
        targets.append((f"personal_{tbb_slug}", [f"{bddk_code:02d}"], [tbb_slug]))

    for canonical_id, bddk_codes, tbb_codes in targets:
        for pair_name, (bddk_metric, tbb_metric) in RECONCILIATION_METRIC_PAIRS.items():
            bddk_side = _canonical_side_values(bddk_obs, bddk_codes, bddk_metric)
            tbb_side = _canonical_side_values(tbb_obs, tbb_codes, tbb_metric)
            joined = pd.DataFrame({"bddk_value": bddk_side, "tbb_value": tbb_side}).dropna()
            if joined.empty:
                continue
            joined["divergence_pct"] = 100 * (joined.tbb_value - joined.bddk_value) / joined.bddk_value
            mean = joined.divergence_pct.mean()
            std = joined.divergence_pct.std()
            joined["band_lower_pct"] = mean - 3 * std
            joined["band_upper_pct"] = mean + 3 * std
            joined["out_of_band"] = ~joined.divergence_pct.between(
                joined.band_lower_pct, joined.band_upper_pct
            )
            if canonical_id == "TOTAL_NATIONAL" and pair_name == "trouble":
                joined["out_of_band"] |= ~joined.divergence_pct.between(
                    TROUBLE_DIVERGENCE_HARD_LOWER_PCT, TROUBLE_DIVERGENCE_HARD_UPPER_PCT
                )
            joined = joined.reset_index()
            joined.insert(1, "canonical_sector", canonical_id)
            joined.insert(2, "comparison", pair_name)
            joined.insert(3, "bddk_metric", bddk_metric)
            joined.insert(4, "tbb_metric", tbb_metric)
            comparisons.append(joined)

    return pd.concat(comparisons, ignore_index=True)
