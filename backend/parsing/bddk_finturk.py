"""Parser for the archived BDDK FinTurk (il-bazli) responses.

Reads `bddk_finturk/_raw_json/<NN>_<slug>/<donem>.json` (see
`backend.ingestion.bddk_finturk` for the fetch protocol) and emits one long
frame: `period, source, dataset, province, taraf_code, taraf_name, metric,
metric_name, value, unit`.

Unlike `parsing.bddk_bulletin`, every response cell already arrives as a JSON
number (int for money/counts, float for ratios) rather than a string BDDK
expects the caller to parse -- measured across all seven tables, `Json.data`
here carries no `Json.uyari` methodology notes either, so there is no footnote
column to carry. And unlike the monthly and weekly bulletins, no column label
states an arithmetic formula, so there is no `formula`/`parent_key` here: see
`domain.finturk_tables` for why that is a fact about the source, not a gap in
this parser.

`metric` is `core.labels.slugify(metric_name)` -- the same ASCII-safe,
Turkish-aware key the bulletin and weekly paths use, so a search across all
three corpora's metric vocabularies behaves the same way (case-safe `ILIKE` on
the key, not the display name).
"""
import json
from pathlib import Path
from typing import List, Optional

import pandas as pd

from ..core.labels import slugify
from ..domain.finturk_tables import BY_SLUG, METRIC_UNIT_OVERRIDES, TABLES, FinturkTable

_DIMENSION_FIELDS = ("EftKodu", "Yil", "Ay", "Sehir", "Grup")


def _coerce(value):
    """Cells already arrive as JSON numbers; only `null` needs handling."""
    return None if value is None else value


def parse_finturk_response(response: dict, table: FinturkTable, donem: str) -> pd.DataFrame:
    """One archived (table, quarter) response -> long observations."""
    label = f"{table.slug} {donem}"

    if not response.get("success"):
        raise ValueError(f"{label}: endpoint reported success=false ({response.get('error')})")

    block = response.get("Json") or {}
    col_names = block.get("colNames")
    col_models = block.get("colModels")
    rows = (block.get("data") or {}).get("rows")
    if not col_names or not col_models or not rows:
        raise ValueError(f"{label}: response carries no table data (quarter not published?)")
    if len(col_names) != len(col_models):
        raise ValueError(f"{label}: colNames and colModels disagree in length")

    field_names = [c["name"] for c in col_models]
    missing = [f for f in _DIMENSION_FIELDS if f not in field_names]
    if missing:
        raise ValueError(f"{label}: unexpected layout, {missing} missing from {field_names}")

    dim_index = {f: field_names.index(f) for f in _DIMENSION_FIELDS}
    measure_indexes = [i for i, name in enumerate(field_names) if name not in _DIMENSION_FIELDS]
    if not measure_indexes:
        raise ValueError(f"{label}: no measure columns in {field_names}")

    expected_year, expected_month = int(donem.split("-")[0]), int(donem.split("-")[1])

    records = []
    for row in rows:
        cell = row["cell"]
        year, month = int(cell[dim_index["Yil"]]), int(cell[dim_index["Ay"]])
        if (year, month) != (expected_year, expected_month):
            raise ValueError(
                f"{label}: row states period {year}-{month:02d}, expected {donem} -- "
                "the archive may have been fetched for the wrong quarter."
            )
        province = str(cell[dim_index["Sehir"]]).strip()
        taraf_code = int(cell[dim_index["EftKodu"]])
        taraf_name = str(cell[dim_index["Grup"]]).strip()

        for i in measure_indexes:
            value = _coerce(cell[i])
            if value is None:
                continue
            metric_name = col_names[i]
            records.append({
                "period": f"{year:04d}-{month:02d}-01",
                "source": "finturk",
                "dataset": table.slug,
                "province": province,
                "taraf_code": taraf_code,
                "taraf_name": taraf_name,
                "metric": slugify(metric_name),
                "metric_name": metric_name,
                "value": value,
                "unit": METRIC_UNIT_OVERRIDES.get(metric_name, table.unit),
            })

    frame = pd.DataFrame.from_records(records)
    frame["period"] = pd.to_datetime(frame.period)
    duplicated = frame.duplicated(["period", "province", "taraf_code", "metric"])
    if duplicated.any():
        raise ValueError(f"{label}: {int(duplicated.sum())} duplicated (period, province, taraf, metric) rows")
    return frame


def parse_finturk_table(raw_dir: Path, slug: str) -> pd.DataFrame:
    """Every archived quarter for one table -> one long frame."""
    table = BY_SLUG[slug]
    directory = raw_dir / f"{table.number:02d}_{slug}"
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise FileNotFoundError(
            f"No archived FinTurk responses at {directory}. The archive is committed; "
            "if it is missing, run: python -m backend.ingestion.bddk_finturk --fetch"
        )

    frames: List[pd.DataFrame] = []
    for path in paths:
        donem = path.stem
        response = json.loads(path.read_text(encoding="utf-8"))
        frames.append(parse_finturk_response(response, table, donem))
    return pd.concat(frames, ignore_index=True)


def parse_finturk_archive(raw_dir: Path) -> pd.DataFrame:
    """Every table's every archived quarter -> one long observations frame."""
    frames = [parse_finturk_table(raw_dir, table.slug) for table in TABLES]
    combined = pd.concat(frames, ignore_index=True)
    return combined.sort_values(
        ["dataset", "period", "province", "taraf_code", "metric"]
    ).reset_index(drop=True)
