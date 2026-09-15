#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BDDK Haftalik Bulten Downloader
===============================

Fetches the nine weekly-bulletin tables from BDDK's `Gelişmiş Gösterim` report
and archives them under

    bddk_haftalik_bulten/_raw/_catalog/kalemler.json   the item picker
    bddk_haftalik_bulten/_raw/<tabloId>.json           one table, whole history

Run:
    python -m backend.ingestion.bddk_weekly --catalog        # refresh the item tree
    python -m backend.ingestion.bddk_weekly --fetch          # all nine tables
    python -m backend.ingestion.bddk_weekly --fetch --tables 289,292
    python -m backend.ingestion.bddk_weekly --list

Why this endpoint and not the weekly page
-----------------------------------------
`Temel Gösterim` renders one table for one week, which would be 296 weeks x 9
tables = 2664 requests against a regulator's server for one corpus. `Gelişmiş
Gösterim` takes a date range and a list of item ids and answers with every week
in the range at once, so the same corpus is NINE requests. It also answers at
full precision (five decimals) where the basic page rounds to whole millions.

Session protocol, measured against the live site
------------------------------------------------
1. GET /BultenHaftalik/tr/Gelismis            -> session cookie + the item tree
2. read `__RequestVerificationToken` from the report form on that page
3. POST /BultenHaftalik/tr/Gelismis/GelismisRaporGetir with that token and the
   cookie. The token is bound to the session cookie, so both must come from the
   same GET; a token reused without its cookie is rejected.

What gets archived
------------------
The response is a ~1 MB HTML page of which ~90% is navigation chrome that would
dominate the repository and change on every BDDK site tweak. So the envelope
stores the request that produced it plus the report table VERBATIM -- the
values, the published labels, the unit caption and the column headers, none of
them reinterpreted. That is the same bargain `backend.ingestion.evds` strikes:
archive the answer, not the page it arrived on, and record the question beside
it so the archive can be regenerated and diffed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.cookiejar
import json
import re
import ssl
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener

from ..core.config import (
    RAW_BDDK_WEEKLY_CATALOG,
    RAW_BDDK_WEEKLY_DIR,
    WEEKLY_FETCH_START,
)
from ..domain.weekly_tables import BY_ID, CURRENCY_COLUMNS, TABLES, TARAF

BASE = "https://www.bddk.org.tr/BultenHaftalik"
ADVANCED_URL = f"{BASE}/tr/Gelismis"
REPORT_URL = f"{BASE}/tr/Gelismis/GelismisRaporGetir"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 120       # a nine-year range for 45 items is a ~5 MB response
RETRIES = 4
REQUEST_DELAY = 1.5         # nine requests total; there is no reason to hurry

# The report form on the advanced page, and the anti-forgery token inside it.
_REPORT_FORM = re.compile(
    r'<form action="/BultenHaftalik/tr/Gelismis/GelismisRaporGetir".*?</form>', re.S)
_TOKEN = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')

# One block per table in the picker: <div id="Kalemler-289" class="Kalemler">.
_ITEM_BLOCK = re.compile(r'(?=<div id="Kalemler-(\d+)")')

# An item inside a block: its id, the label the picker passes to KalemToggle,
# and the span that additionally carries the row code and any retirement date.
_ITEM = re.compile(
    r'id="Kalem-(\d+)"[^>]*onclick="KalemToggle\(\d+,\s*\'(.*?)\'\)".*?'
    r'<span class="text">(.*?)</span>', re.S)

# The report renders the same numbers three times at increasing precision; the
# last one is unrounded and is the only one archived.
_TABLE = re.compile(r"<table.*?</table>", re.S | re.I)
_ROW = re.compile(r"<tr.*?</tr>", re.S | re.I)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _opener():
    """One opener per run: the anti-forgery token is bound to the session cookie.

    www.bddk.org.tr serves an incomplete certificate chain, so the default
    verified context fails here while browsers succeed via AIA fetching -- the
    same reason `ingestion.bddk_bulletin` relaxes it.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return build_opener(
        HTTPSHandler(context=context),
        HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )


def _request(opener, url: str, data: Optional[bytes], label: str) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        "Referer": ADVANCED_URL,
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["Origin"] = "https://www.bddk.org.tr"

    last_error: Optional[Exception] = None
    for attempt in range(1, RETRIES + 1):
        try:
            request = Request(url, data=data, headers=headers,
                              method="POST" if data is not None else "GET")
            with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                return response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < RETRIES:
                wait = min(2 ** (attempt - 1), 8)
                print(f"    request failed ({attempt}/{RETRIES}), retrying in {wait}s: {exc}")
                time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {label}: {last_error}")


def open_session(opener) -> Tuple[str, str]:
    """GET the advanced page; return (page html, anti-forgery token).

    The token lives in several forms on the page and they are not
    interchangeable -- only the one inside the report form is accepted by
    `GelismisRaporGetir` -- so it is read from that form specifically.
    """
    page = _request(opener, ADVANCED_URL, None, "advanced page")
    form = _REPORT_FORM.search(page)
    if not form:
        raise ValueError(
            "The advanced page carries no GelismisRaporGetir form. BDDK has "
            "changed the weekly bulletin's layout; re-derive the session protocol."
        )
    token = _TOKEN.search(form.group(0))
    if not token:
        raise ValueError("GelismisRaporGetir form carries no anti-forgery token.")
    return page, token.group(1)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

def extract_item_blocks(page: str) -> Dict[int, str]:
    """The picker's per-table item blocks, keyed by table id."""
    blocks: Dict[int, str] = {}
    for chunk in _ITEM_BLOCK.split(page):
        match = re.match(r'<div id="Kalemler-(\d+)"', chunk or "")
        if match:
            blocks[int(match.group(1))] = chunk
    return blocks


def fetch_catalogue(opener, token_page: str) -> dict:
    """Archive the item picker: every item id, label and published lifecycle."""
    blocks = extract_item_blocks(token_page)
    known = {t.table_id for t in TABLES}
    if set(blocks) != known:
        raise ValueError(
            f"The picker lists tables {sorted(blocks)}, the registry declares "
            f"{sorted(known)}. Update domain.weekly_tables rather than the parser."
        )

    tables = []
    for table in TABLES:
        items = _ITEM.findall(blocks[table.table_id])
        if not items:
            raise ValueError(f"table {table.table_id}: the picker lists no items")
        if len(items) != table.items_seen:
            print(f"    note: table {table.table_id} lists {len(items)} items, "
                  f"registry saw {table.items_seen}")
        tables.append({
            "table_id": table.table_id,
            "slug": table.slug,
            # Verbatim triples; every interpretation happens in the parser.
            "items": [{"item_id": int(i), "label": label, "display": display}
                      for i, label, display in items],
        })
    return {
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        "url": ADVANCED_URL,
        "tables": tables,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _result_table(page: str, label: str) -> str:
    """The unrounded report table, verbatim.

    The report renders its numbers three times -- whole millions, two decimals,
    then unrounded -- and every other table on the page is picker furniture. So
    the pick is 'the last table with more rows than a picker block has'.
    """
    candidates = [t for t in _TABLE.findall(page) if len(_ROW.findall(t)) >= 50]
    if not candidates:
        raise ValueError(
            f"{label}: the response carries no report table. Either the date range "
            "returned nothing or BDDK changed the report layout."
        )
    return candidates[-1]


def fetch_table(opener, token: str, table_id: int, items: List[int],
                start: str, end: str) -> dict:
    """POST one table's whole history and return the archive envelope."""
    table = BY_ID[table_id]
    fields: List[Tuple[str, str]] = [
        ("__RequestVerificationToken", token),
        ("BaslangicTarihi", f"{start} 00:00:00"),
        ("BitisTarihi", f"{end} 00:00:00"),
        ("dil", "tr"),
        ("Taraflar", str(TARAF)),
        ("SeciliParalar", "TL"),
    ]
    fields += [("Kalemler", str(i)) for i in items]
    fields += [("kalemSutun", column) for column in CURRENCY_COLUMNS]

    label = f"table {table_id} ({table.slug})"
    page = _request(opener, REPORT_URL, urlencode(fields).encode("utf-8"), label)
    return {
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        "url": REPORT_URL,
        # The question, archived beside the answer: an envelope that does not
        # record what was asked cannot be checked for completeness later.
        "request": {
            "table_id": table_id,
            "slug": table.slug,
            "start": start,
            "end": end,
            "items": items,
            "taraf": TARAF,
            "currency_columns": list(CURRENCY_COLUMNS),
        },
        "table_html": _result_table(page, label),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _write(path: Path, payload: dict) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="BDDK weekly bulletin downloader")
    parser.add_argument("--list", action="store_true", help="list the nine tables and exit")
    parser.add_argument("--catalog", action="store_true", help="refresh the item picker archive")
    parser.add_argument("--fetch", action="store_true", help="fetch table data")
    parser.add_argument("--tables", help="comma-separated table ids (default: all nine)")
    parser.add_argument("--start", default=WEEKLY_FETCH_START, help="d.mm.yyyy")
    parser.add_argument("--end", help="d.mm.yyyy (default: today)")
    args = parser.parse_args(argv)

    if args.list:
        for table in TABLES:
            print(f"  {table.table_id}  {table.slug:34s} {table.items_seen:3d} items  {table.title}")
        return 0
    if not (args.catalog or args.fetch):
        parser.error("nothing to do: pass --catalog, --fetch or --list")

    wanted = [int(x) for x in args.tables.split(",")] if args.tables else [t.table_id for t in TABLES]
    unknown = [t for t in wanted if t not in BY_ID]
    if unknown:
        parser.error(f"unknown table id(s) {unknown}; run --list")

    end = args.end or dt.date.today().strftime("%-d.%m.%Y")
    opener = _opener()
    print(f"Opening session at {ADVANCED_URL} ...")
    page, token = open_session(opener)

    catalogue_path = RAW_BDDK_WEEKLY_CATALOG / "kalemler.json"
    if args.catalog or not catalogue_path.exists():
        catalogue = fetch_catalogue(opener, page)
        size = _write(catalogue_path, catalogue)
        total = sum(len(t["items"]) for t in catalogue["tables"])
        print(f"  catalogue: {total} items across {len(catalogue['tables'])} tables "
              f"-> {catalogue_path.name} ({size/1024:.0f} KB)")

    if not args.fetch:
        return 0

    catalogue = json.loads(catalogue_path.read_text(encoding="utf-8"))
    items_by_table = {t["table_id"]: [i["item_id"] for i in t["items"]] for t in catalogue["tables"]}

    print(f"Fetching {len(wanted)} table(s) for {args.start}..{end}")
    for position, table_id in enumerate(wanted, 1):
        items = items_by_table[table_id]
        envelope = fetch_table(opener, token, table_id, items, args.start, end)
        size = _write(RAW_BDDK_WEEKLY_DIR / f"{table_id}.json", envelope)
        weeks = len(_ROW.findall(envelope["table_html"]))
        print(f"  [{position}/{len(wanted)}] {table_id} {BY_ID[table_id].slug:34s} "
              f"{len(items):3d} items, {weeks:4d} rows, {size/1024:6.0f} KB")
        if position < len(wanted):
            time.sleep(REQUEST_DELAY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
