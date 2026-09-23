"""Continuity and identity checks for the BDDK bulletin tables.

The corpus is keyed on normalised row labels, which makes a renamed or dropped
line item look like a series that simply stops. That is exactly the failure we
cannot afford: an entity silently disappearing halfway through history produces
a chart with a cliff in it and no error anywhere.

So every entity must either span the full archived history or be registered in
`bulletin_tables.KNOWN_LIFECYCLES` with the period it starts or ends. An
unregistered gap aborts the build.
"""
import ast
import re

import pandas as pd

from ..core.errors import ValidationError
from ..domain.bulletin_tables import KNOWN_IDENTITY_EXCEPTIONS, KNOWN_LIFECYCLES

# '(2 den 26'ya)' / '(2 dan 24 e)' -- the prose form of a range.
_RANGE_WORDS = re.compile(r"^\(\s*(\d+)\s*(?:den|dan)\s*(\d+)\s*['’]?\s*(?:ya|ye|a|e)\s*\)$")

# BDDK writes a range gap as '..', '...', '….' or the single character '…'.
_ELLIPSIS = re.compile(r"\.{2,}|…\.?|\.…")

# After expansion a formula may only be positions, + and - inside parentheses.
_ADDITIVE_ONLY = re.compile(r"^[()\d+\-]+$")

# 'Risk Ağırlığı %75 Olan Kalemler Toplamı' -- a child row that states the risk
# weight its exposure carries. Only the capital-adequacy table writes these.
_RISK_WEIGHT = re.compile(r"Risk\s+Ağırlığı\s*%\s*(\d+)")

# Measured ceiling for the risk-weight identity below. The buckets are published
# rounded to milyon TL and BDDK aggregates per exposure rather than by weighting
# the rounded bucket, so the reconstruction is close but not exact: the largest
# relative gap across all 67 months is 0.073%. Three times that is the alarm
# threshold -- wide enough for the published rounding, far too narrow to survive
# a bucket being dropped or mis-assigned to its parent.
RISK_WEIGHT_TOLERANCE_PCT = 0.25


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


def expand_formula(formula: str):
    """A BDDK row formula -> a Python arithmetic expression over row positions.

    The labels state their own arithmetic, which makes the identity rules
    machine-derivable instead of hand-curated. Five spellings appear across the
    17 tables, all measured at 2026-06:

        (2+3+4)                 plain addition
        (3+..+9) (1+...+14)     a range, with two, three or four dots, or the
        (40+…+51) (19+….+35)    single '…' character
        (10+...+22+25)          a range plus extra terms
        (15-23)                 subtraction
        (1+...+14)-(2+3+4+5)    chained groups
        [(26+34+50)-45]         bracketed
        (2 den 26'ya)           the Turkish range form

    Returns None for anything that is not pure +/- over positions, which drops
    the ratio definitions such as '((6/7)*100)': those are a percentage of two
    other rows, not an additive identity, and belong to a different check.
    """
    text = str(formula).strip()

    match = _RANGE_WORDS.match(text)
    if match:
        first, last = int(match.group(1)), int(match.group(2))
        return "+".join(str(i) for i in range(first, last + 1)) if first <= last else None

    text = text.replace("[", "(").replace("]", ")")
    text = _ELLIPSIS.sub("...", text)
    text = re.sub(r"\s+", "", text)

    # Expand 'a+...+b' into every position it stands for.
    def _expand(match):
        first, last = int(match.group(1)), int(match.group(2))
        if first > last:
            raise ValueError("descending range")
        return "+".join(str(i) for i in range(first, last + 1))

    try:
        text = re.sub(r"(\d+)\+\.\.\.\+(\d+)", _expand, text)
    except ValueError:
        return None

    if not _ADDITIVE_ONLY.match(text):
        return None
    return text


def _evaluate(expression: str, value_at):
    """Evaluate an expanded formula, reading each position through `value_at`.

    Parsed with `ast` and walked node by node rather than eval'd, so a label is
    never executed as code.
    """
    try:
        tree = ast.parse(expression, mode="eval").body
    except SyntaxError:
        return None

    def walk(node):
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            left, right = walk(node.left), walk(node.right)
            if left is None or right is None:
                return None
            return left + right if isinstance(node.op, ast.Add) else left - right
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            operand = walk(node.operand)
            if operand is None:
                return None
            return operand if isinstance(node.op, ast.UAdd) else -operand
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return value_at(node.value)
        return None

    return walk(tree)


def check_identities(observations: pd.DataFrame, table_slug: str,
                     tolerance: float = 2.0, relative: float = 0.0005) -> list:
    """Verify each row's own '(2+3+4)' formula, per period.

    The formulas are read from the labels of the period being checked, never
    reused across history: 'Kredi Riskine Esas Tutar (11+12+13+27+28)' became
    '(11+12+13)' in 2021-06, so a formula pinned once validates the wrong rows.

    Positions come from `row_position`, which the parser records per period for
    exactly this reason -- the row order of the period being checked, not of
    any other.
    """
    results = []
    formulas = observations[observations.formula.notna()]
    if formulas.empty:
        return results

    for (period, metric, currency), group in observations.groupby(
        ["period", "metric", "currency"], dropna=False
    ):
        by_position = dict(zip(group.row_position, group.value))
        rows = group[group.formula.notna()]

        for row in rows.itertuples():
            expression = expand_formula(row.formula)
            if expression is None:
                continue
            missing = [int(p) for p in re.findall(r"\d+", expression) if int(p) not in by_position]
            if missing:
                continue
            total = _evaluate(expression, by_position.get)
            if total is None:
                continue

            delta = abs(total - row.value)
            if delta <= max(tolerance, abs(row.value) * relative):
                continue

            gap_pct = 100 * delta / abs(row.value) if row.value else float("inf")
            allowed = KNOWN_IDENTITY_EXCEPTIONS.get((table_slug, row.entity_key))
            registered = allowed is not None and gap_pct <= allowed
            results.append(
                {
                    "dataset": table_slug,
                    "check": "row_identity",
                    "entity_key": row.entity_key,
                    "passed": registered,
                    "detail": (
                        f"{period} {metric}/{currency}: {row.formula} sums to {total:,.0f}, "
                        f"row states {row.value:,.0f} (delta {delta:,.0f}, {gap_pct:.2f}%)"
                        + (f"; registered publication inconsistency, within {allowed}%" if registered else "")
                    ),
                }
            )

    return results


def check_risk_weight_identity(observations: pd.DataFrame, table_slug: str,
                               tolerance_pct: float = RISK_WEIGHT_TOLERANCE_PCT) -> list:
    """Verify a parent against the risk weights its own children declare.

    Nothing else in the corpus looks at the sixteen risk-weight bucket rows of
    the capital-adequacy table: they carry no formula of their own, and their
    parent's stated formula addresses rows by position from the other direction.
    So a bucket silently dropped, duplicated, or attached to the wrong parent
    would pass every existing check.

    The rule is still read from the labels rather than hand-curated -- a child
    that says 'Risk Ağırlığı %75 Olan Kalemler Toplamı' is stating its weight:

        parent == sum(weight_i * child_i) + sum(child_j not stating a weight)

    Children that state no weight are already risk-weighted amounts and enter at
    face value; in this corpus that is 'KDA Riskine Esas Tutar' alone. That this
    reconstruction holds is also what proves KDA sits INSIDE the parent, which is
    why the parent's own stated formula double-counts it -- see
    `bulletin_tables.KNOWN_IDENTITY_EXCEPTIONS`.

    Applies to any table whose children declare weights; only table 12 does.
    """
    results = []
    children = observations[observations.parent_key.notna()]
    if children.empty:
        return results

    by_key = observations.set_index(["period", "metric", "currency", "entity_key"], drop=False)
    for (period, metric, currency, parent_key), group in children.groupby(
        ["period", "metric", "currency", "parent_key"], dropna=False
    ):
        weights = group.entity_name.str.extract(_RISK_WEIGHT)[0]
        if weights.notna().sum() == 0:
            continue

        weighted = (weights.astype(float).fillna(0) / 100 * group.value).sum()
        unweighted = group.value[weights.isna()].sum()
        total = weighted + unweighted

        try:
            parent = by_key.loc[(period, metric, currency, parent_key)]
        except KeyError:
            continue
        parent_value = float(parent.value if not hasattr(parent.value, "iloc") else parent.value.iloc[0])
        if not parent_value:
            continue

        gap_pct = 100 * abs(total - parent_value) / abs(parent_value)
        results.append(
            {
                "dataset": table_slug,
                "check": "risk_weight_identity",
                "entity_key": parent_key,
                "passed": gap_pct <= tolerance_pct,
                "detail": (
                    f"{period} {metric}/{currency}: {int(weights.notna().sum())} weighted bucket(s) "
                    f"+ {int(weights.isna().sum())} already-weighted row(s) reconstruct "
                    f"{total:,.0f}, parent states {parent_value:,.0f} ({gap_pct:.3f}%)"
                ),
            }
        )

    return results


def run_bulletin_validations(observations: pd.DataFrame, table_slug: str) -> pd.DataFrame:
    """Run every check for one table; raise if any fails."""
    results = (check_continuity(observations, table_slug)
               + check_identities(observations, table_slug)
               + check_risk_weight_identity(observations, table_slug))
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
