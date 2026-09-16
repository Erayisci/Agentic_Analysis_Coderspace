"""Parser for the archived BDDK weekly-bulletin reports.

Reads `bddk_haftalik_bulten/_raw/` (see `backend.ingestion.bddk_weekly` for the
layout) and emits two frames shaped to match the monthly path, so the two BDDK
corpora answer the same kind of question with the same column names:

    items         one row per line item: its published label, row position,
                  parent, formula, and BDDK's own retirement date
    observations  long facts: period, dataset, entity, currency, value

Facts the parser depends on, every one of them measured against the archive
rather than assumed:

The report's column order is the order the items were requested in.
    Table 297 carries ten retired items whose labels are IDENTICAL to the ten
    that replaced them, so a column cannot be identified by its header. It can
    be identified by its position, because the archive envelope records the
    request that produced it. The parser asserts the two line up and refuses to
    guess if they do not.

The picker's order is the table's row order, and the labels address it.
    'Toplam Krediler (2+10)' means items 2 and 10 of this table, counting active
    items down the picker. Verified at 2026-09-04 across table 289: item 1 equals
    items 2 + 10 exactly, and item 3 equals 4 + 5 + 6. That makes the weekly
    identities machine-derivable from the labels, exactly as the monthly ones
    are, and it is also where `parent_key` comes from -- an item whose label adds
    up a list of positions is the parent of those rows.

A retired item is a superseded DEFINITION, not a series that stopped.
    Where a retired item and an active one overlap they carry identical values:
    measured, all 19 retired items that hold data in the corpus window agree
    with an active item of the same table on every date where both are present.
    They are kept, because deleting published data is worse than labelling it,
    but they carry `retired_on` and they are excluded from the identity and
    continuity checks -- their formulas address the OLD row positions, which the
    current picker no longer has.
"""
import datetime as dt
import html
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from ..core.labels import canonical_key, strip_decorations
from ..domain.weekly_tables import (
    BY_ID,
    BY_SLUG,
    CURRENCY_KEYS,
    ENTITY_TYPE,
    UNIT,
    WEEKLY_FORMULA_OVERRIDES,
)

_TR_ROW = re.compile(r"<tr.*?</tr>", re.S | re.I)
_TR_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")

_DATE = re.compile(r"^(\d{1,2})\.(\d{2})\.(\d{4})$")

# 'Birim: Milyon TL' -- the report states its own unit, the same way the monthly
# response states it in `Json.caption`.
_STATED_UNIT = re.compile(r"Birim\s*:\s*(.+)$", re.I)

# The picker prefixes a retired item with its old row code and suffixes it with
# the date it stopped: '3.0.2 - Gerçeğe Uygun Değer ... (Sonlandırılma Tarihi= 10-09-2022 )'.
_ROW_CODE = re.compile(r"^(\d+(?:\.\d+)+)\s*-\s*")
_RETIRED = re.compile(r"Sonlandırılma\s*Tarihi\s*=\s*(\d{2})-(\d{2})-(\d{4})")

# A label that says the row is published for information and sits OUTSIDE its
# table's totals. 31 of the 201 items say so; the monthly bulletin hides the
# same fact in a footnote, where no arithmetic check can reach it.
_INFORMATIONAL = re.compile(r"\(\s*Bilgi(\s+için)?\s*\)", re.I)

# An additive formula: only these define a parent. '(2-3)' is a net position and
# '(1+4)' over rows that already have parents is a total, not a parentage.
_ADDITIVE = re.compile(r"^\(\s*\d+(?:\s*\+\s*\d+)+\s*\)$")


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", fragment or "")).split())


def _cells(row_html: str) -> List[str]:
    return [_text(c) for c in _TR_CELL.findall(row_html)]


def parse_number(text: str) -> Optional[float]:
    """'28.128.197,29545' -> 28128197.29545; '' and '-' are missing, not zero."""
    text = (text or "").strip()
    if text in ("", "-"):
        return None
    return float(text.replace(".", "").replace(",", "."))


def parse_date(text: str) -> dt.date:
    match = _DATE.match((text or "").strip())
    if not match:
        raise ValueError(f"unrecognised weekly date label {text!r}")
    day, month, year = (int(g) for g in match.groups())
    return dt.date(year, month, day)


def stated_unit(header_cell: str) -> Optional[str]:
    """The unit the report states about itself, normalised to the registry's spelling."""
    match = _STATED_UNIT.search(header_cell or "")
    if not match:
        return None
    return " ".join(match.group(1).split()).lower().replace("tl", "TL")


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

def parse_item_catalogue(catalog_path: Path) -> pd.DataFrame:
    """The picker: one row per line item, with its published lifecycle.

    `row_position` is the item's place in its table, counting ACTIVE items only,
    because that is what the labels' formulas address. Retired items get none --
    their codes refer to a layout the table no longer has.
    """
    if not catalog_path.exists():
        raise FileNotFoundError(
            f"No weekly item catalogue at {catalog_path}. The archive is committed; "
            "if it is missing, run: python -m backend.ingestion.bddk_weekly --catalog"
        )
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))

    records = []
    for block in payload["tables"]:
        table = BY_ID[block["table_id"]]
        position = 0
        for entry in block["items"]:
            display = _text(entry["display"])
            label = html.unescape(entry["label"]).strip()

            retired = _RETIRED.search(display)
            retired_on = (dt.date(int(retired.group(3)), int(retired.group(2)),
                                  int(retired.group(1))) if retired else None)
            code = _ROW_CODE.match(display)

            if retired_on is None:
                position += 1
            name, formula, footnote, _ = strip_decorations(label)
            # One label's numbering is stale; the registry says which rows its
            # arithmetic really addresses. The published text stays in the name.
            override = WEEKLY_FORMULA_OVERRIDES.get((table.slug, int(entry["item_id"])))
            if override is not None:
                if not formula:
                    raise ValueError(
                        f"weekly formula override for {table.slug}/{entry['item_id']} but the "
                        f"label {label!r} states no formula; the override is stale.")
                formula = override
            records.append({
                "source": "BDDK",
                "dataset": table.slug,
                "table_id": table.table_id,
                "item_id": int(entry["item_id"]),
                "row_position": None if retired_on else position,
                "entity_key": str(entry["item_id"]),
                "entity_name": label,
                "entity_type": ENTITY_TYPE[table.table_id],
                "formula": formula or None,
                "footnote": footnote or None,
                "row_code": code.group(1) if code else None,
                "retired_on": retired_on,
                # Read off the label, so it needs no curation and cannot go stale.
                "is_informational": bool(_INFORMATIONAL.search(label)),
                "canonical_key": canonical_key(label),
            })

    items = pd.DataFrame.from_records(records)
    duplicates = items[items.item_id.duplicated()]
    if not duplicates.empty:
        raise ValueError(f"weekly item ids repeat across tables: {duplicates.item_id.tolist()}")
    return _attach_parents(items)


def _attach_parents(items: pd.DataFrame) -> pd.DataFrame:
    """Derive `parent_key` from the additive formulas the labels publish.

    An item saying '(4+5+6)' is the parent of rows 4, 5 and 6 of its own table.
    Only additive formulas qualify: '(2-3)' is a net position and its operands
    are not its children. A row claimed by two parents is a contradiction in the
    published labels, so it raises rather than picking one.
    """
    items = items.copy()
    parents: Dict[int, str] = {}
    for (_, table_id), group in items.groupby(["dataset", "table_id"]):
        active = group[group.retired_on.isna()]
        by_position = dict(zip(active.row_position, active.entity_key))
        for row in active.itertuples():
            # pandas' string dtype represents a missing formula as float NaN,
            # not None -- and NaN is truthy in Python, so `not row.formula`
            # alone lets it through into the regex and crashes.
            if not isinstance(row.formula, str) or not _ADDITIVE.match(row.formula):
                continue
            for position in (int(p) for p in re.findall(r"\d+", row.formula)):
                child = by_position.get(position)
                if child is None:
                    continue
                if child in parents and parents[child] != row.entity_key:
                    raise ValueError(
                        f"weekly table {table_id}: item {child} is claimed by both "
                        f"{parents[child]} and {row.entity_key}"
                    )
                parents[child] = row.entity_key
    items["parent_key"] = items.entity_key.map(parents)
    # A parent must not also be its own child; the formulas are a tree.
    self_parented = items[items.parent_key == items.entity_key]
    if not self_parented.empty:
        raise ValueError(f"weekly items parented to themselves: {self_parented.entity_key.tolist()}")
    return items


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

def parse_weekly_table(raw_dir: Path, items: pd.DataFrame, slug: str) -> pd.DataFrame:
    """One archived table -> long observations, one row per item/date/currency."""
    table = BY_SLUG[slug]
    path = raw_dir / f"{table.table_id}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No archived weekly report at {path}. The archive is committed; if it is "
            "missing, run: python -m backend.ingestion.bddk_weekly --fetch"
        )
    envelope = json.loads(path.read_text(encoding="utf-8"))
    label = f"weekly table {table.table_id} ({slug})"

    requested = [int(i) for i in envelope["request"]["items"]]
    rows = [_cells(r) for r in _TR_ROW.findall(envelope["table_html"])]
    rows = [r for r in rows if r]

    header = next((r for r in rows if r and _STATED_UNIT.search(r[0] or "")), None)
    if header is None:
        raise ValueError(f"{label}: the report states no unit; layout changed")
    unit = stated_unit(header[0])
    if unit != UNIT:
        raise ValueError(
            f"{label}: report states unit {unit!r} but the registry declares {UNIT!r}. "
            "Every figure in this table would be off by a factor; update "
            "domain.weekly_tables rather than the parser."
        )

    # One label per item, then two blank cells for the other two currencies.
    labels = header[1::3]
    if len(labels) != len(requested):
        raise ValueError(
            f"{label}: the report returned {len(labels)} column group(s) for "
            f"{len(requested)} requested item(s); column order cannot be trusted."
        )

    currencies = next((r for r in rows if [c.upper() for c in r[1:4]] == ["TP", "YP", "TOPLAM"]), None)
    if currencies is None:
        raise ValueError(f"{label}: no TP/YP/TOPLAM header row; layout changed")
    expected = [c.upper() for c in currencies[1:1 + 3 * len(requested)]]
    if expected != ["TP", "YP", "TOPLAM"] * len(requested):
        raise ValueError(f"{label}: currency columns are not a clean TP/YP/TOPLAM triple per item")

    meta = items[items.dataset == slug].set_index("item_id")
    missing = [i for i in requested if i not in meta.index]
    if missing:
        raise ValueError(f"{label}: report carries items absent from the catalogue: {missing}")

    data = [r for r in rows if _DATE.match(r[0] or "")]
    if not data:
        raise ValueError(f"{label}: the report carries no dated rows")

    records = []
    for row in data:
        period = parse_date(row[0])
        for index, item_id in enumerate(requested):
            info = meta.loc[item_id]
            for offset, currency in enumerate(("TP", "YP", "TOPLAM")):
                cell = row[1 + index * 3 + offset] if 1 + index * 3 + offset < len(row) else ""
                value = parse_number(cell)
                if value is None:
                    continue
                records.append({
                    "period": period,
                    "source": "BDDK",
                    "dataset": slug,
                    "entity_type": info.entity_type,
                    "entity_key": info.entity_key,
                    "entity_name": info.entity_name,
                    "parent_key": info.parent_key,
                    "row_position": info.row_position,
                    "metric": "balance",
                    "currency": CURRENCY_KEYS[currency],
                    "value": value,
                    "unit": unit,
                    "formula": info.formula,
                    "footnote": info.footnote,
                    "is_informational": bool(info.is_informational),
                    "retired_on": info.retired_on,
                    "temporal_semantics": table.semantics,
                })

    frame = pd.DataFrame.from_records(records)
    frame["period"] = pd.to_datetime(frame.period)
    frame["retired_on"] = pd.to_datetime(frame.retired_on)
    duplicated = frame.duplicated(["period", "entity_key", "currency"])
    if duplicated.any():
        raise ValueError(f"{label}: {int(duplicated.sum())} duplicated (period, item, currency) rows")
    return frame.sort_values(["period", "row_position", "entity_key", "currency"]).reset_index(drop=True)


def parse_weekly_archive(raw_dir: Path):
    """Every archived weekly table -> (observations, items).

    Returned as a pair rather than hung off `DataFrame.attrs`, because attrs
    travel into Parquet as JSON metadata and a frame does not serialise there.
    """
    items = parse_item_catalogue(raw_dir / "_catalog" / "kalemler.json")
    frames = [parse_weekly_table(raw_dir, items, table.slug) for table in BY_SLUG.values()]
    return pd.concat(frames, ignore_index=True), items
