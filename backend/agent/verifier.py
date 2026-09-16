"""Checks a finished turn before anything is said about it.

The verifier is the reason the system can claim its answers are trustworthy
rather than merely plausible. It runs after execution and before composition,
so a problem is reported as a caveat the narrative must carry -- or suppresses
the claim entirely -- instead of surfacing as a confidently wrong sentence.

Every check is arithmetic or structural. None of them ask a model anything.
"""
import re
from typing import Any, Dict, List


from .state import Session

# A column with more missing periods than this is reported as incomplete: an
# average or a "change since 2021" over a half-empty column is misleading even
# when every individual number in it is right.
MAX_MISSING_SHARE = 0.10


def verify(session: Session) -> Dict[str, Any]:
    """Run every check. Returns a report; never raises."""
    checks: List[Dict[str, Any]] = []
    artifact = session.artifact

    def record(name: str, passed: bool, detail: str, severity: str = "error") -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail,
                       "severity": severity if not passed else "info"})

    failed_steps = [a for a in session.audit if not a.ok]
    record("all_steps_ran", not failed_steps,
           "; ".join(f"{a.op}: {a.detail}" for a in failed_steps) or "every step completed",
           severity="warning")

    if artifact.is_empty():
        record("table_has_data", False, "no columns were produced")
        return _summarise(checks, session)

    record("table_has_data", True, f"{len(artifact.frame)} rows, {len(artifact.frame.columns)} columns")

    # Every column must be describable and quotable, or the composer cannot
    # honestly report it.
    unlabelled = [c for c in artifact.frame.columns
                  if c not in artifact.lineage or not artifact.lineage[c].unit]
    record("every_column_has_a_unit", not unlabelled,
           f"columns without a unit: {unlabelled}" if unlabelled else "all columns carry a unit")

    uncited = [c for c, line in artifact.lineage.items()
               if not line.citation and line.source != "derived"]
    record("every_fetched_column_is_cited", not uncited,
           f"uncited: {uncited}" if uncited else "every fetched column has provenance")

    # A derived column whose parents are gone is a number nobody can explain.
    orphans = [c for c, line in artifact.lineage.items()
               if line.derived_from and any(p not in artifact.frame.columns for p in line.derived_from)]
    record("derived_columns_keep_their_inputs", not orphans,
           f"derived columns whose inputs were dropped: {orphans}" if orphans else "lineage intact")

    empty = [c for c in artifact.frame.columns if artifact.frame[c].dropna().empty]
    record("no_empty_columns", not empty, f"all-null columns: {empty}" if empty else "no empty columns")

    incomplete = {c: round(float(artifact.frame[c].isna().mean()), 3)
                  for c in artifact.frame.columns
                  if artifact.frame[c].isna().mean() > MAX_MISSING_SHARE}
    record("coverage_is_complete", not incomplete,
           f"columns with gaps: {incomplete}" if incomplete else "no material gaps",
           severity="warning")

    # Mixing units in one comparison is the single most likely way this corpus
    # produces a confidently wrong answer -- bin TL and milyon TL differ by 1000x.
    monetary = {c: artifact.lineage[c].unit for c in artifact.frame.columns
                if "TL" in (artifact.lineage[c].unit or "")}
    distinct = set(monetary.values())
    record("monetary_columns_share_one_unit", len(distinct) <= 1,
           f"mixed monetary units in one table: {monetary}" if len(distinct) > 1
           else f"monetary unit: {distinct or 'none'}")

    # A stock differenced and described as "new lending" is a domain error no
    # arithmetic check would catch, so the semantics are surfaced explicitly.
    record("temporal_semantics_declared",
           all(artifact.lineage[c].temporal_semantics for c in artifact.frame.columns),
           ", ".join(f"{c}={artifact.lineage[c].temporal_semantics}" for c in artifact.frame.columns))

    index = artifact.frame.index
    record("periods_are_unique_and_sorted",
           index.is_unique and index.is_monotonic_increasing,
           f"{index.min():%Y-%m}..{index.max():%Y-%m}" if len(index) else "empty index")

    return _summarise(checks, session)


def _summarise(checks: List[Dict[str, Any]], session: Session) -> Dict[str, Any]:
    errors = [c for c in checks if not c["passed"] and c["severity"] == "error"]
    warnings = [c for c in checks if not c["passed"] and c["severity"] == "warning"]
    report = {
        "passed": not errors,
        "n_checks": len(checks),
        "n_errors": len(errors),
        "n_warnings": len(warnings),
        "checks": checks,
        "caveats": [c["detail"] for c in errors + warnings],
    }
    session.facts["verification"] = report
    return report


def quotable_numbers(session: Session) -> Dict[str, Any]:
    """The only numbers the composer is allowed to state.

    Handing the model a computed summary rather than the table is what makes
    "the model never does arithmetic" enforceable rather than aspirational: a
    figure that is not in here did not come from the data.
    """
    artifact = session.artifact
    allowed: Dict[str, Any] = {"series": artifact.summary()}
    if session.facts.get("find_periods"):
        allowed["find_periods"] = session.facts["find_periods"]
    if session.facts.get("analysis"):
        allowed["analysis"] = session.facts["analysis"]
    if session.facts.get("documents"):
        allowed["documents"] = [
            {k: v for k, v in doc.items() if k in ("kind", "url", "title", "n_pages", "sheet_names", "text")}
            for doc in session.facts["documents"]]
    if session.facts.get("search"):
        allowed["search"] = session.facts["search"]
    if session.facts.get("discovery"):
        allowed["discovered_keys"] = [
            {"key": c["key"], "name": c["name"], "source": c["source"], "unit": c["unit"]}
            for result in session.facts["discovery"] for c in result["candidates"][:5]]
    return allowed


def numbers_in_text(text: str) -> List[float]:
    """Every number a narrative states, for the unsupported-claim check."""
    out = []
    for token in re.findall(r"-?\d[\d.,]*", text or ""):
        cleaned = token.rstrip(".,")
        # Turkish notation: 1.234,56 -- thousands grouped with dots, decimal
        # comma. "678.970" therefore means 678970, and reading it as 678.97
        # made every correctly-quoted large figure look unsupported.
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", cleaned):
            cleaned = cleaned.replace(".", "")
        try:
            out.append(float(cleaned))
        except ValueError:
            continue
    return out


def unsupported_numbers(text: str, allowed: Dict[str, Any], tolerance: float = 0.02) -> List[float]:
    """Numbers in the narrative that match nothing the tools computed.

    Years and small integers are ignored -- "2021" and "60 ay" are structure,
    not claims. Everything else must appear in the computed facts within a
    tolerance, since a model may round 245.34 to 245.3 legitimately.
    """
    import json as _json

    def harvest(node, into):
        if isinstance(node, dict):
            for value in node.values():
                harvest(value, into)
        elif isinstance(node, list):
            for value in node:
                harvest(value, into)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            into.append(float(node))
        elif isinstance(node, str):
            for token in numbers_in_text(node):
                into.append(token)

    known: List[float] = []
    harvest(_json.loads(_json.dumps(allowed, default=str)), known)

    unsupported = []
    for value in numbers_in_text(text):
        if abs(value) < 100 and float(value).is_integer():
            continue                                   # counts, lags, small integers
        if 1900 <= value <= 2100 and float(value).is_integer():
            continue                                   # years
        if any(abs(value - k) <= max(tolerance * abs(k), 0.05) for k in known):
            continue
        unsupported.append(value)
    return unsupported
