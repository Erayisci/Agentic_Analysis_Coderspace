"""Generic parser for the BDDK monthly-bulletin tables.

Reads the archived endpoint responses under `bddk_aylik_bulten/_raw_json/`
rather than the Excel rendering, because the response carries two things the
workbook layout drops and the parser cannot work without:

    colModels   column names and labels, so measure columns are identified by
                name instead of position
    BasitFont   'bold' for a parent row, 'italic' for a child of the preceding
                bold row -- the only signal that separates the six different
                'a) Gerçek Kişiler' rows in the deposit tables

Output is long format, one row per (period, entity, metric, currency), with the
entity keyed on its normalised label (see `labels.py`) and qualified by its
parent. Row position is never an identifier: three of the seventeen tables
reshuffle their rows mid-history.
"""
import json
import re
from pathlib import Path

import pandas as pd

from ..domain.bulletin_tables import BY_SLUG, ENTITY_TYPE, Table
from ..core.labels import canonical_key, qualified_key, slugify, strip_decorations

PERIOD_PATTERN = re.compile(r"(\d{4})_(\d{2})\.json$")

# Columns the endpoint returns for bookkeeping rather than measurement.
META_FIELDS = ("BankaAdi", "BasitSira", "Ad", "BasitFont")

# A measure column name ending in one of these encodes the currency of the
# figure; 'NakdiKrediToplam' is the total-currency variant of 'NakdiKredi'.
CURRENCY_SUFFIXES = (("Toplam", "total"), ("Tp", "TL"), ("Yp", "FX"))

# Measure name used when a column is nothing but a currency marker, i.e. the
# table has a single measure split three ways (Tp / Yp / Toplam).
DEFAULT_METRIC = "balance"


def _split_camel(field: str) -> str:
    """'KisaVadeliNakdi' -> 'Kisa Vadeli Nakdi', so the slug keeps word boundaries."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", field)


def split_measure(field: str) -> tuple:
    """'KisaTp' -> ('kisa', 'TL');  'Toplam' -> ('balance', 'total');  'Rasyo' -> ('rasyo', None)."""
    for suffix, currency in CURRENCY_SUFFIXES:
        if field == suffix:
            return DEFAULT_METRIC, currency
        if field.endswith(suffix):
            stem = field[: -len(suffix)]
            if stem:
                return slugify(_split_camel(stem)), currency
            return DEFAULT_METRIC, currency
    return slugify(_split_camel(field)), None


def _coerce(value):
    """Endpoint numbers arrive as strings; ratios are decimal, stocks integral."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        try:
            return float(text.replace(".", "").replace(",", "."))
        except ValueError:
            return None


def cell_of(row: dict) -> list:
    return row["cell"]


def assign_parents(parsed: list, table: Table, label: str) -> list:
    """Return the parent key of each row, or None for a top-level row.

    See `Table.hierarchy` for why the two layouts exist and which tables use
    which. A row is only ever qualified by its parent when the table actually
    repeats labels, but the rule is applied uniformly so the parent link is
    available as a dimension either way.
    """
    keys = [canonical_key(raw) for raw, _font, _cell in parsed]
    fonts = [font for _raw, font, _cell in parsed]

    if table.hierarchy == "leading":
        parents = []
        current = None
        for key, font in zip(keys, fonts):
            if font == "italic":
                if current is None:
                    raise ValueError(f"{label}: child row {key!r} before any parent")
                parents.append(current)
            else:
                current = key
                parents.append(None)
        return parents

    if table.hierarchy == "trailing":
        parents = [None] * len(keys)
        pending = []
        for index, (key, font) in enumerate(zip(keys, fonts)):
            if font == "bold":
                for waiting in pending:
                    parents[waiting] = key
                pending = []
            else:
                pending.append(index)
        if pending:
            raise ValueError(
                f"{label}: {len(pending)} row(s) after the last bold section total"
            )
        return parents

    raise ValueError(f"{label}: unknown hierarchy strategy {table.hierarchy!r}")


def parse_bulletin_response(response: dict, table: Table, period: str) -> pd.DataFrame:
    """Parse one month of one bulletin table into long format."""
    label = f"{table.slug} {period}"

    if not response.get("success"):
        raise ValueError(f"{label}: endpoint reported success=false")

    block = response.get("Json") or {}
    col_models = block.get("colModels")
    rows = (block.get("data") or {}).get("rows")
    if not col_models or not rows:
        raise ValueError(f"{label}: response carries no table data")

    names = [c["name"] for c in col_models]
    for required in ("BasitSira", "Ad", "BasitFont"):
        if required not in names:
            raise ValueError(f"{label}: unexpected layout, {required!r} missing from {names}")

    code_index = names.index("BasitSira")
    name_index = names.index("Ad")
    font_index = names.index("BasitFont")
    measures = [(i, *split_measure(names[i])) for i in range(len(names)) if names[i] not in META_FIELDS]
    if not measures:
        raise ValueError(f"{label}: no measure columns in {names}")

    entity_type = ENTITY_TYPE[table.number]
    parsed = [
        (
            str(cell_of(row)[name_index]).strip(),
            str(cell_of(row)[font_index]).strip().lower(),
            cell_of(row),
        )
        for row in rows
    ]
    parents = assign_parents(parsed, table, label)

    records = []
    seen_keys = set()

    for (raw_label, _font, cell), row_parent in zip(parsed, parents):
        name, formula, footnote = strip_decorations(raw_label)
        own_key = canonical_key(raw_label)
        if not own_key:
            raise ValueError(f"{label}: row {cell[code_index]} has an empty label")

        entity_key = qualified_key(row_parent, own_key)
        if entity_key in seen_keys:
            raise ValueError(f"{label}: duplicate entity key {entity_key!r}")
        seen_keys.add(entity_key)

        for index, metric, currency in measures:
            value = _coerce(cell[index])
            if value is None:
                continue
            records.append(
                {
                    "period": period,
                    "source": "BDDK",
                    "dataset": table.slug,
                    "entity_type": entity_type,
                    "entity_key": entity_key,
                    "entity_name": name,
                    "parent_key": row_parent,
                    "metric": metric,
                    "currency": currency,
                    "value": value,
                    "unit": table.unit,
                    "formula": formula or None,
                    "footnote": footnote or None,
                }
            )

    if not records:
        raise ValueError(f"{label}: parsed no observations")
    return pd.DataFrame.from_records(records)


def parse_bulletin_table(raw_root: Path, table_slug: str) -> pd.DataFrame:
    """Parse every archived month of one bulletin table."""
    table = BY_SLUG[table_slug]
    directory = raw_root / f"{table.number:02d}"
    files = sorted(directory.glob("*.json"))
    if not files:
        raise FileNotFoundError(f"No archived responses in {directory}")

    frames = []
    for path in files:
        match = PERIOD_PATTERN.search(path.name)
        if not match:
            raise ValueError(f"Cannot extract period from filename: {path.name}")
        period = f"{match.group(1)}-{match.group(2)}-01"
        response = json.loads(path.read_text(encoding="utf-8"))
        frames.append(parse_bulletin_response(response, table, period))

    combined = pd.concat(frames, ignore_index=True)
    # Match the date type the rest of the pipeline uses, so the bulletin tables
    # join against `observations` without a cast.
    combined["period"] = pd.to_datetime(combined["period"]).dt.date
    return combined
