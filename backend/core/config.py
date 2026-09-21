"""Central paths and constants.

Repository layout the paths below refer to:
    bddk_aylik_bulten/<NN>_<slug>/  raw BDDK monthly Excel, one dir per bulletin
                                    table (never modified)
    bddk_aylik_bulten/_raw_json/    archived endpoint responses -- the source of
                                    truth for the bulletin parser
    bddk_finturk/_raw_json/ archived FinTurk (il-bazli, quarterly) responses -- the
                            source of truth for the province-grained corpus
    riskmerkezi_sectoral/  raw TBB Risk Merkezi monthly Excel files (never modified)
    evds/_raw_json/        archived TCMB EVDS responses -- the source of truth for
                           the macro series (catalogue + data, never modified)
    data/processed/        normalized long-format tables parsed from the raw files
    data/analytics/        derived tables (growth rates, ratios, reconciliation)
    data/lakehouse.duckdb  DuckDB database containing every table above
"""
import os
from pathlib import Path

# This module sits at backend/core/config.py, so the repository root is three
# levels up. Raw data and outputs live beside the package, not inside it.
ROOT = Path(__file__).resolve().parents[2]

RAW_BDDK_ROOT = ROOT / "bddk_aylik_bulten"
RAW_BDDK_DIR = RAW_BDDK_ROOT / "05_sektorel_kredi_dagilimi"
RAW_BDDK_JSON_DIR = RAW_BDDK_ROOT / "_raw_json"
RAW_TBB_DIR = ROOT / "riskmerkezi_sectoral"

# The weekly bulletin answers a DATE RANGE per request, so its archive is one
# envelope per table covering the whole history, not one file per period.
RAW_BDDK_WEEKLY_ROOT = ROOT / "bddk_haftalik_bulten"
RAW_BDDK_WEEKLY_DIR = RAW_BDDK_WEEKLY_ROOT / "_raw"
RAW_BDDK_WEEKLY_CATALOG = RAW_BDDK_WEEKLY_DIR / "_catalog"

# The corpus window, matching the monthly archive's edge. The weekly bulletin
# reaches further back (2014) but the brief asks for 2021 onwards and the
# cross-validation against the monthly tables has nothing to compare before it.
WEEKLY_FETCH_START = "1.01.2021"

# FinTurk (Cografi Dagilim / il-bazli) is a separate BDDK product: 7 tables,
# quarterly (not monthly), broken down by province rather than by sector or
# balance-sheet line. It reaches back to 2007-12; the brief's window (2021-01
# onwards) is what the rest of the lakehouse is pinned to, so that is the
# default fetch range here too.
RAW_BDDK_FINTURK_ROOT = ROOT / "bddk_finturk"
RAW_BDDK_FINTURK_JSON_DIR = RAW_BDDK_FINTURK_ROOT / "_raw_json"
FINTURK_FETCH_START = "2021-3"
FINTURK_FETCH_END = "2026-6"

RAW_EVDS_ROOT = ROOT / "evds"
RAW_EVDS_JSON_DIR = RAW_EVDS_ROOT / "_raw_json"
EVDS_CATALOG_DIR = RAW_EVDS_JSON_DIR / "_catalog"
EVDS_SERIELIST_DIR = EVDS_CATALOG_DIR / "serielist"

# The corpus window the brief asks for is 2021-01..2026-06; the BDDK archive
# already runs one month further, so EVDS is pulled to the same edge.
EVDS_FETCH_START = "2021-01-01"
EVDS_FETCH_END = "2026-07-31"

DATA_DIR = ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
ANALYTICS_DIR = DATA_DIR / "analytics"
DUCKDB_PATH = DATA_DIR / "lakehouse.duckdb"
SCHEMA_CARD_PATH = ANALYTICS_DIR / "schema_card.md"

# Spelled exactly as `bulletin_observations.unit` spells it. The sectoral path
# and the generic bulletin path read the same BDDK table, so a query filtering
# on `unit = 'bin TL'` must match rows from both -- an underscored variant here
# silently excluded `observations` from every such filter.
UNIT = "bin TL"

# Absolute tolerances for arithmetic identity checks (values are in bin TL).
TOLERANCE_BDDK = 2.0   # source values are integers
TOLERANCE_TBB = 1.0    # source values are floats with 3 decimals

# Reconciliation sanity bounds for the national troubled-bucket divergence
# (TBB liquidation vs BDDK follow-up), applied on top of the statistical
# mean +/- 3*sigma band computed from history.
TROUBLE_DIVERGENCE_HARD_LOWER_PCT = 5.0
TROUBLE_DIVERGENCE_HARD_UPPER_PCT = 30.0


# --- Kloudeks / MIA -------------------------------------------------------
# The hackathon brief allows open-weight models only, reached through the
# Kloudeks platform. One OpenAI-compatible endpoint serves all three; nothing
# outside backend/llm may import an LLM SDK or name a model id.
KLOUDEKS_BASE_URL = "https://mia.csp.kloudeks.com/v1"
KLOUDEKS_CHAT_MODEL = "kkbhackathon2026/Qwen3.8-27B"
KLOUDEKS_EMBEDDING_MODEL = "kkbhackathon2026/Qwen3-Embedding-8B"
KLOUDEKS_OCR_MODEL = "kkbhackathon2026/Unlimited-OCR"

# Qwen3 is a reasoning model and thinks before answering. Measured against this
# endpoint: a trivial prompt costs 63 completion tokens with thinking and 2
# without. Planning is a schema-filling task where the server's guided decoding
# already guarantees the shape, so thinking buys nothing and costs latency;
# narrative composition is where it earns its keep. Per-call override exists.
KLOUDEKS_TIMEOUT_SECONDS = 120.0


def _read_dotenv(name: str) -> str:
    """One key from the gitignored repo-root .env, or ''."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip("'\"")
    return ""


def kloudeks_api_key() -> str:
    """The MIA/Kloudeks API key, from the environment or the gitignored `.env`.

    Never logged and never written into code: the key is a credential, and the
    platform guide is explicit that it must not reach a repository.
    """
    key = os.environ.get("KLOUDEKS_API_KEY", "").strip() or _read_dotenv("KLOUDEKS_API_KEY")
    if not key:
        raise RuntimeError(
            "KLOUDEKS_API_KEY is not set. Export it or put `KLOUDEKS_API_KEY=...` in the\n"
            "repo-root .env (gitignored)."
        )
    return key


def evds_api_key() -> str:
    """The EVDS web-service key, from the environment or the gitignored `.env`.

    Only the ingestion CLI needs it: the parser and the build read the
    committed archive, so a clone builds without a key.
    """
    key = os.environ.get("EVDS_API_KEY", "").strip()
    if not key:
        env_file = ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                name, _, value = line.partition("=")
                if name.strip() == "EVDS_API_KEY":
                    key = value.strip().strip("'\"")
                    break
    if not key:
        raise RuntimeError(
            "EVDS_API_KEY is not set. Export it or put `EVDS_API_KEY=...` in the repo-root\n"
            ".env (gitignored). Keys are issued at https://evds3.tcmb.gov.tr -> Profilim."
        )
    return key
