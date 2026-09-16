"""Turn one column of an external Excel/CSV file into a time series the agent
can add to the current turn's table -- session-scoped only.

This is deliberately NOT a write into `data/lakehouse.duckdb`. That database
has exactly one writer (`backend.lakehouse.build`) and every other reader
(every tool, every script) opens it `read_only=True`; a live agent turn
writing an unseen, demo-day file into the shared analytical database on every
question would break that invariant and risk corrupting or locking it for
every other session. So an externally-ingested series lives only in the
current `Session.artifact`, exactly like a `transform`-derived column already
does, and disappears with the session.

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
from io import BytesIO
from typing import NamedTuple, Optional

import pandas as pd

from . import web_url
from .transforms import resample_to_monthly

MIN_DATE_PARSE_RATE = 0.9  # a period column must parse on (almost) every row


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
            "unit_verified": False,
        }


def _read_table(url: str, sheet: Optional[str]) -> pd.DataFrame:
    """Fetch url and read it as a full DataFrame -- not the head(20) preview
    `web_url.read_url` returns for the model's context window."""
    response = web_url._fetch(url)
    kind = web_url._detect_kind(response.headers.get("Content-Type", ""), url)
    if kind == "excel":
        return pd.read_excel(BytesIO(response.content), sheet_name=sheet or 0)
    if kind == "csv":
        if sheet is not None:
            raise ValueError(f"{url!r} is a CSV file; it has no sheets, so sheet={sheet!r} is invalid")
        return pd.read_csv(BytesIO(response.content))
    raise ValueError(f"{url!r} is {kind!r}, not a tabular (excel/csv) source")


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
        parsed = pd.to_datetime(frame[column], errors="coerce")
        rate = parsed.notna().mean() if len(frame) else 0.0
        if rate >= MIN_DATE_PARSE_RATE:
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
) -> ExternalSeriesResult:
    """Fetch an external Excel/CSV file and return one column as a monthly series.

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

    Raises ValueError for: a non-tabular content type, a missing column, or
    no date-like column found with none specified.
    """
    frame = _read_table(url, sheet)
    if frame.empty:
        raise ValueError(f"{url!r} contains no rows")

    value_column = _resolve_column(frame.columns, value_column, "value_column")
    if period_column is not None:
        period_column = _resolve_column(frame.columns, period_column, "period_column")
    else:
        period_column = _detect_period_column(frame, exclude=value_column)

    periods = pd.to_datetime(frame[period_column], errors="coerce")
    values = pd.to_numeric(frame[value_column], errors="coerce")
    series = pd.Series(values.to_numpy(), index=periods).dropna()
    series = series[~series.index.isna()]
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
        unit=unit or str(value_column), sheet=sheet, monthly_rule=monthly_rule,
    )
