"""End-to-end build: parse raw Excel -> validate -> processed + analytics tables.

Run:  python -m backend.lakehouse.build
Output: data/processed/*.parquet, data/analytics/*.parquet, data/lakehouse.duckdb
"""
import sys

import duckdb
import pandas as pd

from ..domain import canonical
from ..transform import analytics, macro
from ..transform.bulletin import build_bulletin_metrics, check_decumulation, decumulate
from ..validation import identities as validate
from ..core.config import (
    ANALYTICS_DIR,
    DUCKDB_PATH,
    EVDS_CATALOG_DIR,
    PROCESSED_DIR,
    RAW_BDDK_DIR,
    RAW_BDDK_JSON_DIR,
    RAW_BDDK_WEEKLY_DIR,
    RAW_EVDS_JSON_DIR,
    RAW_TBB_DIR,
    SCHEMA_CARD_PATH,
    UNIT,
)
from ..domain.bulletin_tables import TABLES as BULLETIN_TABLES
from ..parsing.bddk_sectoral import parse_bddk_directory
from ..parsing.bddk_bulletin import parse_bulletin_footnotes, parse_bulletin_table
from ..parsing.bddk_weekly import parse_weekly_archive
from ..parsing.evds import load_catalogue, parse_archive
from ..parsing.tbb import parse_tbb_directory
from ..validation.continuity import run_bulletin_validations
from ..validation.macro import check_macro_coverage, check_unit_resolution
from ..validation.weekly import check_weekly_against_monthly, run_weekly_validations
from .schema_card import write_schema_card


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
    # One vocabulary across the three series indexes (`metrics`,
    # `bulletin_metrics`, `macro_series`): an agent that learns 'stock' from one
    # must not miss these rows because they say 'period_end_stock' instead.
    metrics["temporal_semantics"] = "stock"

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


def build_bulletin_tables():
    """Parse, validate and de-cumulate all 17 monthly-bulletin tables.

    Runs on every build so that a newly downloaded month cannot introduce an
    unregistered row change without failing loudly -- the whole reason the
    continuity check exists. Validation is two checks now: entity continuity,
    and each row's own label-stated identity ('Tüketici Kredileri (2+3+4)'),
    which makes 60 arithmetic rules machine-derived rather than hand-curated.

    Returns (observations, lifecycle_report, metric_catalog).
    """
    frames = []
    reports = []
    for table in BULLETIN_TABLES:
        observations = parse_bulletin_table(RAW_BDDK_JSON_DIR, table.slug)
        reports.append(run_bulletin_validations(observations, table.slug))
        frames.append(observations)

    combined = decumulate(pd.concat(frames, ignore_index=True))
    report = pd.concat([r for r in reports if len(r)], ignore_index=True) if any(
        len(r) for r in reports
    ) else pd.DataFrame(columns=["dataset", "check", "entity_key", "passed", "detail"])
    return combined, report, build_bulletin_metrics(combined)


def build_weekly_tables():
    """Parse and validate the nine weekly-bulletin tables.

    The weekly bulletin is the timely half of the BDDK corpus: it observes on
    Fridays and publishes days later, where the monthly bulletin observes at
    month end and publishes weeks later. It is validated the same way -- every
    label's own arithmetic, every item's published lifecycle -- plus one check
    the monthly path has no use for: a retired item and the item that replaced
    it must agree wherever both publish, which is what licenses reading them as
    one series.

    Returns (observations, items, report).
    """
    observations, items = parse_weekly_archive(RAW_BDDK_WEEKLY_DIR)
    reports = [run_weekly_validations(group, slug)
               for slug, group in observations.groupby("dataset")]
    report = pd.concat([r for r in reports if len(r)], ignore_index=True) if any(
        len(r) for r in reports
    ) else pd.DataFrame(columns=["dataset", "check", "entity_key", "passed", "detail"])
    return observations, items, report


def build_macro_tables():
    """Parse the EVDS archive, align it to months, derive, and check coverage.

    Returns (catalogue, native, monthly, coverage_report). The catalogue is
    the agent's series index for this source: every row carries the declared
    temporal semantics and monthly rule, so nothing reaches the agent unlabelled.
    """
    catalogue = load_catalogue(EVDS_CATALOG_DIR)
    native = parse_archive(RAW_EVDS_JSON_DIR)
    monthly = macro.align_monthly(native, catalogue)
    monthly = macro.add_derived_series(monthly)
    catalogue = macro.expand_derived_catalogue(catalogue, monthly)
    coverage = pd.concat(
        [check_unit_resolution(catalogue), check_macro_coverage(monthly, catalogue)],
        ignore_index=True,
    )
    return catalogue, native, monthly, coverage


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

    print("Parsing BDDK bulletin tables...")
    bulletin_obs, bulletin_report, bulletin_metrics = build_bulletin_tables()
    bulletin_footnotes = parse_bulletin_footnotes(RAW_BDDK_JSON_DIR)
    decumulation = pd.DataFrame(check_decumulation(bulletin_obs))
    print(f"  {len(bulletin_obs):,} observations across {bulletin_obs.dataset.nunique()} tables, "
          f"{len(bulletin_metrics)} metrics, {len(bulletin_footnotes)} published methodology note(s), "
          f"{len(bulletin_report)} registered lifecycle/identity note(s)")

    print("Parsing BDDK weekly bulletin tables...")
    weekly_obs, weekly_items, weekly_report = build_weekly_tables()
    weekly_vs_monthly = check_weekly_against_monthly(weekly_obs, bulletin_obs)
    print(f"  {len(weekly_obs):,} observations across {weekly_obs.dataset.nunique()} tables, "
          f"{len(weekly_items)} items ({int(weekly_items.retired_on.notna().sum())} retired, "
          f"{int(weekly_items.is_informational.sum())} informational), "
          f"{weekly_obs.period.nunique()} weeks, {len(weekly_report)} lifecycle/identity note(s), "
          f"{len(weekly_vs_monthly)} cross-checks against the monthly bulletin passed")

    print("Parsing TCMB EVDS archive...")
    macro_series, macro_native, macro_monthly, macro_coverage = build_macro_tables()
    print(f"  {len(macro_series):,} series, {len(macro_native):,} native observations, "
          f"{len(macro_monthly):,} monthly rows, {len(macro_coverage)} coverage checks passed")

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
        "data_quality_report": pd.concat(
            [quality_report, decumulation, weekly_vs_monthly, macro_coverage],
            ignore_index=True),
        "bulletin_observations": bulletin_obs,
        "bulletin_metrics": bulletin_metrics,
        "bulletin_footnotes": bulletin_footnotes,
        "bulletin_lifecycle_report": bulletin_report,
        "weekly_observations": weekly_obs,
        "weekly_items": weekly_items,
        "weekly_lifecycle_report": weekly_report,
        "macro_series": macro_series,
        "macro_observations": macro_monthly,
        "macro_observations_native": macro_native,
    }

    print("Writing Parquet and DuckDB...")
    processed_names = {"observations", "sectors", "metrics", "sector_crosswalk",
                       "bulletin_observations", "bulletin_metrics", "bulletin_footnotes",
                       "weekly_observations", "weekly_items",
                       "macro_series", "macro_observations",
                       "macro_observations_native"}
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
