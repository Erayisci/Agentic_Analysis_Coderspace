"""Parse an Excel/CSV or supported PDF column into a monthly numeric series.

The executor adds the result to the working table. The API evidence callback
also persists its source metadata and typed observations in research.duckdb
before the column is used; the curated BDDK lakehouse stays independently built.

Fetching goes through `tools.web_url`'s `_fetch`/`_detect_kind` rather than a
second implementation, because the SSRF guard (reject private/loopback/
link-local addresses, validate every redirect hop) must not fork into two
copies that could silently drift apart -- this tool fetches an arbitrary,
untrusted URL exactly like the Web URL tool does.

Unlike `bulletin_observations` / `weekly_observations` / `macro_observations`,
an external file publishes no unit and no temporal-semantics label the way
the lakehouse's own sources do (see CLAUDE.md: "a number without its unit is
not an answer"). Both are best-effort here -- the raw column header if the
caller gives nothing better -- and are stated as such in the citation, so the
composer can hedge instead of asserting them as fact.
"""
import re
import hashlib
from io import BytesIO
from typing import NamedTuple, Optional

import pandas as pd
from pandas.api.types import is_bool_dtype, is_datetime64_any_dtype, is_numeric_dtype

from . import web_url
from .transforms import resample_to_monthly

MIN_DATE_PARSE_RATE = 0.9  # a period column must parse on (almost) every row
PLAUSIBLE_YEARS = (1900, 2100)  # anything outside is a mis-parse, not a date

# Turkish month names, as the regulators print them ("Ocak", "Şubat / February",
# "Mayıs 2026"); ASCII spellings included because Excel exports often drop the
# diacritics. Mapped onto English so pandas' parser can read them.
_TR_MONTHS = {
    "ocak": "January", "şubat": "February", "subat": "February", "mart": "March",
    "nisan": "April", "mayıs": "May", "mayis": "May", "haziran": "June", "temmuz": "July",
    "ağustos": "August", "agustos": "August", "eylül": "September", "eylul": "September",
    "ekim": "October", "kasım": "November", "kasim": "November", "aralık": "December",
    "aralik": "December",
}
_TR_MONTH_RE = re.compile("|".join(sorted(_TR_MONTHS, key=len, reverse=True)), re.IGNORECASE)
_BILINGUAL_SUFFIX_RE = re.compile(r"\s*/\s*[A-Za-z]+")  # "Ocak / January" -> "Ocak"

_TR_NUMBER_RE = re.compile(r"^-?\d{1,3}(\.\d{3})+(,\d+)?$|^-?\d+,\d+$")   # 93.824.682.381 / 12,5
_EN_NUMBER_RE = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")               # 93,824,682,381.5


def _parse_periods(raw: pd.Series) -> pd.Series:
    """Dates from a column, Turkish-aware: day-first (`03.02.2021` is 3 Feb),
    Turkish month names, and the bilingual `Ocak / January` labels the BIST
    and BDDK publications use. Unparseable cells become NaT."""
    if is_datetime64_any_dtype(raw):
        return pd.to_datetime(raw, errors="coerce")
    text = raw.astype("string").str.strip()
    text = text.str.replace(_BILINGUAL_SUFFIX_RE, "", regex=True)
    text = text.str.replace(_TR_MONTH_RE, lambda m: _TR_MONTHS[m.group(0).lower()], regex=True)
    # ISO exports are year-month-day regardless of the locale. With dayfirst
    # enabled, pandas can read 2021-02-01 as January 2 and collapse a whole
    # monthly CSV into January's last value during resampling.
    iso = text.str.match(r"^\d{4}-\d{2}-\d{2}(?:$|[T ])", na=False)
    parsed = pd.to_datetime(text.mask(iso), errors="coerce", dayfirst=True, format="mixed")
    parsed.loc[iso] = pd.to_datetime(text[iso], errors="coerce", format="ISO8601")
    return parsed


def _parse_numbers(raw: pd.Series) -> pd.Series:
    """Numbers from a column, with the separator convention decided per column.

    Turkish publications group thousands with `.` and mark decimals with `,`,
    so `pd.to_numeric` alone reads `1.017` as 1.017 (a thousandfold error, in
    silence) and drops `93.824.682.381` as unparseable. The convention is
    sniffed over the whole column -- one `93.824.682.381` settles it for the
    `1.017` beside it -- because a lone `1.017` is genuinely ambiguous and, on
    its own, stays a decimal.
    """
    if is_numeric_dtype(raw):
        return pd.to_numeric(raw, errors="coerce")
    text = raw.astype("string").str.strip().str.replace("\u00a0", "", regex=False) \
        .str.replace(" ", "", regex=False).str.rstrip("%")
    values = text.dropna()
    turkish = values.str.match(_TR_NUMBER_RE).any() and not values.str.match(_EN_NUMBER_RE).any()
    if turkish:
        text = text.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    elif values.str.match(_EN_NUMBER_RE).any():
        text = text.str.replace(",", "", regex=False)
    return pd.to_numeric(text, errors="coerce")


class ExternalSeriesResult(NamedTuple):
    """Mirrors `tools.series.SeriesResult`'s shape (values/source/key/name/unit/
    temporal_semantics/citation()) so the executor can treat an ingested
    external column exactly like a lakehouse-fetched one."""

    values: pd.Series           # float, month-start indexed, ascending, unique
    url: str
    value_column: str
    period_column: str
    unit: str
    sheet: Optional[str] = None
    monthly_rule: str = "last"
    temporal_semantics: str = "unknown"
    n_dropped_rows: int = 0     # rows with no parseable date or number, dropped
    source_metadata: Optional[dict] = None

    @property
    def source(self) -> str:
        return "external"

    @property
    def key(self) -> str:
        return self.url

    @property
    def name(self) -> str:
        return self.value_column

    @property
    def period_start(self) -> str:
        return self.values.index.min().strftime("%Y-%m-%d")

    @property
    def period_end(self) -> str:
        return self.values.index.max().strftime("%Y-%m-%d")

    def citation(self) -> dict:
        return {
            "table": "external", "url": self.url, "sheet": self.sheet,
            "period_column": self.period_column, "value_column": self.value_column,
            "monthly_rule": self.monthly_rule,
            "unit": self.unit, "temporal_semantics": self.temporal_semantics,
            "period_start": self.period_start, "period_end": self.period_end,
            "n_points": int(len(self.values)),
            "n_dropped_rows": int(self.n_dropped_rows),
            **(self.source_metadata or {}),
            "unit_verified": bool(self.source_metadata and
                                  self.source_metadata.get("units", {}).get(self.value_column) == self.unit),
        }


def _read_table(url: str, sheet: Optional[str]) -> pd.DataFrame:
    """Fetch url and read it as a full DataFrame -- not the head(20) preview
    `web_url.read_url` returns for the model's context window."""
    response = web_url._fetch(url)
    kind = web_url._detect_kind(response.headers.get("Content-Type", ""), url)
    if kind == "excel":
        frame = pd.read_excel(BytesIO(response.content), sheet_name=sheet or 0)
    elif kind == "csv":
        if sheet is not None:
            raise ValueError(f"{url!r} is a CSV file; it has no sheets, so sheet={sheet!r} is invalid")
        frame = pd.read_csv(BytesIO(response.content))
    elif kind == "pdf":
        from .bist_precious_metals import parse_gold_pdf
        if sheet is not None:
            raise ValueError("A PDF has no Excel sheets")
        frame = parse_gold_pdf(response.content)
    else:
        raise ValueError(f"{url!r} is {kind!r}, not a tabular (excel/csv/supported pdf) source")
    frame.attrs.setdefault("source_metadata", {}).update(
        format=kind, content_sha256=hashlib.sha256(response.content).hexdigest())
    return frame


def _resolve_column(columns, name: str, role: str) -> str:
    """Exact match, then case-insensitive -- mirrors executor._resolve_column
    so a near-miss column name behaves the same way in both places."""
    columns = list(columns)
    if name in columns:
        return name
    lowered = {str(c).lower(): c for c in columns}
    if name.lower() in lowered:
        return lowered[name.lower()]
    raise ValueError(f"{role} {name!r} not found; available columns: {[str(c) for c in columns]}")


def _detect_period_column(frame: pd.DataFrame, exclude: str) -> str:
    """The first column (left to right, excluding the value column) where
    parsing as a date succeeds on almost every row.

    Errs toward refusing rather than guessing: a wrong guess here silently
    mis-dates every value that column produces, which nothing downstream
    would catch.
    """
    candidates = []
    for column in frame.columns:
        if column == exclude:
            continue
        # A numeric column is never a period: pandas would happily read every
        # float as an epoch-nanosecond timestamp (100% "success", all of 1970),
        # and the plausible-year check below is the second line of defence.
        if is_numeric_dtype(frame[column]) or is_bool_dtype(frame[column]):
            continue
        parsed = _parse_periods(frame[column])
        rate = parsed.notna().mean() if len(frame) else 0.0
        years = parsed.dropna().dt.year
        if rate >= MIN_DATE_PARSE_RATE and years.between(*PLAUSIBLE_YEARS).all():
            candidates.append((column, rate))
    if not candidates:
        raise ValueError(
            "could not find a date-like column to use as the period; "
            f"pass period_column explicitly. Columns: {[str(c) for c in frame.columns]}"
        )
    # Highest parse rate first; a tie keeps the leftmost (its original order).
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0][0]


def ingest_external_series(
    url: str,
    value_column: str,
    period_column: Optional[str] = None,
    sheet: Optional[str] = None,
    unit: Optional[str] = None,
    monthly_rule: str = "last",
    frame: Optional[pd.DataFrame] = None,
) -> ExternalSeriesResult:
    """Fetch Excel/CSV or a supported PDF and return one monthly numeric series.

    Args:
        url: the file to fetch. Goes through the same SSRF guard as read_url.
        value_column: which column holds the numbers. Required -- an arbitrary
            external file has no schema to infer this from safely, so a plan
            must name it (typically after previewing the file with read_url).
        period_column: which column holds the dates. Auto-detected (first
            column that parses as a date on almost every row) when omitted.
        sheet: Excel sheet name; ignored for CSV, defaults to the first sheet.
        unit: best-effort only -- an external file publishes no unit the way
            the lakehouse's own sources do. Defaults to the raw column header.
        monthly_rule: "last" (default, matches a stock), "avg" (a rate) or
            "sum" (a flow) -- how multiple rows in the same month are
            collapsed onto one monthly value, via the same
            `tools.transforms.resample_to_monthly` the weekly bulletin uses.
        frame: previously parsed complete table, reused within a turn to avoid
            downloading the same source again for each selected column.

    Raises ValueError for: a non-tabular content type, a missing column, or
    no date-like column found with none specified.
    """
    frame = _read_table(url, sheet) if frame is None else frame
    if frame.empty:
        raise ValueError(f"{url!r} contains no rows")

    value_column = _resolve_column(frame.columns, value_column, "value_column")
    metadata = frame.attrs.get("source_metadata", {})
    verified_unit = metadata.get("units", {}).get(value_column)
    if verified_unit and unit and unit != verified_unit:
        raise ValueError(f"Source unit is {verified_unit!r}, not {unit!r}; explicit conversion is required")
    if period_column is not None:
        period_column = _resolve_column(frame.columns, period_column, "period_column")
    else:
        period_column = _detect_period_column(frame, exclude=value_column)

    periods = _parse_periods(frame[period_column])
    values = _parse_numbers(frame[value_column])
    series = pd.Series(values.to_numpy(), index=periods).dropna()
    series = series[~series.index.isna()]
    n_dropped_rows = len(frame) - len(series)
    if series.empty:
        raise ValueError(
            f"no row has both a parseable {period_column!r} date and a numeric {value_column!r} value"
        )
    series = series.sort_index()

    monthly = resample_to_monthly(series, monthly_rule)
    if monthly.empty:
        raise ValueError(f"resampling to monthly with rule={monthly_rule!r} produced no points")

    return ExternalSeriesResult(
        values=monthly, url=url, value_column=str(value_column), period_column=str(period_column),
        unit=verified_unit or unit or str(value_column), sheet=sheet, monthly_rule=monthly_rule,
        temporal_semantics=metadata.get("temporal_semantics", "unknown"),
        n_dropped_rows=n_dropped_rows, source_metadata=metadata or None,
    )
