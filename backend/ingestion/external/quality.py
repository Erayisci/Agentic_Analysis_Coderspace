"""Non-fatal checks on a landed source, persisted as `external_quality_report`.

The base corpora abort the build on a failed identity; an external file has
no identities to check and must land even when it is imperfect, so every
finding here is a row the agent can quote, never an exception. The columns
match `data_quality_report`'s family (`check, passed, detail`) plus the
series it concerns.
"""
from typing import List

import numpy as np
import pandas as pd

from ...extensions.web_tools.asset_common import WARNING as EXTENSION_BOILERPLATE

MIN_PERIODS = 3


def series_checks(source_id: str, series_key: str, native: pd.Series, parse_rate: float,
                  native_frequency: str, n_duplicates: int) -> List[dict]:
    def row(check, passed, detail):
        return {"source_id": source_id, "series_key": series_key, "check": check,
                "passed": bool(passed), "detail": detail}

    years = native.index.year if len(native) else np.array([])
    return [
        row("period_parse_rate", parse_rate >= 0.9, f"{parse_rate:.0%} of rows carried a parseable date"),
        row("no_duplicate_periods", n_duplicates == 0,
            "each native date appears once" if not n_duplicates else f"{n_duplicates} repeated date(s), last kept"),
        row("plausible_years", bool(len(years)) and bool(((years >= 1800) & (years <= 2100)).all()),
            f"{years.min()}..{years.max()}" if len(years) else "no dates"),
        row("enough_periods", len(native) >= MIN_PERIODS, f"{len(native)} native observation(s)"),
        row("frequency_detected", native_frequency != "irregular", native_frequency),
        row("values_finite", bool(np.isfinite(native.to_numpy(dtype=float)).all()) if len(native) else False,
            "all values finite" if len(native) else "no values"),
    ]


def source_checks(source_id: str, evidence: dict) -> List[dict]:
    """The extractor's own warnings and errors, one row each."""
    rows = []
    for warning in evidence.get("warnings") or []:
        if warning == EXTENSION_BOILERPLATE:
            continue
        rows.append({"source_id": source_id, "series_key": None, "check": "extraction_warning",
                     "passed": False, "detail": str(warning)[:500]})
    for error in evidence.get("processing_errors") or []:
        message = error.get("message") if isinstance(error, dict) else str(error)
        rows.append({"source_id": source_id, "series_key": None, "check": "extraction_error",
                     "passed": False, "detail": str(message)[:500]})
    status = evidence.get("status")
    rows.append({"source_id": source_id, "series_key": None, "check": "extraction_complete",
                 "passed": status == "ok", "detail": f"extractor status: {status}"})
    return rows
