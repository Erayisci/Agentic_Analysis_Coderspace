"""Parser for BDDK 'Sektorel Kredi Dagilimi' monthly Excel files.

Every file has an identical layout (verified across 2022-01..2026-07):
    row 1: header
    rows 2..71: one row per sector, columns:
        A: literal 'Sektör'
        B: sector code 1..70 (70 = TOPLAM, the grand total)
        C: sector name (may embed the aggregation formula, e.g. 'İmalat Sanayi (10+...+22+25)')
        D..I: six credit-stock metrics, plain integers in bin TL

All figures are period-end outstanding balances (stocks), not flows.
"""
import re
import warnings
from pathlib import Path

import openpyxl
import pandas as pd

BDDK_METRIC_COLUMNS = [
    "bddk_short_term_cash",
    "bddk_medium_long_term_cash",
    "bddk_cash_current",
    "bddk_follow_up",
    "bddk_total_cash",
    "bddk_noncash",
]

FILENAME_PATTERN = re.compile(r"(\d{4})_(\d{2})\.xlsx$")


def parse_bddk_file(path: Path) -> pd.DataFrame:
    """Parse one monthly file into long format: one row per (sector, metric)."""
    match = FILENAME_PATTERN.search(path.name)
    if not match:
        raise ValueError(f"Cannot extract period from filename: {path.name}")
    period = f"{match.group(1)}-{match.group(2)}-01"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        workbook = openpyxl.load_workbook(path, read_only=True)
    worksheet = workbook.worksheets[0]

    records = []
    for row in worksheet.iter_rows(min_row=2, values_only=True):
        code_cell, name_cell = row[1], row[2]
        if code_cell is None or name_cell is None:
            continue
        code_text = str(code_cell).strip()
        if not code_text.isdigit():
            continue
        sector_code = int(code_text)
        sector_name = str(name_cell).strip()
        values = row[3:9]
        if any(v is None for v in values):
            raise ValueError(f"{path.name}: missing value in sector row {sector_code}")
        for metric, value in zip(BDDK_METRIC_COLUMNS, values):
            records.append(
                {
                    "period": period,
                    "source": "BDDK",
                    "sector_code": f"{sector_code:02d}",
                    "sector_name": sector_name,
                    "metric": metric,
                    "value": float(value),
                }
            )
    workbook.close()

    frame = pd.DataFrame.from_records(records)
    expected_rows = 70 * len(BDDK_METRIC_COLUMNS)
    if len(frame) != expected_rows:
        raise ValueError(f"{path.name}: expected {expected_rows} observations, got {len(frame)}")
    return frame


def parse_bddk_directory(directory: Path) -> pd.DataFrame:
    files = sorted(p for p in directory.glob("BDDK_*.xlsx") if not p.name.startswith("~$"))
    if not files:
        raise FileNotFoundError(f"No BDDK files found in {directory}")
    frame = pd.concat([parse_bddk_file(p) for p in files], ignore_index=True)
    frame["period"] = pd.to_datetime(frame["period"]).dt.date
    return frame
