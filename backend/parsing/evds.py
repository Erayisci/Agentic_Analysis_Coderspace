"""Parser for the archived TCMB EVDS responses.

Reads `evds/_raw_json/` (see `backend.ingestion.evds` for the layout) and
emits two frames:

    catalogue   one row per series with the metadata TCMB publishes
                (name, unit, native frequency, hierarchy) joined to the
                registry's declarations (tier, semantics, monthly rule)
    native      long observations at each series' own frequency:
                date, series_code, value

Response quirks the parser absorbs, all verified against live calls:
    - numbers arrive as strings, missing values as null
    - `Tarih` is 'dd-mm-yyyy' for daily/weekly series, 'yyyy-m' (no zero
      padding) for monthly and 'yyyy-Qn' for quarterly
    - a series column is simply absent when nothing was returned for it,
      which is why the envelope records which series were requested
    - a weekly request for a calendar year returns the Friday that falls in
      the first week of the next year as well, so consecutive year files
      overlap by one observation; overlaps must agree
"""
import datetime as dt
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

from ..core.labels import TURKISH_TO_ASCII
from ..domain.evds_series import BY_CODE, DERIVED_SERIES, GROUPS, DataGroup

FREQUENCY_LABELS = {
    "GÜNLÜK": "daily",
    "İŞ GÜNÜ": "business_daily",
    "HAFTALIK(CUMA)": "weekly",
    "HAFTALIK(ÇARŞAMBA)": "weekly",
    "AYDA İKİ KEZ": "semimonthly",
    "AYLIK": "monthly",
    "ÜÇ AYLIK": "quarterly",
    "ALTI AYLIK": "semiannual",
    "YILLIK": "annual",
}

_DAILY = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")
_MONTHLY = re.compile(r"^(\d{4})-(\d{1,2})$")
_QUARTERLY = re.compile(r"^(\d{4})-Q([1-4])$")


def parse_tarih(text: str) -> Tuple[dt.date, str]:
    """EVDS date label -> (date, grain). Monthly labels map to the first of the
    month, quarterly to the first day of the quarter's LAST month, so that a
    quarterly value sits on the month it is published for."""
    match = _DAILY.match(text)
    if match:
        day, month, year = (int(g) for g in match.groups())
        return dt.date(year, month, day), "day"
    match = _MONTHLY.match(text)
    if match:
        return dt.date(int(match.group(1)), int(match.group(2)), 1), "month"
    match = _QUARTERLY.match(text)
    if match:
        return dt.date(int(match.group(1)), 3 * int(match.group(2)), 1), "quarter"
    raise ValueError(f"unrecognised EVDS date label {text!r}")


def _column_name(series_code: str) -> str:
    return series_code.replace(".", "_")


def _series_declaration(group: DataGroup, code: str) -> Tuple[str, str]:
    if group.overrides and code in group.overrides:
        return group.overrides[code]
    return group.semantics, group.monthly_rule


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
# TCMB states a unit per DATA GROUP (`BIRIMI`), never per series, and the field
# is free text. Measured across the 44 registered groups it breaks in three
# ways, so it cannot be copied into the catalogue as-is:
#
#   compound    'milyon TL ve yüzde' covers eleven TCMB funding amounts AND the
#               funding cost, which is a percentage; 'TL/kg, USD/ons, Euro/ons,
#               TL/gr' covers four differently quoted gold series.
#   not a unit  'Ağırlıklı ortalama' (92 interest-rate series) names a method,
#               'İşlem' names the object counted, and one KFE group carries a
#               province list that leaked into the field.
#   spelling    '%' / 'Yüzde' / '% Değişim', and 'endeks' / 'Endeks' /
#               '2003=100' / 'Harcama Yöntemiyle, Zincirlenmiş Endeks'.
#
# So the published string is kept verbatim in `unit_source` and `unit` is
# resolved per series -- the same discipline the bulletin parser applies to
# `Json.caption`: read what the source states, then resolve it for the row
# rather than stamping one label across a whole group.
#
# Matching runs on the ASCII fold (`core.labels.TURKISH_TO_ASCII`) because
# Python's re.IGNORECASE does not equate 'İ' with 'i': 'İşlem' would otherwise
# never match. Patterns are ordered most specific first and every match is
# collected, so a compound string yields every unit it names.
UNIT_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"milyar\s+abd\s+dolar"), "milyar ABD doları"),
    (re.compile(r"milyon\s+abd\s+dolar"), "milyon ABD doları"),
    (re.compile(r"milyar\s+tl"), "milyar TL"),
    (re.compile(r"milyon\s+tl"), "milyon TL"),
    (re.compile(r"bin\s+tl"), "bin TL"),
    (re.compile(r"turk\s+lirasi"), "TL"),
    (re.compile(r"tl\s*/\s*kg"), "TL/kg"),
    (re.compile(r"tl\s*/\s*gr"), "TL/gr"),
    (re.compile(r"usd\s*/\s*ons"), "USD/ons"),
    (re.compile(r"euro\s*/\s*ons"), "Euro/ons"),
    (re.compile(r"metrekare|m2"), "m2"),
    # The building-permit group states 'Adet,TL/m2' for four series of which one
    # is a TL amount; that series says so in its own name, '... (Değer(TL))'.
    (re.compile(r"deger\s*\(\s*tl"), "TL"),
    (re.compile(r"bin\s+kisi"), "bin kişi"),
    (re.compile(r"%|yuzde"), "%"),
    (re.compile(r"endeks|=\s*100"), "endeks"),
    # 'İşlem' is anchored: as a whole BIRIMI it is the unit of a card-transaction
    # count, but inside a series name ('Altın - İşlem Hacmi - TL/kg') it is not.
    (re.compile(r"\badet\b|\bsayisi\b|^islem$"), "adet"),
)

CANONICAL_UNITS = tuple(dict.fromkeys(unit for _, unit in UNIT_PATTERNS))


def _fold(text: str) -> str:
    return str(text or "").translate(TURKISH_TO_ASCII).lower().strip()


def scan_units(text: str) -> Tuple[str, ...]:
    """Every canonical unit the string names, in vocabulary order."""
    folded = _fold(text)
    if not folded:
        return ()
    found = [unit for pattern, unit in UNIT_PATTERNS if pattern.search(folded)]
    return tuple(dict.fromkeys(found))


def _disambiguate(candidates: Tuple[str, ...], semantics: str) -> Tuple[str, ...]:
    """Narrow a compound unit string using what the series declares it is."""
    if semantics == "rate" and "%" in candidates:
        return ("%",)
    if semantics == "index" and "endeks" in candidates:
        return ("endeks",)
    # A level or a flow is never the percentage half of a compound string.
    remainder = tuple(u for u in candidates if u not in ("%", "endeks"))
    return remainder if len(remainder) == 1 else candidates


def resolve_unit(name: str, published_unit, semantics: str, overridden: bool):
    """The unit of one series, or None when nothing in the source states it.

    Resolution order, each step reading the source rather than a hand-typed
    table:

    1. the group's `BIRIMI`, when it names exactly one unit -- 30 of the 44
       groups are homogeneous and this is the whole answer for them;
    2. the series' own name, when the group names none or several. The gold
       and building-permit groups both put the real unit in the series name;
    3. the declared semantics, for the groups whose `BIRIMI` states a method
       rather than a unit: `rate` is a percentage, `index` an index number.

    A series the registry OVERRIDES is excluded from step 1 by construction:
    an override says this series does not behave like its group, and where the
    behaviour differs the group's unit does not describe it either. That is
    what separates TCMB's funding cost (%) from the funding amounts it is
    published beside (milyon TL), and the three labour-force ratios from the
    headcounts in their group.
    """
    published = () if overridden else scan_units(published_unit)
    candidates = published
    if len(candidates) != 1:
        candidates = scan_units(name) or candidates
    if len(candidates) > 1:
        candidates = _disambiguate(candidates, semantics)
    if len(candidates) == 1:
        return candidates[0]
    if semantics == "index":
        return "endeks"
    if semantics == "rate":
        return "%"
    return None


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

def load_catalogue(catalog_dir: Path) -> pd.DataFrame:
    """Series metadata for every registered group, from the archived serieLists."""
    categories_path = catalog_dir / "categories.json"
    if not categories_path.exists():
        raise FileNotFoundError(
            f"No EVDS catalogue at {catalog_dir}. The archive is committed; if it is missing, "
            "run: python -m backend.ingestion.evds --catalog --fetch"
        )
    categories = json.loads(categories_path.read_text(encoding="utf-8"))
    group_meta: Dict[str, dict] = {}
    for category in categories:
        for entry in category.get("DATAGROUPS") or []:
            group_meta[entry["DATAGROUP_CODE"]] = {
                "datagroup_name_tr": entry["DATAGROUP_TYPE"].strip(),
                "datagroup_name_en": (entry.get("DATAGROUP_TYPE_ENG") or "").strip(),
                "category_tr": category["TOPIC_TITLE_TR"].strip(),
                "unit": (entry.get("BIRIMI") or "").strip() or None,
                "unit_en": (entry.get("BIRIMI_EN") or "").strip() or None,
                "datasource": (entry.get("DATASOURCE") or "").strip() or None,
            }

    from ..ingestion.evds import select_series  # registry narrowing lives with the downloader

    records: List[dict] = []
    for group in GROUPS:
        path = catalog_dir / "serielist" / f"{group.code}.json"
        if not path.exists():
            raise FileNotFoundError(f"No archived serieList for {group.code} at {path}")
        serielist = json.loads(path.read_text(encoding="utf-8"))
        selected = set(select_series(group, serielist))
        meta = group_meta.get(group.code, {})
        for entry in serielist:
            code = entry["SERIE_CODE"]
            if code not in selected:
                continue
            semantics, rule = _series_declaration(group, code)
            published_unit = meta.get("unit")
            overridden = bool(group.overrides and code in group.overrides)
            records.append(
                {
                    "series_code": code,
                    "datagroup": group.code,
                    "tier": group.tier,
                    "name_tr": entry["SERIE_NAME"].strip(),
                    "name_en": (entry.get("SERIE_NAME_ENG") or "").strip() or None,
                    "datagroup_name_tr": meta.get("datagroup_name_tr"),
                    "category_tr": meta.get("category_tr"),
                    "unit": resolve_unit(entry["SERIE_NAME"], published_unit, semantics, overridden),
                    "unit_source": published_unit,
                    "datasource": entry.get("DATASOURCE") or meta.get("datasource"),
                    "native_frequency": FREQUENCY_LABELS.get(entry["FREQUENCY_STR"], entry["FREQUENCY_STR"]),
                    "temporal_semantics": semantics,
                    "monthly_rule": rule,
                    "level": entry.get("SEVIYE"),
                    "parent_series": entry.get("UST_SERIE_CODE") if entry.get("UST_SERIE_CODE") not in (None, "-1") else None,
                    "published_start": entry.get("START_DATE"),
                    "published_end": entry.get("END_DATE"),
                    "derived": False,
                }
            )

    for code, source_group, semantics, name_tr, name_en in DERIVED_SERIES:
        base = next(r for r in records if r["datagroup"] == source_group)
        records.append(
            {
                **{k: None for k in records[0]},
                "series_code": code,
                "datagroup": source_group,
                "tier": BY_CODE[source_group].tier,
                "name_tr": name_tr,
                "name_en": name_en,
                "datagroup_name_tr": base["datagroup_name_tr"],
                "category_tr": base["category_tr"],
                "unit": "%",
                "datasource": "derived",
                "native_frequency": base["native_frequency"],
                "temporal_semantics": semantics,
                "monthly_rule": "avg",
                "level": 1,
                "derived": True,
            }
        )

    catalogue = pd.DataFrame.from_records(records)
    duplicates = catalogue[catalogue.series_code.duplicated()]
    if not duplicates.empty:
        raise ValueError(f"series listed under several groups: {duplicates.series_code.tolist()}")
    return catalogue


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

def parse_envelope(envelope: dict) -> pd.DataFrame:
    """One archived response -> long frame (date, series_code, value, grain)."""
    label = f"{envelope['datagroup']} {envelope['startDate']}..{envelope['endDate']}"
    response = envelope["response"]
    items = response.get("items")
    if items is None:
        raise ValueError(f"{label}: response carries no items: {str(response)[:200]}")

    requested = envelope["series"]
    columns = {code: _column_name(code) for code in requested}
    records = []
    for item in items:
        date, grain = parse_tarih(item["Tarih"])
        for code, column in columns.items():
            raw = item.get(column)
            if raw in (None, ""):
                continue
            records.append((date, code, float(raw), grain))
    return pd.DataFrame.from_records(records, columns=["date", "series_code", "value", "grain"])


def parse_group(raw_root: Path, group: DataGroup) -> pd.DataFrame:
    directory = raw_root / group.code
    files = sorted(directory.glob("*.json"))
    if not files:
        raise FileNotFoundError(
            f"No archived EVDS data for {group.code} in {directory}. "
            "Run: python -m backend.ingestion.evds --fetch"
        )
    frames = [parse_envelope(json.loads(p.read_text(encoding="utf-8"))) for p in files]
    combined = pd.concat(frames, ignore_index=True)
    if combined.empty:
        raise ValueError(f"{group.code}: archive parsed to zero observations")

    # Year files overlap by one weekly observation; the overlap must agree.
    key = ["series_code", "date"]
    conflicting = combined.groupby(key)["value"].nunique()
    conflicting = conflicting[conflicting > 1]
    if not conflicting.empty:
        raise ValueError(f"{group.code}: {len(conflicting)} (series, date) pairs archived with different values")
    combined = combined.drop_duplicates(key).reset_index(drop=True)
    combined.insert(1, "datagroup", group.code)
    return combined


def parse_archive(raw_root: Path) -> pd.DataFrame:
    """Every registered group -> one native-frequency long frame."""
    frames = [parse_group(raw_root, group) for group in GROUPS]
    native = pd.concat(frames, ignore_index=True)
    native = native.sort_values(["datagroup", "series_code", "date"]).reset_index(drop=True)
    return native
