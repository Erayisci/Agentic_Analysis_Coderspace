#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TBB Risk Merkezi - Sectoral Credit Report Downloader
=====================================================

Downloads every Excel report in:
"Bankalarca Kullandırılan Kredilerin Sektörel Dağılımı"

Default range:
    2022-01 -> latest month currently available on the Risk Merkezi archive

Features:
- One-shot / portable Python script.
- Uses Python standard library for scraping/downloading.
- Automatically detects the latest available month.
- Automatically distinguishes PDF / XLSX / legacy XLS by file bytes,
  not by unreliable filename extensions.
- Ignores PDFs.
- Saves all output consistently as YYYY-MM.xlsx.
- If a historical report is legacy .xls, automatically installs the
  small Python dependencies needed to convert it to .xlsx.
- Safe to rerun: existing valid files are skipped.
- Verifies every month in the expected period range at the end.

Run:
    python riskmerkezi_download.py

Optional:
    python riskmerkezi_download.py --out ./my_folder
    python riskmerkezi_download.py --start 2022-01
    python riskmerkezi_download.py --overwrite
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import ssl
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


BASE_URL = "https://www.riskmerkezi.org"
ARCHIVE_URL = (
    "https://www.riskmerkezi.org/"
    "istatistiki-raporlar-liste/2556"
)

DEFAULT_START = (2022, 1)
REQUEST_TIMEOUT = 45
RETRIES = 4
REQUEST_DELAY = 0.20

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0 Safari/537.36"
)

TARGET_TITLE_FRAGMENT = (
    "bankalarca kullandırılan kredilerin sektörel dağılımı"
)

MONTHS = {
    "ocak": 1,
    "şubat": 2,
    "subat": 2,
    "mart": 3,
    "nisan": 4,
    "mayıs": 5,
    "mayis": 5,
    "haziran": 6,
    "temmuz": 7,
    "ağustos": 8,
    "agustos": 8,
    "eylül": 9,
    "eylul": 9,
    "ekim": 10,
    "kasım": 11,
    "kasim": 11,
    "aralık": 12,
    "aralik": 12,
}

REPORT_TITLE_RE = re.compile(
    r"^\s*(20\d{2})\s+"
    r"(Ocak|Şubat|Subat|Mart|Nisan|Mayıs|Mayis|Haziran|Temmuz|"
    r"Ağustos|Agustos|Eylül|Eylul|Ekim|Kasım|Kasim|Aralık|Aralik)"
    r"\s*-\s*Bankalarca\s+Kullandırılan\s+Kredilerin\s+Sektörel\s+Dağılımı",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

class AnchorParser(HTMLParser):
    """Collect anchors in DOM order as (href, visible_text)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: List[Tuple[str, str]] = []
        self._href: Optional[str] = None
        self._text_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a":
            return

        attrs_dict = dict(attrs)
        self._href = attrs_dict.get("href")
        self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return

        text = " ".join("".join(self._text_parts).split())
        self.anchors.append((self._href, text))

        self._href = None
        self._text_parts = []


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def normalize_text(value: str) -> str:
    return (
        value.lower()
        .replace("İ", "i")
        .replace("I", "ı")
        .strip()
    )


def parse_period(value: str) -> Tuple[int, int]:
    match = re.fullmatch(r"(\d{4})-(\d{2})", value.strip())
    if not match:
        raise argparse.ArgumentTypeError(
            f"Invalid period {value!r}; expected YYYY-MM"
        )

    year = int(match.group(1))
    month = int(match.group(2))

    if not 1 <= month <= 12:
        raise argparse.ArgumentTypeError(
            f"Invalid month in {value!r}"
        )

    return year, month


def period_str(period: Tuple[int, int]) -> str:
    return f"{period[0]:04d}-{period[1]:02d}"


def month_range(
    start: Tuple[int, int],
    end: Tuple[int, int],
):
    year, month = start

    while (year, month) <= end:
        yield (year, month)

        month += 1
        if month == 13:
            month = 1
            year += 1


def month_count(
    start: Tuple[int, int],
    end: Tuple[int, int],
) -> int:
    return (
        (end[0] - start[0]) * 12
        + (end[1] - start[1])
        + 1
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch_bytes(
    url: str,
    *,
    referer: Optional[str] = None,
) -> Tuple[bytes, str]:
    """
    Fetch URL with retries.
    Returns: (body, content_type)
    """

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        "Accept": "*/*",
    }

    if referer:
        headers["Referer"] = referer

    # Normal verified TLS context.
    context = ssl.create_default_context()

    last_error: Optional[Exception] = None

    for attempt in range(1, RETRIES + 1):
        try:
            request = Request(
                url,
                headers=headers,
                method="GET",
            )

            with urlopen(
                request,
                timeout=REQUEST_TIMEOUT,
                context=context,
            ) as response:
                body = response.read()
                content_type = response.headers.get(
                    "Content-Type",
                    "",
                )
                return body, content_type

        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc

            if attempt < RETRIES:
                wait = min(2 ** (attempt - 1), 8)
                print(
                    f"    request failed ({attempt}/{RETRIES}), "
                    f"retrying in {wait}s: {exc}"
                )
                time.sleep(wait)

    raise RuntimeError(
        f"Failed to fetch {url}: {last_error}"
    )


def fetch_html(url: str) -> str:
    body, content_type = fetch_bytes(url)

    # The site uses UTF-8; tolerate bad bytes rather than aborting.
    return body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Report discovery
# ---------------------------------------------------------------------------

def discover_reports() -> Dict[Tuple[int, int], List[str]]:
    """
    Read the archive page and map:
        (YYYY, MM) -> [attachment URLs for that report]

    We intentionally keep all attachments for the matching report because
    newer entries sometimes have ambiguous names/extensions. Later we inspect
    the actual binary signature and select the Excel file.
    """

    print(f"Reading archive:\n  {ARCHIVE_URL}")

    html = fetch_html(ARCHIVE_URL)

    parser = AnchorParser()
    parser.feed(html)

    reports: Dict[Tuple[int, int], List[str]] = {}
    current_period: Optional[Tuple[int, int]] = None
    inside_target_report = False

    for href, visible_text in parser.anchors:
        title_match = REPORT_TITLE_RE.match(visible_text)

        if title_match:
            year = int(title_match.group(1))
            month_name = normalize_text(title_match.group(2))
            month = MONTHS.get(month_name)

            if month is None:
                raise RuntimeError(
                    f"Could not parse Turkish month: "
                    f"{title_match.group(2)!r}"
                )

            current_period = (year, month)
            reports.setdefault(current_period, [])
            inside_target_report = True
            continue

        # Any other report-title-like anchor means attachments that follow
        # should not accidentally be assigned to our previous target report.
        if re.match(r"^\s*20\d{2}\s+\S+", visible_text):
            if TARGET_TITLE_FRAGMENT not in normalize_text(visible_text):
                inside_target_report = False
                current_period = None

        if (
            inside_target_report
            and current_period is not None
            and "/download/" in href
        ):
            full_url = urljoin(BASE_URL, href)
            if full_url not in reports[current_period]:
                reports[current_period].append(full_url)

    # Drop report titles for which no attachment links were captured.
    reports = {
        period: urls
        for period, urls in reports.items()
        if urls
    }

    if not reports:
        raise RuntimeError(
            "No matching reports were found. "
            "The Risk Merkezi page structure may have changed."
        )

    return reports


# ---------------------------------------------------------------------------
# File identification
# ---------------------------------------------------------------------------

def detect_binary_type(data: bytes) -> Optional[str]:
    """
    Detect actual format by magic bytes.

    XLSX is a ZIP container.
    XLS is an OLE Compound File.
    PDF starts with %PDF.
    """

    if data.startswith(b"%PDF"):
        return "pdf"

    if data.startswith(b"PK\x03\x04"):
        return "xlsx"

    if data.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        return "xls"

    return None


def is_valid_xlsx(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 100:
        return False

    try:
        with path.open("rb") as f:
            return detect_binary_type(f.read(8)) == "xlsx"
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Dependency bootstrap for legacy XLS conversion
# ---------------------------------------------------------------------------

def ensure_module(
    import_name: str,
    pip_name: Optional[str] = None,
) -> None:
    """
    Install an optional dependency automatically if missing.

    This is only needed when Risk Merkezi serves a historical .xls file.
    Modern .xlsx downloads require no third-party packages at all.
    """

    try:
        importlib.import_module(import_name)
        return
    except ImportError:
        pass

    package = pip_name or import_name

    print(
        f"    Missing dependency {package!r}; "
        f"installing automatically..."
    )

    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--quiet",
        package,
    ]

    try:
        subprocess.check_call(command)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Could not install required package {package!r}. "
            f"Try manually: {sys.executable} -m pip install {package}"
        ) from exc

    importlib.invalidate_caches()

    try:
        importlib.import_module(import_name)
    except ImportError as exc:
        raise RuntimeError(
            f"{package!r} was installed but could not be imported."
        ) from exc


def convert_xls_to_xlsx(
    source_bytes: bytes,
    destination: Path,
) -> None:
    """
    Convert legacy XLS to XLSX.

    This preserves cell values and sheet layout dimensions sufficiently for
    analytical ingestion. It does not attempt pixel-perfect formatting.
    """

    ensure_module("xlrd")
    ensure_module("openpyxl")

    import io
    import xlrd
    from openpyxl import Workbook

    source = xlrd.open_workbook(
        file_contents=source_bytes,
        formatting_info=False,
    )

    output = Workbook()

    # Remove default sheet after creating the real sheets.
    default_sheet = output.active

    created_any = False

    for sheet_index in range(source.nsheets):
        src_sheet = source.sheet_by_index(sheet_index)

        # Excel limits worksheet titles to 31 characters.
        title = src_sheet.name[:31] or f"Sheet{sheet_index + 1}"

        if not created_any:
            dst_sheet = default_sheet
            dst_sheet.title = title
            created_any = True
        else:
            dst_sheet = output.create_sheet(title=title)

        for row_index in range(src_sheet.nrows):
            for col_index in range(src_sheet.ncols):
                cell = src_sheet.cell(row_index, col_index)
                value = cell.value

                # xlrd represents Excel dates as serial numbers. Convert them
                # when the source cell is typed as a date.
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        value = xlrd.xldate.xldate_as_datetime(
                            value,
                            source.datemode,
                        )
                    except Exception:
                        pass

                # Excel errors are not useful numerical input; preserve a
                # readable marker rather than crashing conversion.
                elif cell.ctype == xlrd.XL_CELL_ERROR:
                    value = f"#ERROR({int(value)})"

                dst_sheet.cell(
                    row=row_index + 1,
                    column=col_index + 1,
                    value=value,
                )

    destination.parent.mkdir(parents=True, exist_ok=True)
    output.save(destination)


# ---------------------------------------------------------------------------
# Download one report
# ---------------------------------------------------------------------------

def download_report(
    period: Tuple[int, int],
    attachment_urls: List[str],
    destination: Path,
    *,
    overwrite: bool,
) -> bool:
    label = period_str(period)

    if (
        not overwrite
        and is_valid_xlsx(destination)
    ):
        print(f"[SKIP] {label} already exists")
        return True

    if destination.exists() and overwrite:
        try:
            destination.unlink()
        except OSError:
            pass

    saw_pdf = False
    unknown_types = []

    for index, url in enumerate(attachment_urls, start=1):
        try:
            data, content_type = fetch_bytes(
                url,
                referer=ARCHIVE_URL,
            )
        except Exception as exc:
            print(
                f"[WARN] {label}: attachment "
                f"{index}/{len(attachment_urls)} failed: {exc}"
            )
            continue

        file_type = detect_binary_type(data)

        if file_type == "pdf":
            saw_pdf = True
            continue

        if file_type == "xlsx":
            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            destination.write_bytes(data)

            if not is_valid_xlsx(destination):
                print(
                    f"[WARN] {label}: downloaded XLSX failed "
                    "post-write validation"
                )
                try:
                    destination.unlink()
                except OSError:
                    pass
                continue

            print(
                f"[ OK ] {label} -> {destination.name} "
                f"({len(data):,} bytes)"
            )
            return True

        if file_type == "xls":
            print(
                f"[INFO] {label}: legacy XLS found; "
                "converting automatically to XLSX"
            )

            try:
                convert_xls_to_xlsx(
                    data,
                    destination,
                )
            except Exception as exc:
                print(
                    f"[WARN] {label}: XLS conversion failed: {exc}"
                )
                continue

            if not is_valid_xlsx(destination):
                print(
                    f"[WARN] {label}: converted XLSX failed validation"
                )
                try:
                    destination.unlink()
                except OSError:
                    pass
                continue

            print(
                f"[ OK ] {label} -> {destination.name} "
                "(converted from legacy XLS)"
            )
            return True

        unknown_types.append(
            (
                url,
                content_type,
                data[:32],
            )
        )

    print(f"[MISS] {label}: no usable Excel attachment found")

    if unknown_types:
        for url, content_type, prefix in unknown_types:
            print(
                f"       unknown attachment: {url}\n"
                f"       content-type={content_type!r}, "
                f"prefix={prefix!r}"
            )
    elif saw_pdf:
        print(
            "       PDF attachment(s) existed, "
            "but no Excel attachment was detected."
        )

    return False


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download TBB Risk Merkezi sectoral credit Excel reports "
            "from 2022-01 (or a custom start month) through the latest "
            "available month."
        )
    )

    parser.add_argument(
        "--start",
        type=parse_period,
        default=DEFAULT_START,
        metavar="YYYY-MM",
        help="First month to download (default: 2022-01)",
    )

    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Output directory. Default: a 'riskmerkezi_sectoral' "
            "folder next to this script."
        ),
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Redownload files that already exist.",
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    script_dir = Path(__file__).resolve().parent
    output_dir = (
        args.out.expanduser().resolve()
        if args.out is not None
        else script_dir / "riskmerkezi_sectoral"
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        reports = discover_reports()
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2

    candidate_periods = sorted(
        period
        for period in reports
        if period >= args.start
    )

    if not candidate_periods:
        print(
            f"ERROR: no reports found from "
            f"{period_str(args.start)} onward.",
            file=sys.stderr,
        )
        return 2

    latest = max(candidate_periods)

    expected = list(
        month_range(args.start, latest)
    )

    print()
    print("=" * 72)
    print("TBB RISK MERKEZI - SECTORAL CREDIT REPORTS")
    print("=" * 72)
    print(f"Start            : {period_str(args.start)}")
    print(f"Latest detected  : {period_str(latest)}")
    print(f"Expected months  : {month_count(args.start, latest)}")
    print(f"Output directory : {output_dir}")
    print("=" * 72)
    print()

    for period in expected:
        urls = reports.get(period)

        if not urls:
            print(
                f"[MISS] {period_str(period)}: "
                "month absent from archive page"
            )
            continue

        destination = (
            output_dir
            / f"TBB_Sektorel_Kredi_Dagilimi_{period[0]:04d}_{period[1]:02d}.xlsx"
        )

        download_report(
            period,
            urls,
            destination,
            overwrite=args.overwrite,
        )

        time.sleep(REQUEST_DELAY)

    # ------------------------------------------------------------------
    # Final verification
    # ------------------------------------------------------------------

    missing: List[Tuple[int, int]] = []

    for period in expected:
        path = (
            output_dir
            / f"TBB_Sektorel_Kredi_Dagilimi_{period[0]:04d}_{period[1]:02d}.xlsx"
        )

        if not is_valid_xlsx(path):
            missing.append(period)

    completed = len(expected) - len(missing)

    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"Range            : {period_str(args.start)} -> {period_str(latest)}")
    print(f"Expected         : {len(expected)}")
    print(f"Valid XLSX files : {completed}")
    print(f"Missing          : {len(missing)}")

    if missing:
        print()
        print("Missing months:")
        for period in missing:
            print(f"  - {period_str(period)}")

        print()
        print(
            "Some months failed. Rerun the same script; "
            "successful files will be skipped automatically."
        )
        return 1

    print()
    print("[DONE] Complete series downloaded successfully.")
    print(f"[DONE] Folder: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
