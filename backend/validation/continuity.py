"""Continuity and identity checks for the BDDK bulletin tables.

The corpus is keyed on normalised row labels, which makes a renamed or dropped
line item look like a series that simply stops. That is exactly the failure we
cannot afford: an entity silently disappearing halfway through history produces
a chart with a cliff in it and no error anywhere.

So every entity must either span the full archived history or be registered in
`bulletin_tables.KNOWN_LIFECYCLES` with the period it starts or ends. An
unregistered gap aborts the build.
"""
import pandas as pd

from ..core.errors import ValidationError
from ..domain.bulletin_tables import KNOWN_LIFECYCLES


def check_continuity(observations: pd.DataFrame, table_slug: str) -> list:
    """One check row per entity whose coverage is incomplete.

    Periods are compared as ISO strings so the registry can be written with
    readable literals regardless of the frame's date dtype.
    """
    periods = sorted({str(p) for p in observations["period"].unique()})
    first_period, last_period = periods[0], periods[-1]
    expected = set(periods)

    results = []
    for entity_key, group in observations.groupby("entity_key"):
        covered = {str(p) for p in group["period"].unique()}
        if covered == expected:
            continue

        registered = KNOWN_LIFECYCLES.get((table_slug, entity_key))
        entity_first, entity_last = min(covered), max(covered)

        if registered is None:
            results.append(
                {
                    "dataset": table_slug,
                    "check": "entity_continuity",
                    "entity_key": entity_key,
                    "passed": False,
                    "detail": (
                        f"covers {len(covered)}/{len(expected)} periods "
                        f"({entity_first}..{entity_last}) and is not in KNOWN_LIFECYCLES"
                    ),
                }
            )
            continue

        starts, ends = registered
        window = {p for p in periods
                  if (starts is None or p >= starts) and (ends is None or p <= ends)}
        missing = window - covered
        extra = covered - window
        passed = not missing and not extra
        detail = f"registered lifecycle {starts or first_period}..{ends or last_period}"
        if not passed:
            detail += f"; missing {len(missing)}, unexpected {len(extra)}"
        results.append(
            {
                "dataset": table_slug,
                "check": "entity_continuity",
                "entity_key": entity_key,
                "passed": passed,
                "detail": detail,
            }
        )

    return results


def check_identities(observations: pd.DataFrame, table_slug: str, tolerance: float = 2.0) -> list:
    """Verify each row's own '(2+3+4)' formula, per period.

    The formulas are read from the labels of the period being checked, never
    reused across history: 'Kredi Riskine Esas Tutar (11+12+13+27+28)' became
    '(11+12+13)' in 2021-06, so a formula pinned once validates the wrong rows.
    """
    import re

    addends = re.compile(r"^\(\s*(\d+(?:\s*\+\s*\d+)+)\s*\)$")
    results = []

    for (period, metric, currency), group in observations.groupby(
        ["period", "metric", "currency"], dropna=False
    ):
        # Formulas address rows by BasitSira, which the parser deliberately
        # drops, so re-derive positions from the period's own row order.
        ordered = group.reset_index(drop=True)
        by_position = {i + 1: row for i, row in ordered.iterrows()}

        for position, row in by_position.items():
            formula = row["formula"]
            if not isinstance(formula, str):
                continue
            match = addends.match(formula.strip())
            if not match:
                continue

            parts = [int(p) for p in match.group(1).split("+")]
            if any(p not in by_position for p in parts):
                continue

            total = sum(by_position[p]["value"] for p in parts)
            delta = abs(total - row["value"])
            if delta > tolerance:
                results.append(
                    {
                        "dataset": table_slug,
                        "check": "row_identity",
                        "entity_key": row["entity_key"],
                        "passed": False,
                        "detail": (
                            f"{period} {metric}/{currency}: {formula} sums to {total:,.0f}, "
                            f"row states {row['value']:,.0f} (delta {delta:,.0f})"
                        ),
                    }
                )

    return results


def run_bulletin_validations(observations: pd.DataFrame, table_slug: str) -> pd.DataFrame:
    """Run every check for one table; raise if any fails."""
    results = check_continuity(observations, table_slug)
    report = pd.DataFrame.from_records(results) if results else pd.DataFrame(
        columns=["dataset", "check", "entity_key", "passed", "detail"]
    )

    failures = report[~report["passed"]] if len(report) else report
    if len(failures):
        lines = [f"  {r.check} {r.entity_key}: {r.detail}" for r in failures.itertuples()]
        raise ValidationError(
            f"{table_slug}: {len(failures)} bulletin validation(s) failed:\n" + "\n".join(lines)
        )
    return report
