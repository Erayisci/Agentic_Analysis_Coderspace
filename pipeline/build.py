"""End-to-end build: parse raw Excel -> validate -> processed + analytics tables.

Run:  python -m pipeline.build
Output: data/processed/*.parquet, data/analytics/*.parquet, data/lakehouse.duckdb
"""
import sys

import duckdb
import pandas as pd

from . import analytics, canonical, validate
from .config import (
    ANALYTICS_DIR,
    DUCKDB_PATH,
    PROCESSED_DIR,
    RAW_BDDK_DIR,
    RAW_TBB_DIR,
    SCHEMA_CARD_PATH,
    UNIT,
)
from .parse_bddk import parse_bddk_directory
from .parse_tbb import parse_tbb_directory


def build_dimension_tables(bddk_obs: pd.DataFrame, tbb_obs: pd.DataFrame):
    """Sector dimensions (with hierarchy), metric catalog and the crosswalk."""
    bddk_names = (
        bddk_obs.drop_duplicates("sector_code").set_index("sector_code")["sector_name"].to_dict()
    )
    bddk_sectors = pd.DataFrame(canonical.build_bddk_sector_table(bddk_names))
    bddk_sectors.insert(0, "source", "BDDK")

    tbb_dims = tbb_obs[tbb_obs.sector_code != "TOTAL"][
        ["sector_code", "sector_name", "parent_code"]
    ].drop_duplicates("sector_code")
    tbb_sectors = pd.DataFrame({
        "source": "TBB_RM",
        "sector_code": tbb_dims.sector_code,
        "sector_name": tbb_dims.sector_name,
        "parent_code": tbb_dims.parent_code,
        "relation": tbb_dims.parent_code.map(lambda p: "child" if pd.notna(p) else "top_level"),
        "is_leaf": ~tbb_dims.sector_code.isin(tbb_dims.parent_code.dropna()),
    })
    sectors = pd.concat([bddk_sectors, tbb_sectors], ignore_index=True)

    metrics = pd.DataFrame(
        canonical.METRIC_CATALOG,
        columns=["metric", "source", "name_turkish", "name_english"],
    )
    metrics["unit"] = UNIT
    metrics["temporal_semantics"] = "period_end_stock"

    crosswalk_rows = []
    for canonical_id, name_en, bddk_codes, tbb_slugs, relation, confidence in canonical.CANONICAL_SECTORS:
        for code in bddk_codes:
            crosswalk_rows.append((canonical_id, name_en, "BDDK", f"{code:02d}",
                                   "component" if len(bddk_codes) > 1 else relation, confidence))
        for slug in tbb_slugs:
            crosswalk_rows.append((canonical_id, name_en, "TBB_RM", slug,
                                   "component" if len(tbb_slugs) > 1 else relation, confidence))
    for bddk_code, tbb_slug in canonical.MANUFACTURING_PAIRS.items():
        pair_id = f"mfg_{tbb_slug[:24]}"
        crosswalk_rows.append((pair_id, f"Manufacturing pair: {tbb_slug}", "BDDK", f"{bddk_code:02d}", "exact", "high"))
        crosswalk_rows.append((pair_id, f"Manufacturing pair: {tbb_slug}", "TBB_RM", tbb_slug, "exact", "high"))
    for bddk_code, tbb_slug in canonical.PERSONAL_CREDIT_PAIRS.items():
        pair_id = f"personal_{tbb_slug}"
        crosswalk_rows.append((pair_id, f"Personal credit pair: {tbb_slug}", "BDDK", f"{bddk_code:02d}", "exact", "high"))
        crosswalk_rows.append((pair_id, f"Personal credit pair: {tbb_slug}", "TBB_RM", tbb_slug, "exact", "high"))
    crosswalk = pd.DataFrame(
        crosswalk_rows,
        columns=["canonical_sector", "canonical_name", "source", "source_sector_code",
                 "relation_type", "mapping_confidence"],
    )
    return sectors, metrics, crosswalk


def write_schema_card(tables: dict, tbb_footnotes: list) -> None:
    """Compact, token-efficient description of the lakehouse for the agent's context."""
    lines = [
        "# Lakehouse schema card",
        "",
        "All monetary values: bin TL (thousands of Turkish lira), period-end outstanding",
        "balances (stocks). A month-over-month change is a NET balance change (new lending",
        "minus repayments, plus FX revaluation / write-offs), never 'new lending'.",
        "BDDK and TBB_RM are methodologically different sources: compare, never merge.",
        "BDDK 'follow-up' and TBB 'liquidation' are DIFFERENT concepts (persistent ~19-21% gap,",
        "documented scope differences; see reconciliation_monitor before explaining it).",
        "",
        "## Tables",
    ]
    for name, frame in tables.items():
        periods = ""
        if "period" in frame.columns:
            periods = f", periods {frame.period.min()}..{frame.period.max()}"
        lines.append(f"- **{name}** ({len(frame):,} rows{periods}): {', '.join(frame.columns)}")
    lines += [
        "",
        "## Query rules",
        "- Sector hierarchy: BDDK parents already contain their children; sum only rows",
        "  with relation='top_level' for national aggregates, or use the TOPLAM/TOTAL row.",
        "  BDDK sector 46 is a non-additive detail of 45. TBB sub-sectors sit under parent_code.",
        "- Cross-source questions: read reconciliation_monitor (bddk_value, tbb_value,",
        "  divergence_pct, out_of_band). If out_of_band=True, report a divergence regime",
        "  change instead of the standard methodology explanation.",
        "",
        "## TBB methodology footnotes (primary source, June 2026 report)",
    ]
    lines += [f"> {note}" for note in tbb_footnotes]
    SCHEMA_CARD_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    for directory in (PROCESSED_DIR, ANALYTICS_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    print("Parsing BDDK files...")
    bddk_obs = parse_bddk_directory(RAW_BDDK_DIR)
    print(f"  {len(bddk_obs):,} observations, {bddk_obs.period.nunique()} months")

    print("Parsing TBB Risk Merkezi files...")
    tbb_obs = parse_tbb_directory(RAW_TBB_DIR)
    tbb_footnotes = tbb_obs.attrs["footnotes"]
    print(f"  {len(tbb_obs):,} observations, {tbb_obs.period.nunique()} months")

    print("Checking curated crosswalk against parsed sector names...")
    dims = tbb_obs[tbb_obs.sector_code != "TOTAL"][["sector_code", "parent_code"]].drop_duplicates("sector_code")
    canonical.validate_crosswalk_against_data(
        set(dims[dims.parent_code.isna()].sector_code),
        set(dims[dims.parent_code.notna()].sector_code),
    )

    print("Running integrity validations...")
    quality_report = validate.run_all_validations(bddk_obs, tbb_obs)
    print(f"  {len(quality_report)} checks passed")

    print("Building dimension tables and crosswalk...")
    sectors, metrics, crosswalk = build_dimension_tables(bddk_obs, tbb_obs)

    print("Building analytics tables...")
    combined = pd.concat(
        [bddk_obs.assign(parent_code=None) if "parent_code" not in bddk_obs else bddk_obs, tbb_obs],
        ignore_index=True,
    )[["period", "source", "sector_code", "sector_name", "metric", "value"]]
    growth = analytics.build_growth_table(combined)
    ratios = analytics.build_ratio_table(bddk_obs, tbb_obs)
    reconciliation = analytics.build_reconciliation_monitor(bddk_obs, tbb_obs)
    out_of_band = reconciliation[reconciliation.out_of_band]
    print(f"  reconciliation monitor: {len(reconciliation):,} rows, {len(out_of_band)} out-of-band")

    tables = {
        "observations": combined.assign(unit=UNIT),
        "sectors": sectors,
        "metrics": metrics,
        "sector_crosswalk": crosswalk,
        "growth": growth,
        "ratios": ratios,
        "reconciliation_monitor": reconciliation,
        "data_quality_report": quality_report,
    }

    print("Writing Parquet and DuckDB...")
    processed_names = {"observations", "sectors", "metrics", "sector_crosswalk"}
    connection = duckdb.connect(str(DUCKDB_PATH))
    for name, frame in tables.items():
        target_dir = PROCESSED_DIR if name in processed_names else ANALYTICS_DIR
        parquet_path = target_dir / f"{name}.parquet"
        frame.to_parquet(parquet_path, index=False)
        connection.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM read_parquet('{parquet_path.as_posix()}')")
    connection.close()

    write_schema_card(tables, tbb_footnotes)
    print(f"Done. DuckDB at {DUCKDB_PATH}, schema card at {SCHEMA_CARD_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
