"""Coverage checks for the EVDS macro corpus.

The failure this guards against is a series that silently stops or starts
late: a chart with a cliff and no error anywhere. The window a series is
expected to cover is the brief's window clipped to what TCMB says it
publishes (`published_start`/`published_end` from the serieList), so KKM
starting 2021-12 or the weekly bank tables starting 2024-06 are fine.

Two regimes, because the corpus has two kinds of series:

    strict    national tier-0 series (level 1): the demo inputs. Any missing
              month inside the window aborts the build.
    reported  everything else. Small provinces publish no row for a month
              with zero mortgaged sales, the BIST gold market has quiet
              days, survey items get retired. Gaps are counted in the
              data-quality report so the agent can see them, not hidden and
              not fatal.
"""
import datetime as dt

import pandas as pd

from ..core.errors import ValidationError

WINDOW_START = dt.date(2021, 1, 1)
WINDOW_END = dt.date(2026, 6, 1)


def _months(start: dt.date, end: dt.date) -> list:
    return [p.to_timestamp().date() for p in pd.period_range(start, end, freq="M")]


def _published(catalogue: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_datetime(catalogue.set_index("series_code")[column], format="%d-%m-%Y", errors="coerce").dt.date


def check_unit_resolution(catalogue: pd.DataFrame) -> pd.DataFrame:
    """Every series must carry a resolved unit, and say where it came from.

    TCMB publishes `BIRIMI` per data group, so the raw string is compound for
    some groups and not a unit at all for others (see
    `parsing.evds.resolve_unit`). The resolver falls back on the declared
    semantics; a series that reaches the end of that chain unresolved would
    reach the agent with a NULL unit, which is exactly the silent failure the
    bulletin's caption check exists to prevent. So it aborts the build instead.

    The passing rows are kept: they record how many series took each route, so
    a later TCMB relabelling shows up as a shift in the counts rather than as
    nothing at all.
    """
    unresolved = catalogue[catalogue.unit.isna()]
    if not unresolved.empty:
        listed = unresolved[["series_code", "unit_source", "temporal_semantics"]].head(10)
        raise ValidationError(
            f"{len(unresolved)} EVDS series resolved to no unit:\n"
            + listed.to_string(index=False)
            + "\nAdd the missing spelling to parsing.evds.UNIT_PATTERNS."
        )

    published = catalogue[~catalogue.derived.fillna(False)]
    rows = []
    for unit, group in published.groupby("unit"):
        verbatim = int((group.unit_source.fillna("") == unit).sum())
        rows.append({
            "source": "TCMB_EVDS",
            "check": f"unit resolution {unit}",
            "passed": True,
            "detail": (
                f"{len(group)} series across {group.datagroup.nunique()} group(s); "
                f"{verbatim} taken verbatim from BIRIMI, {len(group) - verbatim} resolved from "
                f"the series name or the declared semantics "
                f"(BIRIMI: {', '.join(sorted(set(group.unit_source.fillna('<none>'))))})"
            ),
        })
    return pd.DataFrame(rows)


def check_macro_coverage(monthly: pd.DataFrame, catalogue: pd.DataFrame) -> pd.DataFrame:
    """One report row per series; a failed strict row aborts the build."""
    meta = catalogue.set_index("series_code")
    published_start = _published(catalogue, "published_start")
    published_end = _published(catalogue, "published_end")

    rows = []
    for code, group in monthly.groupby("series_code"):
        tier = int(meta.at[code, "tier"])
        strict = tier == 0 and int(meta.at[code, "level"] or 1) == 1
        start, end = WINDOW_START, WINDOW_END
        if pd.notna(published_start.get(code)):
            start = max(start, published_start[code].replace(day=1))
        if pd.notna(published_end.get(code)):
            end = min(end, published_end[code].replace(day=1))
        expected = _months(start, end) if start <= end else []
        if meta.at[code, "native_frequency"] == "quarterly":
            expected = [m for m in expected if m.month in (3, 6, 9, 12)]

        covered = set(group.period)
        missing = [m for m in expected if m not in covered]
        detail = f"{len(expected) - len(missing)}/{len(expected)} months in {start}..{end}"
        if missing:
            detail += f"; {len(missing)} gap(s): " + ", ".join(str(m) for m in missing[:3])
            detail += " ..." if len(missing) > 3 else ""
        rows.append({
            "source": "TCMB_EVDS",
            "check": f"coverage {'strict' if strict else 'reported'} tier{tier} {code}",
            "passed": not (strict and missing),
            "detail": detail,
        })

    report = pd.DataFrame(rows)
    failed = report[~report.passed]
    if not failed.empty:
        raise ValidationError(
            "EVDS coverage checks FAILED:\n" + failed[["check", "detail"]].to_string(index=False)
        )
    return report
