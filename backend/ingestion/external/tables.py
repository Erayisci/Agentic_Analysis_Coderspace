"""Row grids from the extractor -> time series.

The web-tools extractor hands every format back the same way: a section with
`rows`, a grid of strings (`asset_extract.add_rows` stringifies cells, dates
as ISO). That is the whole input contract here, which is why one module
covers a BDDK workbook, a pdfplumber table, an HTML table and an OCR'd image.

What a regulator's grid looks like, and what each step is for:

    Tüketici Kredileri                      <- caption rows (title, "(Milyon TL)")
    (Milyon TL)
    Dönem      | Konut  | Taşıt  | İhtiyaç  <- the header row
               | TP     | TP     | TP       <- sometimes a second header row
    Ocak 2021  | 1.234  | 567    | 890      <- data
    ...
    Kaynak: BDDK                            <- a footnote row, no date

1. header detection: the first row that is mostly text and is followed by a
   row that is mostly numbers; a second text row directly under it is a
   second header line and is joined ("Konut | TP"). Rows above it are the
   caption -- where BDDK prints the unit.
2. orientation: when the header itself is mostly dates ("2021-01 | 2021-02 |
   ..."), periods run ACROSS and the grid is transposed so each row label
   becomes a series. BDDK and TÜİK both publish that way.
3. the period column: the column that parses as a date on (almost) every
   non-empty row; footnote rows have no date and are dropped and counted.
4. every remaining column that parses as a number on most rows is one series.

Numbers arrive as text, and the decimal convention is decided per column from
the evidence in it (see `parse_number_column`): two dot-separated groups or a
decimal comma prove Turkish notation, a comma group or a short decimal proves
English. With no evidence at all a lone `1.017` is read as a decimal point,
because the dominant producer of these grids is `str(float)` from a
spreadsheet cell -- the opposite default from `tools.external_series`, which
reads typed cells and only ever sees text in a CSV.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

import pandas as pd

from ...core.labels import slugify
from ...tools.external_series import _BILINGUAL_SUFFIX_RE, _parse_periods
from .align import infer_frequency
from .labels import clean_name, infer_semantics, infer_unit, monthly_rule_for
from .ocr import grids_from_ocr_text

MAX_HEADER_SCAN = 30
MIN_LABEL_SHARE = 0.6        # a header row: this share of its cells are text
MIN_NUMERIC_SHARE = 0.5      # a data row: this share of its cells are numbers
MIN_PERIOD_PARSE_RATE = 0.8  # a period column parses on this share of rows
MIN_VALUE_PARSE_RATE = 0.6   # a value column parses on this share of rows
MIN_ROWS = 3
PLAUSIBLE_YEARS = (1800, 2100)   # historical series exist; a mis-parse lands in 1970 or 1234, not 1833

_BLANK = {"", "nan", "none", "null", "-", "—", "–", "n/a", "na", ".", ".."}
_NUMBER_RE = re.compile(r"^[-+−]?\(?\s*\d[\d.,\s]*\)?\s*%?$")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_MONTHS = (r"ocak|şubat|subat|mart|nisan|mayıs|mayis|haziran|temmuz|ağustos|agustos|eylül|eylul|ekim|"
           r"kasım|kasim|aralık|aralik|january|february|march|april|may|june|july|august|september|"
           r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec")
# Year-first dates take '-' or '/' only: with '.' allowed, '1000.5' read as a date.
_DATE_RE = re.compile(
    rf"(\b\d{{1,2}}[./-]\d{{1,2}}[./-](19|20)\d{{2}}\b)|(\b(19|20)\d{{2}}[-/]\d{{1,2}}([-/]\d{{1,2}})?\b)|"
    rf"(\b(19|20)\d{{2}}\s*[-/]?\s*[qç][1-4]\b)|(\b({_MONTHS})\b)", re.I)
_MONTH_ONLY_RE = re.compile(rf"^\s*({_MONTHS})\s*$", re.I)
_CAPTION_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")

_TR_STRONG = re.compile(r"^[-+−]?\d{1,3}(\.\d{3}){2,}(,\d+)?$|^[-+−]?\d{1,3}(\.\d{3})*,\d+$|^[-+−]?\d+,\d+$")
_EN_STRONG = re.compile(r"^[-+−]?\d{1,3}(,\d{3})+(\.\d+)?$|^[-+−]?\d+\.\d{1,2}$|^[-+−]?\d+\.\d{4,}$")


@dataclass
class RawTable:
    """One grid as the extractor delivered it, plus where it was and the text around it."""

    frame: pd.DataFrame          # strings, header=None
    location: str
    caption: str = ""
    method: str = "table"


@dataclass
class SeriesBundle:
    """One series with everything `external_series` records about it."""

    series_key: str
    name: str
    name_clean: str
    location: str
    unit: str
    unit_source: str
    temporal_semantics: str
    semantics_source: str
    native_frequency: str
    monthly_rule: str
    native: pd.Series            # float, DatetimeIndex ascending unique
    parse_rate: float
    n_duplicates: int
    caption: str = ""
    warnings: List[str] = field(default_factory=list)


# -- cell predicates ----------------------------------------------------------

def _text(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, float) and pd.isna(cell):
        return ""
    return str(cell).replace(" ", " ").strip()


def _is_blank(cell) -> bool:
    return _text(cell).lower() in _BLANK


def _is_number(cell) -> bool:
    text = _text(cell).replace(" ", "")
    return bool(text) and bool(_NUMBER_RE.match(text)) and not _DATE_RE.search(text) and not _YEAR_RE.match(text)


def _is_year(cell) -> bool:
    return bool(_YEAR_RE.match(_text(cell)))


def _is_date(cell) -> bool:
    text = _text(cell)
    return bool(text) and (bool(_DATE_RE.search(text)) or _is_year(cell))


def _row_stats(row) -> Tuple[int, float, float]:
    """(non-blank cells, share that are text/date, share that are numbers)."""
    cells = [c for c in row if not _is_blank(c)]
    if not cells:
        return 0, 0.0, 0.0
    numeric = sum(1 for c in cells if _is_number(c))
    return len(cells), (len(cells) - numeric) / len(cells), numeric / len(cells)


# -- grids from evidence --------------------------------------------------------

def _grid(rows) -> Optional[pd.DataFrame]:
    rows = [[_text(c) for c in row] for row in rows if row is not None]
    rows = [row for row in rows if any(not _is_blank(c) for c in row)]
    if len(rows) < 2:
        return None
    width = max(len(row) for row in rows)
    return pd.DataFrame([row + [""] * (width - len(row)) for row in rows])


def tables_from_evidence(evidence: dict) -> List[RawTable]:
    """Every grid in an extractor result, with its location and caption."""
    title = _text(evidence.get("title"))
    sections = evidence.get("sections") or []
    tables: List[RawTable] = []
    previous_text = ""
    for index, section in enumerate(sections):
        location = _text(section.get("location")) or f"section {index + 1}"
        rows = section.get("rows")
        method = section.get("method") or "table"
        if rows:
            grids, extra_caption = [_grid(rows)], (section.get("text") or "")
        elif method in ("mia_ocr", "local_ocr", "mia_vision", "pdf_text", "plain_text", "docx_text", "html_text"):
            # OCR output and free text: HTML tables (what Unlimited-OCR
            # returns), then markdown / tab tables; the text around them
            # is the caption -- the title carries the year.
            found, extra_caption = grids_from_ocr_text(section.get("text") or "")
            grids = [_grid(g) for g in found]
            if not any(g is not None for g in grids):
                previous_text = extra_caption[:300]
                continue
        else:
            grids, extra_caption = [], ""
        caption = " ".join(part for part in (title, previous_text, extra_caption) if part).strip()[:500]
        for number, grid in enumerate(grids, start=1):
            if grid is None:
                continue
            where = location if len(grids) == 1 else f"{location}, table {number}"
            tables.append(RawTable(frame=grid, location=where, caption=caption, method=method))
        previous_text = ""
    return _merge_continued(tables)


def _merge_continued(tables: List[RawTable]) -> List[RawTable]:
    """A table that continues over several PDF pages repeats its header on
    each; consecutive grids with an identical first row are one table."""
    merged: List[RawTable] = []
    for table in tables:
        if merged and merged[-1].method == table.method and table.method in ("pdf_words", "table"):
            previous = merged[-1]
            same_width = previous.frame.shape[1] == table.frame.shape[1]
            if same_width and list(previous.frame.iloc[0]) == list(table.frame.iloc[0]) and len(table.frame) > 1:
                frame = pd.concat([previous.frame, table.frame.iloc[1:]], ignore_index=True)
                first = previous.location.split("-")[0].split(",")[0]
                last = table.location.split(",")[0]
                merged[-1] = RawTable(frame=frame, location=f"{first}-{last}", caption=previous.caption,
                                      method=previous.method)
                continue
        merged.append(table)
    return merged


# -- header detection -------------------------------------------------------------

def _next_nonblank(frame: pd.DataFrame, start: int) -> Optional[int]:
    for r in range(start, len(frame)):
        if _row_stats(frame.iloc[r])[0]:
            return r
    return None


def _forward_fill(cells: List[str]) -> List[str]:
    out, last = [], ""
    for cell in cells:
        if not _is_blank(cell):
            last = _text(cell)
        out.append(last)
    return out


def find_header(frame: pd.DataFrame) -> Tuple[List[str], int, str]:
    """(column labels, first data row, caption text)."""
    n = len(frame)
    for r in range(min(MAX_HEADER_SCAN, n - 1)):
        count, label_share, _ = _row_stats(frame.iloc[r])
        if count < 2 or label_share < MIN_LABEL_SHARE:
            continue
        nxt = _next_nonblank(frame, r + 1)
        if nxt is None:
            continue
        ncount, nlabel, nnum = _row_stats(frame.iloc[nxt])
        if nnum >= MIN_NUMERIC_SHARE:
            labels = [_text(c) for c in frame.iloc[r]]
            return _unique(labels), nxt, _caption(frame, r)
        if nlabel >= MIN_LABEL_SHARE and ncount >= 2:
            after = _next_nonblank(frame, nxt + 1)
            if after is not None and _row_stats(frame.iloc[after])[2] >= MIN_NUMERIC_SHARE:
                top = _forward_fill([_text(c) for c in frame.iloc[r]])
                bottom = [_text(c) for c in frame.iloc[nxt]]
                labels = [" | ".join(p for p in (a, b) if p and not _is_blank(p)) for a, b in zip(top, bottom)]
                return _unique(labels), after, _caption(frame, r)
    labels = [_text(c) for c in frame.iloc[0]]
    return _unique(labels), 1, ""


def _caption(frame: pd.DataFrame, header_row: int) -> str:
    parts = []
    for r in range(header_row):
        parts += [_text(c) for c in frame.iloc[r] if not _is_blank(c)]
    return " ".join(parts)[:500]


def _unique(labels: List[str]) -> List[str]:
    seen, out = {}, []
    for index, label in enumerate(labels):
        name = label if label and not _is_blank(label) else f"col_{index + 1}"
        seen[name] = seen.get(name, 0) + 1
        out.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return out


# -- numbers and periods ------------------------------------------------------------

def _votes(cells: pd.Series) -> Tuple[int, int]:
    text = cells.map(_text).str.replace(" ", "", regex=False)
    candidates = text[text.map(lambda t: bool(t) and t.lower() not in _BLANK)]
    return int(candidates.str.match(_TR_STRONG).sum()), int(candidates.str.match(_EN_STRONG).sum())


def table_convention(frame: pd.DataFrame) -> str:
    """The whole grid's decimal convention: 'tr', 'en' or 'default' (no evidence)."""
    tr_votes, en_votes = _votes(pd.Series(frame.to_numpy().ravel()))
    if tr_votes and tr_votes >= en_votes:
        return "tr"
    return "en" if en_votes else "default"


def parse_number_column(raw: pd.Series, fallback: str = "default") -> Tuple[pd.Series, str]:
    """Floats from a column of text, the decimal convention decided per column.

    A column with no evidence of its own ('825', '1.008', '13.837' -- one dot,
    three digits) takes `fallback`: the table's convention, because a Turkish
    report that prints 93.824.682.381 in one column prints 1.008 for a count of
    one thousand and eight in the next. With no evidence anywhere, '.' is the
    decimal point (`str(float)` from a spreadsheet cell).

    Returns (values, convention) with convention in {'tr', 'en', 'default'}.
    """
    text = raw.map(_text).str.replace(" ", "", regex=False).str.replace("−", "-", regex=False)
    text = text.str.replace(r"^\((.*)\)$", r"-\1", regex=True).str.rstrip("%")
    tr_votes, en_votes = _votes(text)
    if tr_votes and tr_votes >= en_votes:
        convention = "tr"
    elif en_votes:
        convention = "en"
    else:
        convention = fallback
    if convention == "tr":
        text = text.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    else:
        text = text.str.replace(",", "", regex=False)
    return pd.to_numeric(text, errors="coerce"), convention


def _parse_period_text(raw: pd.Series) -> pd.Series:
    text = raw.map(_text)
    text = text.str.replace(r"^(\d{4})\s*[-/]?\s*[qQçÇ]([1-4])$",
                            lambda m: f"{m.group(1)}-{int(m.group(2)) * 3:02d}-01", regex=True)
    text = text.str.replace(r"^(\d{4})[-/.](\d{1,2})$", lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-01", regex=True)
    text = text.str.replace(r"^(\d{4})$", r"\1-01-01", regex=True)
    parsed = _parse_periods(text.where(text != "", None))
    years = parsed.dt.year
    return parsed.where(years.between(*PLAUSIBLE_YEARS), pd.NaT)


def _month_only(value) -> bool:
    return bool(_MONTH_ONLY_RE.match(_BILINGUAL_SUFFIX_RE.sub("", _text(value))))


def _period_column(data: pd.DataFrame, caption: str = "") -> Tuple[Optional[str], pd.Series, float]:
    """The column that reads as a date on most rows, or a year column plus a
    month column combined, or month names plus the year the caption states.
    Returns (label, parsed periods, parse rate)."""
    # A year column beside a month-name column ("2021" | "Ocak") is one period
    # axis in two columns; checked first, because the year column alone also
    # "parses" -- onto a single January, collapsing every row of the year.
    years = [c for c in data.columns if data[c].map(lambda v: _is_year(v) or _is_blank(v)).all()
             and data[c].map(_is_year).sum() >= MIN_ROWS]
    months = [c for c in data.columns
              if data[c].map(_month_only).sum() >= MIN_ROWS
              and data[c].map(lambda v: _month_only(v) or _is_blank(v) or not _is_number(v)).mean() >= 0.8]
    if years and months:
        combined = data[months[0]].map(_text) + " " + _forward_fill_series(data[years[0]])
        parsed = _parse_period_text(combined)
        rate = float(parsed.notna().mean())
        if rate >= MIN_PERIOD_PARSE_RATE:
            return f"{years[0]}+{months[0]}", parsed, rate
    # "Ocak / January" with the year only in the title: a regulator's annual
    # sheet. pandas would silently stamp the current year on a bare month.
    if months:
        stated = _CAPTION_YEAR_RE.findall(caption or "")
        if stated:
            year = stated[-1]
            column = data[months[0]]
            combined = column.map(lambda v: f"{_BILINGUAL_SUFFIX_RE.sub('', _text(v))} {year}" if _month_only(v) else "")
            parsed = _parse_period_text(combined)
            nonblank = ~column.map(_is_blank)
            rate = float(parsed.notna().sum() / max(1, nonblank.sum()))
            if parsed.notna().sum() >= MIN_ROWS:
                return f"{months[0]}+caption:{year}", parsed, rate

    best = (None, None, 0.0)
    for column in data.columns:
        nonblank = ~data[column].map(_is_blank)
        if nonblank.sum() < MIN_ROWS or data[column][nonblank].map(_is_number).mean() > 0.5:
            continue
        parsed = _parse_period_text(data[column])
        rate = float(parsed.notna().sum() / nonblank.sum())
        if rate >= MIN_PERIOD_PARSE_RATE and rate > best[2]:
            best = (column, parsed, rate)
    if best[0] is not None:
        return best
    return None, pd.Series(pd.NaT, index=data.index), 0.0


def _forward_fill_series(column: pd.Series) -> pd.Series:
    return pd.Series(_forward_fill(list(column)), index=column.index)


# -- series ---------------------------------------------------------------------------

def _bundle(source_id: str, location: str, header: str, caption: str, values: pd.Series,
            periods: pd.Series, parse_rate: float, seen: Set[str], warnings: List[str]) -> Optional[SeriesBundle]:
    native = pd.Series(values.to_numpy(dtype=float), index=pd.DatetimeIndex(periods))
    native = native[~native.index.isna()].dropna()
    if len(native) < MIN_ROWS:
        return None
    duplicates = int(native.index.duplicated().sum())
    if duplicates:
        native = native[~native.index.duplicated(keep="last")]
    native = native.sort_index()

    name_clean, _ = clean_name(header)
    unit, unit_source = infer_unit(header, caption)
    semantics, semantics_source = infer_semantics(header, caption, unit)
    key = f"{source_id}/{slugify(location) or 'table'}/{slugify(name_clean) or 'series'}"
    suffix = 2
    while key in seen:
        key = f"{source_id}/{slugify(location) or 'table'}/{slugify(name_clean) or 'series'}_{suffix}"
        suffix += 1
    seen.add(key)
    return SeriesBundle(
        series_key=key, name=header, name_clean=name_clean, location=location,
        unit=unit, unit_source=unit_source, temporal_semantics=semantics,
        semantics_source=semantics_source, native_frequency=infer_frequency(native.index),
        monthly_rule=monthly_rule_for(semantics), native=native, parse_rate=parse_rate,
        n_duplicates=duplicates, caption=caption, warnings=list(warnings))


def series_from_table(raw: RawTable, source_id: str, seen: Optional[Set[str]] = None) -> List[SeriesBundle]:
    """Every time series one grid holds. Raises ValueError when the grid has
    no period axis -- a table of names and numbers is not a time series."""
    seen = seen if seen is not None else set()
    frame = raw.frame.copy()
    frame = frame.loc[:, [c for c in frame.columns if not frame[c].map(_is_blank).all()]]
    frame = frame[~frame.apply(lambda row: all(_is_blank(c) for c in row), axis=1)].reset_index(drop=True)
    if len(frame) < MIN_ROWS + 1 or frame.shape[1] < 2:
        raise ValueError("fewer than two columns or four rows")

    labels, first_data_row, detected_caption = find_header(frame)
    caption = " ".join(part for part in (raw.caption, detected_caption) if part).strip()
    data = frame.iloc[first_data_row:].reset_index(drop=True)
    data.columns = labels

    # Periods across the top: transpose so each row label becomes a series.
    date_like = [_is_date(label) for label in labels[1:]]
    if labels and sum(date_like) >= max(2, MIN_LABEL_SHARE * len(date_like)):
        return _series_from_periods_across(data, labels, source_id, raw.location, caption, seen)

    period_label, periods, rate = _period_column(data, caption)
    if period_label is None:
        raise ValueError(f"no column reads as a date; columns: {labels[:8]}")
    period_columns = {part for part in period_label.split("+") if not part.startswith("caption:")}
    keep = periods.notna()
    dropped = int((~keep).sum())
    warnings = [f"{dropped} row(s) without a date dropped (footnotes, totals)"] if dropped else []

    bundles = []
    fallback = table_convention(data)
    for column in data.columns:
        if column in period_columns:
            continue
        cells = data[column][keep]
        nonblank = ~cells.map(_is_blank)
        if nonblank.sum() < MIN_ROWS:
            continue
        values, convention = parse_number_column(cells, fallback)
        share = float(values.notna().sum() / nonblank.sum())
        if share < MIN_VALUE_PARSE_RATE:
            continue
        notes = warnings + ([f"decimal convention: {convention}"] if convention != "default" else [])
        bundle = _bundle(source_id, raw.location, column, caption, values, periods[keep], rate, seen, notes)
        if bundle is not None:
            bundles.append(bundle)
    return bundles


def _series_from_periods_across(data: pd.DataFrame, labels: List[str], source_id: str, location: str,
                                caption: str, seen: Set[str]) -> List[SeriesBundle]:
    periods = _parse_period_text(pd.Series(labels[1:]))
    period_columns = [label for label, stamp in zip(labels[1:], periods) if pd.notna(stamp)]
    stamps = pd.Series([s for s in periods if pd.notna(s)])
    rate = float(len(period_columns) / max(1, len(labels) - 1))
    label_column = labels[0]

    bundles = []
    fallback = table_convention(data)
    for _, row in data.iterrows():
        header = _text(row[label_column])
        if _is_blank(header) or _is_number(header):
            continue
        values, convention = parse_number_column(pd.Series([row[c] for c in period_columns]), fallback)
        nonblank = sum(1 for c in period_columns if not _is_blank(row[c]))
        if nonblank < MIN_ROWS or values.notna().sum() / nonblank < MIN_VALUE_PARSE_RATE:
            continue
        notes = ["periods run across the columns; transposed"] + (
            [f"decimal convention: {convention}"] if convention != "default" else [])
        bundle = _bundle(source_id, location, header, caption, values, stamps, rate, seen, notes)
        if bundle is not None:
            bundles.append(bundle)
    return bundles
