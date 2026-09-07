"""Central paths and constants for the data pipeline.

Layout:
    bddk_excel_verileri/   raw BDDK monthly Excel files (never modified)
    riskmerkezi_sectoral/  raw TBB Risk Merkezi monthly Excel files (never modified)
    data/processed/        normalized long-format tables parsed from the raw files
    data/analytics/        derived tables (growth rates, ratios, reconciliation)
    data/lakehouse.duckdb  DuckDB database containing every table above
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RAW_BDDK_DIR = ROOT / "bddk_excel_verileri"
RAW_TBB_DIR = ROOT / "riskmerkezi_sectoral"

DATA_DIR = ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
ANALYTICS_DIR = DATA_DIR / "analytics"
DUCKDB_PATH = DATA_DIR / "lakehouse.duckdb"
SCHEMA_CARD_PATH = ANALYTICS_DIR / "schema_card.md"

UNIT = "bin_TL"

# Absolute tolerances for arithmetic identity checks (values are in bin TL).
TOLERANCE_BDDK = 2.0   # source values are integers
TOLERANCE_TBB = 1.0    # source values are floats with 3 decimals

# Reconciliation sanity bounds for the national troubled-bucket divergence
# (TBB liquidation vs BDDK follow-up), applied on top of the statistical
# mean +/- 3*sigma band computed from history.
TROUBLE_DIVERGENCE_HARD_LOWER_PCT = 5.0
TROUBLE_DIVERGENCE_HARD_UPPER_PCT = 30.0
