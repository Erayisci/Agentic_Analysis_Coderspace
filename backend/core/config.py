"""Central paths and constants.

Repository layout the paths below refer to:
    bddk_aylik_bulten/<NN>_<slug>/  raw BDDK monthly Excel, one dir per bulletin
                                    table (never modified)
    bddk_aylik_bulten/_raw_json/    archived endpoint responses -- the source of
                                    truth for the bulletin parser
    riskmerkezi_sectoral/  raw TBB Risk Merkezi monthly Excel files (never modified)
    data/processed/        normalized long-format tables parsed from the raw files
    data/analytics/        derived tables (growth rates, ratios, reconciliation)
    data/lakehouse.duckdb  DuckDB database containing every table above
"""
from pathlib import Path

# This module sits at backend/core/config.py, so the repository root is three
# levels up. Raw data and outputs live beside the package, not inside it.
ROOT = Path(__file__).resolve().parents[2]

RAW_BDDK_ROOT = ROOT / "bddk_aylik_bulten"
RAW_BDDK_DIR = RAW_BDDK_ROOT / "05_sektorel_kredi_dagilimi"
RAW_BDDK_JSON_DIR = RAW_BDDK_ROOT / "_raw_json"
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
