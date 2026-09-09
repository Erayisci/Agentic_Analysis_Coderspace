#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BDDK Aylik Bulten Downloader
============================

Fetches any of the 17 monthly-bulletin tables straight from the BDDK JSON
endpoint and writes each month out as one Excel file under

    bddk_aylik_bulten/<NN>_<slug>/BDDK_<slug>_<YYYY>_<MM>.xlsx

Every bulletin table shares one shape -- a row label column plus measure
columns -- so a single generic extractor covers all 17. The Excel layout is:

    row 1      : ['', '', '<title> (Dönem:YYYY/M)', <measure labels...>]
    rows 2..N  : [<row-kind literal>, code, row label, <values...>]

which is exactly the layout `backend.parsing.bddk_sectoral` already expects for table 5,
so the sectoral corpus round-trips unchanged.

The raw JSON response is also cached under `bddk_aylik_bulten/_raw_json/`
(gitignored). It carries the endpoint's own column metadata (`colModels`),
which the Excel layout drops, so a later parser can recover column names and
types without re-hitting BDDK.

"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from openpyxl import Workbook

from ..core.config import RAW_BDDK_JSON_DIR, RAW_BDDK_ROOT
from ..domain.bulletin_tables import BY_NUMBER, TABLES, Table

ENDPOINT = "https://www.bddk.org.tr/BultenAylik/tr/Home/BasitRaporGetir"
REFERER = "https://www.bddk.org.tr/bultenaylik"

# taraf 10001 is the whole banking sector (Mevduat + Katılım + Kalkınma/Yatırım),
# matching the archived files. Other values break the corpus down by bank group.
TARAF = 10001
PARA_BIRIMI = "TL"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 45
RETRIES = 4
REQUEST_DELAY = 0.50

# Columns the endpoint returns for bookkeeping rather than measurement.
META_FIELDS = ("BankaAdi", "BasitSira", "Ad", "BasitFont")


# Table 5's header labels as they appear in the archived workbooks. The endpoint
# exposes field names (KisaVadeliNakdi), not these Turkish labels, so keeping
# them here is what makes regenerated files match the corpus already on disk.
SECTORAL_HEADERS = {
    "KisaVadeliNakdi": "Kısa Vadeli Nakdi Krediler",
    "OrtaUzunVadeliNakdi": "Orta ve Uzun Vadeli Nakdi Krediler",
    "Nakdi": "Nakdi Krediler",
    "Takipteki": "Takipteki Krediler",
    "ToplamNakdi": "Toplam Nakdi Krediler",
    "GayriNakdi": "Gayri Nakdi Krediler",
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _ssl_context() -> ssl.SSLContext:
    """
    www.bddk.org.tr serves an incomplete certificate chain, so the default
    verified context fails here while browsers succeed via AIA fetching.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def fetch_report(table: Table, year: int, month: int) -> dict:
    """POST one (table, year, month) to the bulletin endpoint, return parsed JSON."""
    payload = urlencode(
        {
            "tabloNo": table.number,
            "yil": year,
            "ay": month,
            "paraBirimi": PARA_BIRIMI,
            "taraf": TARAF,
        }
    ).encode("utf-8")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://www.bddk.org.tr",
        "Referer": REFERER,
        "X-Requested-With": "XMLHttpRequest",
    }

    context = _ssl_context()
    last_error: Optional[Exception] = None

    for attempt in range(1, RETRIES + 1):
        try:
            request = Request(ENDPOINT, data=payload, headers=headers, method="POST")
            with urlopen(request, timeout=REQUEST_TIMEOUT, context=context) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            if attempt < RETRIES:
                wait = min(2 ** (attempt - 1), 8)
                print(f"    request failed ({attempt}/{RETRIES}), retrying in {wait}s: {exc}")
                time.sleep(wait)

    raise RuntimeError(f"Failed to fetch table {table.number} for {year}-{month:02d}: {last_error}")


# ---------------------------------------------------------------------------
# Response -> rows
# ---------------------------------------------------------------------------

def _coerce(value):
    """Endpoint numbers arrive as strings; ratios are decimal, stocks integral."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return text


def extract_rows(response: dict, table: Table, year: int, month: int) -> Tuple[List[str], List[Tuple]]:
    """
    Turn the endpoint payload into (measure labels, row tuples).

    Fails loudly on structural surprises: a silently reshaped response would
    otherwise produce a workbook full of plausible but wrong numbers.
    """
    label = f"table {table.number} {year}-{month:02d}"

    if not response.get("success"):
        raise ValueError(f"{label}: endpoint reported success=false ({response.get('error')})")

    block = response.get("Json") or {}
    col_models = block.get("colModels")
    rows = (block.get("data") or {}).get("rows")
    if not col_models or not rows:
        raise ValueError(f"{label}: response carries no table data (month not published?)")

    names = [c["name"] for c in col_models]
    for required in ("BasitSira", "Ad"):
        if required not in names:
            raise ValueError(f"{label}: unexpected layout, {required!r} missing from {names}")

    code_index = names.index("BasitSira")
    name_index = names.index("Ad")
    measure_indexes = [i for i, name in enumerate(names) if name not in META_FIELDS]
    if not measure_indexes:
        raise ValueError(f"{label}: no measure columns in {names}")

    if table.strict and len(rows) != table.rows_seen:
        raise ValueError(f"{label}: expected {table.rows_seen} rows, got {len(rows)}")

    headers = [
        SECTORAL_HEADERS.get(names[i], col_models[i].get("label") or names[i])
        for i in measure_indexes
    ]

    extracted: List[Tuple] = []
    for position, row in enumerate(rows, start=1):
        cell = row["cell"]
        code = int(str(cell[code_index]).strip())
        if table.strict:
            if code != position:
                raise ValueError(f"{label}: codes out of order at row {position} (got {code})")
            if any(cell[i] in (None, "") for i in measure_indexes):
                raise ValueError(f"{label}: missing value in row {code}")

        values = [_coerce(cell[i]) for i in measure_indexes]
        extracted.append((table.row_kind, code, str(cell[name_index]).strip(), *values))

    return headers, extracted


def write_workbook(
    headers: List[str],
    rows: List[Tuple],
    table: Table,
    year: int,
    month: int,
    destination: Path,
) -> None:
    """Write the rows in the archived files' layout."""
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = f"{table.slug[:24]}_{year}_{month}"

    unit = f" ({table.unit})" if table.unit else ""
    worksheet.append(["", "", f"{table.title}{unit}, Dönem:{year}/{month}", *headers])
    for row in rows:
        worksheet.append(list(row))

    last_column = 3 + len(headers)
    for excel_row in worksheet.iter_rows(min_row=2, min_col=4, max_col=last_column):
        for cell in excel_row:
            cell.number_format = "#,##0"

    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(destination)


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------

def parse_months(value: str) -> List[int]:
    return _parse_int_ranges(value, 1, 12, "month")


def parse_tables(value: str) -> List[int]:
    return _parse_int_ranges(value, 1, 17, "table")


def _parse_int_ranges(value: str, low: int, high: int, kind: str) -> List[int]:
    picked: List[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(part)
        if not (low <= start <= end <= high):
            raise argparse.ArgumentTypeError(
                f"Invalid {kind} range {part!r}; expected {low}..{high}"
            )
        picked.extend(range(start, end + 1))
    if not picked:
        raise argparse.ArgumentTypeError(f"No {kind}s selected")
    return sorted(set(picked))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download BDDK monthly bulletin tables as Excel."
    )
    parser.add_argument("--year", type=int, help="Calendar year, e.g. 2021")
    parser.add_argument(
        "--months",
        type=parse_months,
        default=list(range(1, 13)),
        metavar="1-12",
        help="Months to fetch, e.g. '1-6' or '1,3,7' (default: all 12)",
    )
    parser.add_argument(
        "--tables",
        type=parse_tables,
        default=[t.number for t in TABLES],
        metavar="1-17",
        help="Bulletin tables to fetch, e.g. '4,5' or '1-5' (default: all 17)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output root. Default: 'bddk_aylik_bulten' next to this script.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Rewrite existing files.")
    parser.add_argument("--no-raw-json", action="store_true", help="Skip the raw JSON cache.")
    parser.add_argument("--list", action="store_true", help="List the tables and exit.")
    parser.add_argument(
        "--from-cache",
        action="store_true",
        help="Rebuild the Excel files from the archived JSON responses instead of "
             "fetching. Needs no network; --year and --months are ignored.",
    )
    return parser


def render_from_cache(output_root: Path, raw_root: Path, tables: List[Table]) -> int:
    """Regenerate every Excel file from the archived responses.

    The workbooks are a derived rendering of the JSON, so they are not kept in
    version control. A fresh clone runs this once to materialise the sectoral
    files that `backend.parsing.bddk_sectoral` reads.
    """
    written = 0
    for table in tables:
        directory = raw_root / f"{table.number:02d}"
        paths = sorted(directory.glob("*.json"))
        if not paths:
            print(f"  [SKIP] {table.number:02d} {table.title}: no archived responses")
            continue

        for path in paths:
            year, month = int(path.stem[:4]), int(path.stem[5:7])
            response = json.loads(path.read_text(encoding="utf-8"))
            headers, rows = extract_rows(response, table, year, month)
            destination = (
                output_root / f"{table.number:02d}_{table.slug}"
                / f"BDDK_{table.slug}_{year:04d}_{month:02d}.xlsx"
            )
            write_workbook(headers, rows, table, year, month, destination)
            written += 1
        print(f"  [ OK ] {table.number:02d} {table.title}: {len(paths)} file(s)")

    print(f"\n[DONE] {written} workbook(s) rendered from cache.")
    return 0


def main() -> int:
    args = build_parser().parse_args()

    if args.list:
        print(f"{'No':>3}  {'Slug':<28} Title")
        for table in TABLES:
            print(f"{table.number:>3}  {table.slug:<28} {table.title}")
        return 0

    if args.year is None and not args.from_cache:
        build_parser().error("--year is required (or use --list / --from-cache)")

    output_root = args.out.expanduser().resolve() if args.out is not None else RAW_BDDK_ROOT
    raw_root = output_root / "_raw_json" if args.out is not None else RAW_BDDK_JSON_DIR

    tables = [BY_NUMBER[n] for n in args.tables]

    if args.from_cache:
        print(f"\nRendering Excel from {raw_root}\n")
        return render_from_cache(output_root, raw_root, tables)

    print()
    print("=" * 72)
    print("BDDK AYLIK BULTEN")
    print("=" * 72)
    print(f"Year        : {args.year}")
    print(f"Months      : {', '.join(str(m) for m in args.months)}")
    print(f"Tables      : {', '.join(str(t.number) for t in tables)}")
    print(f"Output root : {output_root}")
    print("=" * 72)

    failed: List[str] = []
    written = 0
    skipped = 0

    for table in tables:
        directory = output_root / f"{table.number:02d}_{table.slug}"
        print(f"\n--- {table.number:02d} {table.title}")

        for month in args.months:
            label = f"{args.year}-{month:02d}"
            destination = directory / f"BDDK_{table.slug}_{args.year:04d}_{month:02d}.xlsx"

            if destination.exists() and not args.overwrite:
                skipped += 1
                continue

            try:
                response = fetch_report(table, args.year, month)
                headers, rows = extract_rows(response, table, args.year, month)
                write_workbook(headers, rows, table, args.year, month, destination)
            except Exception as exc:                                   # noqa: BLE001
                print(f"  [FAIL] {label}: {exc}")
                failed.append(f"t{table.number}/{label}")
                time.sleep(REQUEST_DELAY)
                continue

            if not args.no_raw_json:
                raw_path = raw_root / f"{table.number:02d}" / f"{args.year:04d}_{month:02d}.json"
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")

            written += 1
            drift = "" if len(rows) == table.rows_seen else f"  [rows {len(rows)} != {table.rows_seen}]"
            print(f"  [ OK ] {label} -> {destination.name} ({len(rows)} rows){drift}")
            sys.stdout.flush()
            time.sleep(REQUEST_DELAY)

    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"Written : {written}")
    print(f"Skipped : {skipped} (already on disk)")
    print(f"Failed  : {len(failed)}")

    if failed:
        print("Failed: " + ", ".join(failed))
        print("Rerun the same command; successful files are skipped automatically.")
        return 1

    print()
    print("[DONE] Rebuild the lakehouse with: .venv/bin/python -m backend.lakehouse.build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
