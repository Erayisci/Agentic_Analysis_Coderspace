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

# The response states its own unit in the caption: 'Bilanço (milyon TL),
# Dönem:2026/6'. Fifteen of the seventeen tables are milyon TL and only
# `05_sektorel_kredi_dagilimi` is bin TL, so this is never assumed from the
# corpus-wide default -- it is read per response and checked against the
# registry, so a rebasing by BDDK fails the build instead of rescaling every
# figure by 1000 in silence.
CAPTION_UNIT = re.compile(r"\((\s*(?:bin|milyon|milyar)\s+TL\s*)\)", re.IGNORECASE)

# Columns the endpoint returns for bookkeeping rather than measurement.
META_FIELDS = ("BankaAdi", "BasitSira", "Ad", "BasitFont")

# A measure column name ending in one of these encodes the currency of the
# figure; 'NakdiKrediToplam' is the total-currency variant of 'NakdiKredi'.
CURRENCY_SUFFIXES = (("Toplam", "total"), ("Tp", "TL"), ("Yp", "FX"))

# Measure name used when a column is nothing but a currency marker, i.e. the
# table has a single measure split three ways (Tp / Yp / Toplam).
DEFAULT_METRIC = "balance"

# The column set of a table that carries one measure split by currency only.
SIMPLE_MEASURES = {"Tp", "Yp", "Toplam"}


def _split_camel(field: str) -> str:
    """'KisaVadeliNakdi' -> 'Kisa Vadeli Nakdi', so the slug keeps word boundaries."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", field)


def split_measure(field: str, columns=None) -> tuple:
    """Split a measure column name into (metric, currency).

    'Toplam' is the column name that needs the table's other columns to read
    correctly, which is why `columns` exists:

        table 01  {Tp, Yp, Toplam}         'Toplam' -> ('balance', 'total')
                  one measure split by currency, so the name carries no measure
        table 03  {KisaTp, ..., Toplam}    'Toplam' -> ('toplam', 'total')
                  'Toplam' is the total-maturity measure AND the total currency
        table 09  {OnBin, ..., Toplam}     'Toplam' -> ('toplam', None)
                  no currency split at all; 'Toplam' means all size buckets

    Reading the third case as currency='total' -- which is what a suffix rule
    alone does -- tells the agent a deposit-size total is a currency total.

    With `columns` omitted the currency suffix is peeled, which is the right
    reading for every column name that carries a measure of its own.
    """
    if columns is None:
        columns = {field, "Tp", "Yp"}
    columns = set(columns)
    if columns <= SIMPLE_MEASURES:
        for suffix, currency in CURRENCY_SUFFIXES:
            if field == suffix:
                return DEFAULT_METRIC, currency
        return slugify(_split_camel(field)), None

    # Only peel a currency suffix when the table actually splits by currency.
    if any(name.endswith(("Tp", "Yp")) for name in columns):
        for suffix, currency in CURRENCY_SUFFIXES:
            if field == suffix:
                return suffix.lower(), currency
            if field.endswith(suffix):
                stem = field[: -len(suffix)]
                if stem:
                    return slugify(_split_camel(stem)), currency
                return DEFAULT_METRIC, currency
    return slugify(_split_camel(field)), None


def caption_unit(caption: str):
    """The monetary unit the response states about itself, or None.

    Tables 15, 16 and 17 state none, because they publish ratios and counters
    rather than money; those declare their unit in the registry or per row.
    """
    match = CAPTION_UNIT.search(caption or "")
    if not match:
        return None
    return " ".join(match.group(1).split()).lower().replace(" tl", " TL")


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
    stated_unit = caption_unit(block.get("caption", ""))
    if stated_unit and table.unit and stated_unit != table.unit:
        raise ValueError(
            f"{label}: response states unit {stated_unit!r} but the registry declares "
            f"{table.unit!r}. Every figure in this table would be off by a factor; "
            f"update domain.bulletin_tables rather than the parser."
        )
    table_unit = stated_unit or table.unit
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
    measure_columns = [name for name in names if name not in META_FIELDS]
    measures = [
        (i, *split_measure(names[i], measure_columns))
        for i in range(len(names)) if names[i] not in META_FIELDS
    ]
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

    for position, ((raw_label, _font, cell), row_parent) in enumerate(zip(parsed, parents), start=1):
        name, formula, footnote, row_unit = strip_decorations(raw_label)
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
                    # The row's own 1-based position in THIS period. Never part
                    # of the key -- three tables reshuffle -- but the identity
                    # formulas address rows by position, so validation needs it.
                    "row_position": position,
                    "metric": metric,
                    "currency": currency,
                    "value": value,
                    "unit": row_unit or table_unit,
                    "temporal_semantics": table.semantics,
                    "formula": formula or None,
                    "footnote": footnote or None,
                }
            )

    if not records:
        raise ValueError(f"{label}: parsed no observations")
    return pd.DataFrame.from_records(records)


_TAG = re.compile(r"<[^>]+>")


def clean_footnote(text: str) -> str:
    """The `uyari` field is HTML; flatten it to one line of plain text."""
    return " ".join(_TAG.sub(" ", text or "").split())


def parse_bulletin_footnotes(raw_root: Path) -> pd.DataFrame:
    """The methodology notes each table publishes about itself (`Json.uyari`).

    These are not decoration. Several of them state that a published row is
    excluded from its own table's totals -- table 5's 'Bankalara Kullandırılan
    Krediler' and table 8's repo securities are both marked 'bilgi amaçlı ...
    hesaplamalara dahil edilmemiştir' -- and table 6 warns that one customer
    using several loan types is counted once in the total column, so its counts
    are not additive across rows. An agent that sums those rows is wrong in a
    way no arithmetic check can catch, because the published totals are
    self-consistent without them.

    The text is period-dependent: table 12's note changed at 2021-11, the same
    month its capital-adequacy formula changed, and table 3's at 2022-01, the
    month that table restructured. One row per distinct text with the span it
    covers, so the agent reads a handful of rows rather than 67 copies.
    """
    records = []
    for table in sorted(BY_SLUG.values(), key=lambda t: t.number):
        directory = raw_root / f"{table.number:02d}"
        if not directory.is_dir():
            continue
        spans = {}
        for path in sorted(directory.glob("*.json")):
            match = PERIOD_PATTERN.search(path.name)
            if not match:
                raise ValueError(f"Cannot extract period from filename: {path.name}")
            period = f"{match.group(1)}-{match.group(2)}-01"
            text = clean_footnote(
                (json.loads(path.read_text(encoding="utf-8")).get("Json") or {}).get("uyari")
            )
            if not text:
                continue
            if text in spans:
                spans[text][1] = period
                spans[text][2] += 1
            else:
                spans[text] = [period, period, 1]
        for text, (first, last, count) in spans.items():
            records.append({
                "source": "BDDK",
                "dataset": table.slug,
                "first_period": first,
                "last_period": last,
                "n_periods": count,
                "footnote": text,
            })

    frame = pd.DataFrame.from_records(
        records,
        columns=["source", "dataset", "first_period", "last_period", "n_periods", "footnote"],
    )
    for column in ("first_period", "last_period"):
        frame[column] = pd.to_datetime(frame[column]).dt.date
    return frame


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
