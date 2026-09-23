#!/usr/bin/env python3
"""
TCMB EVDS Downloader
====================

Archives the EVDS web-service responses for every data group in
`backend.domain.evds_series.GROUPS` under

    evds/_raw_json/_catalog/categories.json       public category tree
    evds/_raw_json/_catalog/serielist/<group>.json series metadata per group
    evds/_raw_json/<group>/<YYYY>[_pN].json        one calendar year of data
                                                   (pN = series batch)

The archive is the source of truth (committed), exactly like the BDDK
`_raw_json/`: the parser reads it, so a clone builds with no key and no
network. Responses are stored verbatim inside a small envelope that records
which series were asked for, because the response omits a series column
when nothing was returned for it.

Data are pulled at each series' NATIVE frequency. The monthly alignment is a
build-time transform with a declared rule per series, not a URL parameter --
that keeps the rule in the catalogue and keeps the native series for the
change-detection and anomaly tools.

The endpoint is the EVDS3 backend (`/igmevdsms-dis/`); the old
`evds2.../service/evds/` URLs redirect and are gone. Only the category tree is
public; `serieList` and data calls need the `key` request header.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from typing import Iterable, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..core.config import (
    EVDS_CATALOG_DIR,
    EVDS_FETCH_END,
    EVDS_FETCH_START,
    EVDS_SERIELIST_DIR,
    RAW_EVDS_JSON_DIR,
    evds_api_key,
)
from ..domain.evds_series import BY_CODE, GROUPS, DataGroup

BASE_URL = "https://evds3.tcmb.gov.tr/igmevdsms-dis/"

REQUEST_TIMEOUT = 90
RETRIES = 4
REQUEST_DELAY = 0.4

# A request URL carries every series code joined by '-'; 166 codes fit in
# ~3.5 KB, comfortably inside common limits. Batch above that to stay safe.
MAX_SERIES_PER_REQUEST = 150


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _get(path: str, key: Optional[str]) -> object:
    headers = {"Accept": "application/json"}
    if key:
        headers["key"] = key
    last_error: Optional[Exception] = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urlopen(Request(BASE_URL + path, headers=headers), timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            # 4xx are not transient: a bad series code or a missing key.
            body = exc.read().decode("utf-8", "replace")[:300]
            if 400 <= exc.code < 500:
                raise RuntimeError(f"EVDS {exc.code} for {path[:120]}...: {body}") from exc
            last_error = exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
        if attempt < RETRIES:
            wait = min(2 ** (attempt - 1), 8)
            print(f"    request failed ({attempt}/{RETRIES}), retrying in {wait}s: {last_error}")
            time.sleep(wait)
    raise RuntimeError(f"EVDS request failed: {path[:120]}...: {last_error}")


def fetch_categories() -> list:
    """The full category -> data group tree. Public, no key needed."""
    return _get("categories/withDatagroups/type=json", key=None)


def fetch_serielist(group_code: str, key: str) -> list:
    return _get(f"serieList/type=json&code={group_code}", key)


def fetch_data(series: Sequence[str], start: dt.date, end: dt.date, key: str) -> dict:
    query = (
        f"series={'-'.join(series)}"
        f"&startDate={start:%d-%m-%Y}&endDate={end:%d-%m-%Y}&type=json"
    )
    return _get(quote(query, safe="=&-."), key)


# ---------------------------------------------------------------------------
# Series selection
# ---------------------------------------------------------------------------

def load_serielist(group_code: str) -> list:
    path = EVDS_SERIELIST_DIR / f"{group_code}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No archived serieList for {group_code}: run `--catalog` first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def select_series(group: DataGroup, serielist: list) -> List[str]:
    """Apply the registry's narrowing to the published series list."""
    published = [s["SERIE_CODE"] for s in serielist]
    if group.series is not None:
        missing = [c for c in group.series if c not in published]
        if missing:
            raise ValueError(f"{group.code}: registry names unpublished series {missing}")
        return list(group.series)
    if group.max_level is not None:
        return [s["SERIE_CODE"] for s in serielist if (s.get("SEVIYE") or 1) <= group.max_level]
    return published


def _batches(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _year_windows(start: dt.date, end: dt.date) -> Iterable[tuple]:
    for year in range(start.year, end.year + 1):
        lo = max(start, dt.date(year, 1, 1))
        hi = min(end, dt.date(year, 12, 31))
        yield year, lo, hi


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def archive_catalog(groups: Sequence[DataGroup], key: str, overwrite: bool) -> int:
    EVDS_SERIELIST_DIR.mkdir(parents=True, exist_ok=True)
    categories_path = EVDS_CATALOG_DIR / "categories.json"
    if overwrite or not categories_path.exists():
        categories = fetch_categories()
        categories_path.write_text(json.dumps(categories, ensure_ascii=False), encoding="utf-8")
        print(f"  [ OK ] categories: {len(categories)} categories")
    else:
        print("  [SKIP] categories.json already archived")

    for group in groups:
        path = EVDS_SERIELIST_DIR / f"{group.code}.json"
        if path.exists() and not overwrite:
            print(f"  [SKIP] {group.code}")
            continue
        serielist = fetch_serielist(group.code, key)
        if not isinstance(serielist, list) or not serielist:
            raise RuntimeError(f"{group.code}: empty serieList response: {str(serielist)[:200]}")
        path.write_text(json.dumps(serielist, ensure_ascii=False), encoding="utf-8")
        chosen = select_series(group, serielist)
        print(f"  [ OK ] {group.code}: {len(serielist)} published, {len(chosen)} selected")
        time.sleep(REQUEST_DELAY)
    return 0


def archive_data(groups: Sequence[DataGroup], key: str, start: dt.date, end: dt.date,
                 overwrite: bool) -> int:
    written = skipped = 0
    failed: List[str] = []
    for group in groups:
        serielist = load_serielist(group.code)
        series = select_series(group, serielist)
        batches = list(_batches(series, MAX_SERIES_PER_REQUEST))
        directory = RAW_EVDS_JSON_DIR / group.code
        print(f"\n--- {group.code}: {len(series)} series in {len(batches)} batch(es)")

        for year, lo, hi in _year_windows(start, end):
            for index, batch in enumerate(batches, start=1):
                suffix = f"_p{index}" if len(batches) > 1 else ""
                destination = directory / f"{year}{suffix}.json"
                if destination.exists() and not overwrite:
                    skipped += 1
                    continue
                try:
                    response = fetch_data(batch, lo, hi, key)
                except Exception as exc:                               # noqa: BLE001
                    print(f"  [FAIL] {year}{suffix}: {exc}")
                    failed.append(f"{group.code}/{year}{suffix}")
                    time.sleep(REQUEST_DELAY)
                    continue
                envelope = {
                    "datagroup": group.code,
                    "series": list(batch),
                    "startDate": lo.isoformat(),
                    "endDate": hi.isoformat(),
                    "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "response": response,
                }
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
                written += 1
                print(f"  [ OK ] {year}{suffix}: {response.get('totalCount')} rows")
                sys.stdout.flush()
                time.sleep(REQUEST_DELAY)

    print()
    print("=" * 72)
    print(f"Written : {written}\nSkipped : {skipped} (already on disk)\nFailed  : {len(failed)}")
    if failed:
        print("Failed: " + ", ".join(failed))
        print("Rerun the same command; archived files are skipped automatically.")
        return 1
    print("\n[DONE] Rebuild the lakehouse with: .venv/bin/python -m backend.lakehouse.build")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_groups(value: str) -> List[DataGroup]:
    picked = []
    for code in value.split(","):
        code = code.strip()
        if not code:
            continue
        if code not in BY_CODE:
            raise argparse.ArgumentTypeError(f"unknown data group {code!r}; see --list")
        picked.append(BY_CODE[code])
    return picked


def _parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive TCMB EVDS series for the lakehouse.")
    parser.add_argument("--list", action="store_true", help="List the registered data groups and exit.")
    parser.add_argument("--catalog", action="store_true",
                        help="Archive the category tree and each group's serieList.")
    parser.add_argument("--fetch", action="store_true", help="Archive the data (needs --catalog first).")
    parser.add_argument("--groups", type=_parse_groups, default=None, metavar="a,b",
                        help="Restrict to these data group codes (default: every registered group).")
    parser.add_argument("--tier", type=int, default=None, choices=(0, 1, 2),
                        help="Restrict to groups of this tier and below.")
    parser.add_argument("--start", type=_parse_date, default=_parse_date(EVDS_FETCH_START), metavar="YYYY-MM-DD")
    parser.add_argument("--end", type=_parse_date, default=_parse_date(EVDS_FETCH_END), metavar="YYYY-MM-DD")
    parser.add_argument("--overwrite", action="store_true", help="Re-download files already on disk.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    groups = list(args.groups) if args.groups else list(GROUPS)
    if args.tier is not None:
        groups = [g for g in groups if g.tier <= args.tier]

    if args.list:
        print(f"{'tier':>4}  {'code':<18} {'semantics':<9} {'monthly':<8} note")
        for g in GROUPS:
            print(f"{g.tier:>4}  {g.code:<18} {g.semantics:<9} {g.monthly_rule:<8} {g.note}")
        return 0

    if not (args.catalog or args.fetch):
        build_parser().error("nothing to do: pass --catalog and/or --fetch (or --list)")

    key = evds_api_key()
    status = 0
    if args.catalog:
        print(f"\nArchiving catalogue to {EVDS_CATALOG_DIR}\n")
        status = archive_catalog(groups, key, args.overwrite)
    if args.fetch and status == 0:
        print(f"\nArchiving data {args.start}..{args.end} to {RAW_EVDS_JSON_DIR}")
        status = archive_data(groups, key, args.start, args.end, args.overwrite)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
