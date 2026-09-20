#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BDDK FinTurk Downloader
=======================

Fetches the seven FinTurk (il-bazli / geographic distribution) tables from

    POST https://www.bddk.org.tr/BultenFinturk/tr/Home/VeriGetir

and archives each (table, quarter) response verbatim under

    bddk_finturk/_raw_json/<NN>_<slug>/<donem>.json

FinTurk answers one quarter per request (unlike the weekly bulletin's date
range), but unlike the monthly bulletin's fixed `taraf=10001`, it accepts every
`taraf` and every province in ONE call: measured live, `tarafList`/`sehirList`
sent as repeated form keys (ASP.NET MVC's ordinary `List<T>` binding, no index
suffix needed) return every requested group's rows in one response. So the
whole corpus is `len(tables) * len(quarters)` requests, not that times seven
taraf groups times 82 provinces -- for the brief's 2021-Q1..2026-Q2 window,
7 * 22 = 154 requests, comparable to the weekly bulletin's nine.

No session/CSRF handshake is needed here (measured: a bare POST with no prior
GET succeeds), unlike the weekly bulletin's __RequestVerificationToken dance --
a simpler protocol than either sibling bulletin's.

Run:
    python -m backend.ingestion.bddk_finturk --list
    python -m backend.ingestion.bddk_finturk --fetch
    python -m backend.ingestion.bddk_finturk --fetch --tables 1,5 --start 2024-3 --end 2026-6
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
from pathlib import Path
from typing import List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..core.config import FINTURK_FETCH_END, FINTURK_FETCH_START, RAW_BDDK_FINTURK_JSON_DIR
from ..domain.finturk_tables import ALL_PROVINCES, BY_NUMBER, TABLES, TARAF_GROUPS, FinturkTable

ENDPOINT = "https://www.bddk.org.tr/BultenFinturk/tr/Home/VeriGetir"
REFERER = "https://www.bddk.org.tr/BultenFinTurk"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 45
RETRIES = 4
REQUEST_DELAY = 0.5

_DONEM = re.compile(r"^(\d{4})-(3|6|9|12)$")


def _ssl_context() -> ssl.SSLContext:
    """www.bddk.org.tr serves an incomplete certificate chain; see bddk_bulletin."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def quarters_between(start: str, end: str) -> List[str]:
    """'2021-3'..'2026-6' -> every quarter-end donem value in between, inclusive.

    FinTurk's own dropdown speaks unpadded 'YYYY-M' (month 3/6/9/12, not
    'YYYY-03'), and the endpoint is strict about it -- padding produces an
    empty response rather than an error, which is why this is validated here
    rather than left to string formatting at the call site.
    """
    start_match, end_match = _DONEM.match(start), _DONEM.match(end)
    if not start_match or not end_match:
        raise ValueError(f"donem must look like 'YYYY-3'/'YYYY-6'/'YYYY-9'/'YYYY-12', got {start!r}/{end!r}")
    start_year, start_q = int(start_match.group(1)), int(start_match.group(2))
    end_year, end_q = int(end_match.group(1)), int(end_match.group(2))
    if (start_year, start_q) > (end_year, end_q):
        raise ValueError(f"start {start!r} is after end {end!r}")

    quarters = []
    year, month = start_year, start_q
    while (year, month) <= (end_year, end_q):
        quarters.append(f"{year}-{month}")
        month += 3
        if month > 12:
            month, year = 3, year + 1
    return quarters


def fetch_report(table: FinturkTable, donem: str) -> dict:
    """POST one (table, quarter) to the FinTurk endpoint; every taraf and province."""
    fields = [("tabloNo", str(table.number)), ("donem", donem)]
    fields += [("tarafList", str(code)) for code in TARAF_GROUPS]
    fields += [("sehirList", ALL_PROVINCES)]
    payload = urlencode(fields, doseq=False).encode("utf-8")

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
    raise RuntimeError(f"Failed to fetch table {table.number} for {donem}: {last_error}")


def parse_tables_arg(value: str) -> List[int]:
    picked: List[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            picked.extend(range(int(lo), int(hi) + 1))
        else:
            picked.append(int(part))
    unknown = [n for n in picked if n not in BY_NUMBER]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown table number(s) {unknown}; run --list")
    return sorted(set(picked))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download BDDK FinTurk (il-bazli) tables as JSON.")
    parser.add_argument("--list", action="store_true", help="List the seven tables and exit.")
    parser.add_argument("--fetch", action="store_true", help="Fetch table data.")
    parser.add_argument("--tables", type=parse_tables_arg, default=[t.number for t in TABLES],
                        metavar="1-7", help="Tables to fetch, e.g. '1,5' or '1-4' (default: all seven)")
    parser.add_argument("--start", default=FINTURK_FETCH_START, help="First quarter, e.g. 2021-3")
    parser.add_argument("--end", default=FINTURK_FETCH_END, help="Last quarter, e.g. 2026-6")
    parser.add_argument("--overwrite", action="store_true", help="Refetch quarters already on disk.")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list:
        print(f"{'No':>3}  {'Slug':<22} {'Unit':<8} Title")
        for table in TABLES:
            print(f"{table.number:>3}  {table.slug:<22} {table.unit:<8} {table.title}")
        return 0

    if not args.fetch:
        parser.error("nothing to do: pass --fetch or --list")

    quarters = quarters_between(args.start, args.end)
    tables = [BY_NUMBER[n] for n in args.tables]

    print()
    print("=" * 72)
    print("BDDK FINTURK")
    print("=" * 72)
    print(f"Quarters : {quarters[0]}..{quarters[-1]} ({len(quarters)} total)")
    print(f"Tables   : {', '.join(str(t.number) for t in tables)}")
    print(f"Output   : {RAW_BDDK_FINTURK_JSON_DIR}")
    print("=" * 72)

    written = 0
    skipped = 0
    failed: List[str] = []

    for table in tables:
        directory = RAW_BDDK_FINTURK_JSON_DIR / f"{table.number:02d}_{table.slug}"
        print(f"\n--- {table.number:02d} {table.title}")

        for donem in quarters:
            destination = directory / f"{donem}.json"
            if destination.exists() and not args.overwrite:
                skipped += 1
                continue

            try:
                response = fetch_report(table, donem)
            except Exception as exc:                                   # noqa: BLE001
                print(f"  [FAIL] {donem}: {exc}")
                failed.append(f"t{table.number}/{donem}")
                time.sleep(REQUEST_DELAY)
                continue

            if not response.get("success"):
                print(f"  [FAIL] {donem}: endpoint reported success=false ({response.get('error')})")
                failed.append(f"t{table.number}/{donem}")
                time.sleep(REQUEST_DELAY)
                continue

            n_rows = len(((response.get("Json") or {}).get("data") or {}).get("rows") or [])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
            written += 1
            print(f"  [ OK ] {donem} -> {destination.name} ({n_rows} rows)")
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
