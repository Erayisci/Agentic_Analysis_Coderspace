"""Parser for TBB Risk Merkezi 'Bankalarca Kullandirilan Kredilerin Sektorel Dagilimi' files.

Layout (verified identical across 2022-01..2026-06):
    row 1: report title, row 2: column header
    data rows: 31 main sectors + 25 indented sub-sectors
        column B: sector number  -> WARNING: this is a SIZE RANK re-sorted every
                  month, NOT a stable identifier. Rows are keyed by sector NAME,
                  which is byte-identical across all files.
        column C: sector name; sub-sectors are marked by leading spaces and an
                  empty number cell, and always belong to the preceding main sector.
        columns D/F/H: gross, cash, liquidation credit stocks (bin TL, floats)
        columns E/G/I: percentage shares (dropped - recomputable)
    'Toplam' row: grand total; footnote rows follow (methodology text).

All figures are period-end outstanding balances (stocks), not flows.
Identity in every file: gross = cash + liquidation.
"""
import re
import unicodedata
import warnings
from pathlib import Path

import openpyxl
import pandas as pd

TBB_METRIC_COLUMNS = ["tbb_gross", "tbb_cash", "tbb_liquidation"]

FILENAME_PATTERN = re.compile(r"(\d{4})_(\d{2})\.xlsx$")

TURKISH_TO_ASCII = str.maketrans("çğıöşüÇĞİÖŞÜâî", "cgiosuCGIOSUai")


def slugify_sector_name(name: str) -> str:
    """Stable ASCII key for a sector name, e.g. 'Otel ve Restoranlar (Turizm)' -> 'otel_ve_restoranlar_turizm'."""
    text = name.strip().translate(TURKISH_TO_ASCII).lower()
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def parse_tbb_file(path: Path) -> pd.DataFrame:
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
    current_main_slug = None
    main_count = 0
    sub_count = 0
    total_seen = False
    footnotes = []

    for row in worksheet.iter_rows(values_only=True):
        number_cell, name_cell = row[1], row[2]
        number_text = str(number_cell).strip() if number_cell is not None else ""
        values = (row[3], row[5], row[7])

        if number_text == "Toplam":
            total_seen = True
            for metric, value in zip(TBB_METRIC_COLUMNS, values):
                records.append((period, "TOTAL", "Toplam", None, metric, float(value)))
            continue

        # Footnote / title rows carry long text in column B and no numeric data.
        if number_text and not number_text.isdigit() and values[0] is None:
            if number_text.startswith("("):
                footnotes.append(number_text)
            continue

        if name_cell is None or not isinstance(values[0], (int, float)):
            continue
        raw_name = str(name_cell)
        sector_name = raw_name.strip()
        is_sub_sector = raw_name.startswith(" ") and not number_text

        slug = slugify_sector_name(sector_name)
        if is_sub_sector:
            if current_main_slug is None:
                raise ValueError(f"{path.name}: sub-sector before any main sector: {sector_name}")
            parent_slug = current_main_slug
            sub_count += 1
        else:
            parent_slug = None
            current_main_slug = slug
            main_count += 1

        for metric, value in zip(TBB_METRIC_COLUMNS, values):
            records.append((period, slug, sector_name, parent_slug, metric, float(value)))

    workbook.close()

    if main_count != 31 or sub_count != 25 or not total_seen:
        raise ValueError(
            f"{path.name}: unexpected structure "
            f"(main={main_count}, sub={sub_count}, total_row={total_seen})"
        )

    frame = pd.DataFrame.from_records(
        records,
        columns=["period", "sector_code", "sector_name", "parent_code", "metric", "value"],
    )
    frame.insert(1, "source", "TBB_RM")
    frame.attrs["footnotes"] = footnotes
    return frame


def parse_tbb_directory(directory: Path) -> pd.DataFrame:
    files = sorted(p for p in directory.glob("TBB_*.xlsx") if not p.name.startswith("~$"))
    if not files:
        raise FileNotFoundError(f"No TBB files found in {directory}")
    frames = [parse_tbb_file(p) for p in files]
    footnotes = frames[-1].attrs["footnotes"]
    frame = pd.concat(frames, ignore_index=True)
    frame["period"] = pd.to_datetime(frame["period"]).dt.date
    frame.attrs["footnotes"] = footnotes
    return frame
