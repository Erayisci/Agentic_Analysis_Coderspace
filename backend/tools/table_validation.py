"""Explicit validation gate between extracted tables and numeric analysis.

The trusted application/user supplies the schema, unit and period after checking
the source. A language model is not allowed to certify its own extracted data.
This module writes nothing to the lakehouse and uses no optional dependencies.
"""

from decimal import Decimal, InvalidOperation
import re


def validate_extracted_table(evidence, section_index, *, expected_columns, numeric_columns,
                             unit, period, source_verified=False,
                             decimal_separator=".", thousands_separator=""):
    """Validate complete rows against an explicit schema; preserve decimal precision.

    Returns normalized numbers as decimal strings, together with source and units.
    Passing checks does not assert that the publisher's underlying data is true.
    """
    errors = []
    result = {"status": "error", "ready_for_calculation": False, "errors": errors,
              "rows": [], "unit": unit, "period": period,
              "validation_scope": "explicit source contract, table shape and numeric syntax"}
    if (not isinstance(evidence, dict) or type(section_index) is not int or section_index < 0
            or not isinstance(expected_columns, list) or not expected_columns
            or len(expected_columns) > 200 or any(not isinstance(c, str) or not c for c in expected_columns)
            or len(set(expected_columns)) != len(expected_columns)
            or not isinstance(numeric_columns, list) or not numeric_columns
            or any(c not in expected_columns for c in numeric_columns)
            or decimal_separator not in {".", ","}
            or thousands_separator not in {"", ",", ".", " "}
            or decimal_separator == thousands_separator):
        errors.append("Invalid table contract.")
        return result
    if source_verified is not True:
        errors.append("A trusted caller must verify the source, units and period first.")
    if not isinstance(unit, str) or not unit.strip() or not isinstance(period, str) or not period.strip():
        errors.append("Explicit units and report period are required.")
    if evidence.get("status") != "ok" or evidence.get("truncated") or evidence.get("processing_errors"):
        errors.append("Incomplete or failed extraction cannot be certified for calculations.")
    sections = evidence.get("sections", [])
    if not isinstance(sections, list) or section_index >= len(sections) or not isinstance(sections[section_index], dict):
        errors.append("The requested table section is missing.")
        return result
    section = sections[section_index]
    result.update({"source_url": evidence.get("final_url"), "fetched_at": evidence.get("fetched_at"),
                   "location": section.get("location"), "columns": expected_columns})
    if not result["source_url"] or not result["fetched_at"] or not result["location"]:
        errors.append("Source URL, fetch time and section location are required.")
    rows = section.get("rows")
    if (not isinstance(rows, list) or not 2 <= len(rows) <= 2000
            or any(not isinstance(row, list) or len(row) != len(expected_columns) for row in rows)):
        errors.append("The table must contain a header and rectangular data rows within limits.")
        return result
    if rows[0] != expected_columns:
        errors.append("Extracted headers do not match the explicit schema.")
    sep = re.escape(thousands_separator)
    integer = r"[0-9]+" if not sep else rf"(?:[0-9]+|[0-9]{{1,3}}(?:{sep}[0-9]{{3}})+)"
    pattern = re.compile(rf"[+-]?{integer}(?:{re.escape(decimal_separator)}[0-9]+)?")
    normalized = []
    for index, row in enumerate(rows[1:], 2):
        output = {}
        for column, value in zip(expected_columns, row):
            if not isinstance(value, str) or not value.strip():
                errors.append(f"Row {index}, column {column}: missing or non-text cell.")
                continue
            value = value.strip()
            if column in numeric_columns:
                if not pattern.fullmatch(value):
                    errors.append(f"Row {index}, column {column}: ambiguous or invalid numeric value.")
                    continue
                canonical = value.replace(thousands_separator, "") if thousands_separator else value
                try:
                    output[column] = str(Decimal(canonical.replace(decimal_separator, ".")))
                except InvalidOperation:
                    errors.append(f"Row {index}, column {column}: invalid number.")
            else:
                output[column] = value
        normalized.append(output)
        if len(errors) >= 20:
            errors.append("Further validation errors omitted.")
            break
    if not errors:
        result.update(status="ok", ready_for_calculation=True, rows=normalized)
    return result
