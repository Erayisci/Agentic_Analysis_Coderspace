"""Tests for backend.tools.external_series -- no real network calls.

Fetching is mocked the same way tests/test_web_url.py mocks it (a fake
requests.Response via web_url._fetch), because this module deliberately
reuses web_url's fetch/SSRF path rather than a second implementation.
"""
from io import BytesIO
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.tools import external_series as E
from backend.tools import web_url


def _excel_bytes(frame: pd.DataFrame, sheet_name: str = "Sheet1") -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name=sheet_name, index=False)
    return buf.getvalue()


def _fake_response(content_type: str, content: bytes) -> SimpleNamespace:
    return SimpleNamespace(headers={"Content-Type": content_type}, content=content)


def _mock_fetch(monkeypatch, content_type: str, content: bytes):
    monkeypatch.setattr(web_url, "_fetch", lambda url: _fake_response(content_type, content))


MONTHLY_FRAME = pd.DataFrame({
    "Tarih": pd.date_range("2021-01-01", periods=6, freq="MS"),
    "Fiyat (USD)": [1800.0, 1810.5, 1795.0, 1820.0, 1830.0, 1825.5],
    "Not": ["a", "b", "c", "d", "e", "f"],
})


# --- basic ingestion, excel and csv -----------------------------------------

def test_ingests_an_excel_column_with_auto_detected_period(monkeypatch):
    _mock_fetch(
        monkeypatch, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _excel_bytes(MONTHLY_FRAME),
    )

    result = E.ingest_external_series("https://example.com/gold.xlsx", value_column="Fiyat (USD)")

    assert result.source == "external"
    assert result.period_column == "Tarih"
    assert result.value_column == "Fiyat (USD)"
    assert len(result.values) == 6
    assert result.values.iloc[0] == 1800.0
    assert result.unit == "Fiyat (USD)"  # best-effort default: the raw header


def test_ingests_a_csv_column(monkeypatch):
    csv_bytes = MONTHLY_FRAME.to_csv(index=False).encode("utf-8")
    _mock_fetch(monkeypatch, "text/csv", csv_bytes)

    result = E.ingest_external_series("https://example.com/gold.csv", value_column="Fiyat (USD)")

    assert len(result.values) == 6
    assert result.values.iloc[-1] == 1825.5


def test_value_column_match_is_case_insensitive(monkeypatch):
    _mock_fetch(
        monkeypatch, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _excel_bytes(MONTHLY_FRAME),
    )

    result = E.ingest_external_series("https://example.com/gold.xlsx", value_column="fiyat (usd)")

    assert result.value_column == "Fiyat (USD)"


def test_explicit_period_column_is_honoured(monkeypatch):
    frame = MONTHLY_FRAME.rename(columns={"Tarih": "Ay"})
    _mock_fetch(
        monkeypatch, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _excel_bytes(frame),
    )

    result = E.ingest_external_series(
        "https://example.com/gold.xlsx", value_column="Fiyat (USD)", period_column="Ay")

    assert result.period_column == "Ay"
    assert len(result.values) == 6


def test_unit_hint_overrides_the_default(monkeypatch):
    _mock_fetch(
        monkeypatch, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _excel_bytes(MONTHLY_FRAME),
    )

    result = E.ingest_external_series(
        "https://example.com/gold.xlsx", value_column="Fiyat (USD)", unit="USD/ons")

    assert result.unit == "USD/ons"


def test_excel_sheet_selection(monkeypatch):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame({"Tarih": ["2021-01-01"], "V": [1]}).to_excel(writer, sheet_name="Wrong", index=False)
        MONTHLY_FRAME.to_excel(writer, sheet_name="Right", index=False)
    _mock_fetch(
        monkeypatch, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buf.getvalue())

    result = E.ingest_external_series(
        "https://example.com/gold.xlsx", value_column="Fiyat (USD)", sheet="Right")

    assert len(result.values) == 6


def test_sheet_argument_rejected_for_csv(monkeypatch):
    _mock_fetch(monkeypatch, "text/csv", MONTHLY_FRAME.to_csv(index=False).encode())

    with pytest.raises(ValueError, match="no sheets"):
        E.ingest_external_series("https://example.com/gold.csv", value_column="Fiyat (USD)", sheet="Sheet1")


# --- monthly resampling ------------------------------------------------------

def test_monthly_rule_last_takes_the_final_row_in_each_month(monkeypatch):
    frame = pd.DataFrame({
        "Tarih": pd.to_datetime(["2021-01-05", "2021-01-20", "2021-02-10"]),
        "V": [10.0, 20.0, 30.0],
    })
    _mock_fetch(
        monkeypatch, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _excel_bytes(frame),
    )

    result = E.ingest_external_series("https://example.com/x.xlsx", value_column="V", monthly_rule="last")

    assert list(result.values) == [20.0, 30.0]


def test_monthly_rule_avg_and_sum_differ_from_last(monkeypatch):
    frame = pd.DataFrame({
        "Tarih": pd.to_datetime(["2021-01-05", "2021-01-20"]),
        "V": [10.0, 20.0],
    })
    _mock_fetch(
        monkeypatch, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _excel_bytes(frame),
    )

    avg_result = E.ingest_external_series("https://example.com/x.xlsx", value_column="V", monthly_rule="avg")
    sum_result = E.ingest_external_series("https://example.com/x.xlsx", value_column="V", monthly_rule="sum")

    assert avg_result.values.iloc[0] == 15.0
    assert sum_result.values.iloc[0] == 30.0


# --- error paths --------------------------------------------------------------

def test_missing_value_column_lists_what_exists(monkeypatch):
    _mock_fetch(
        monkeypatch, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _excel_bytes(MONTHLY_FRAME),
    )

    with pytest.raises(ValueError, match="value_column.*not found"):
        E.ingest_external_series("https://example.com/gold.xlsx", value_column="Does Not Exist")


def test_missing_period_column_lists_what_exists(monkeypatch):
    _mock_fetch(
        monkeypatch, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _excel_bytes(MONTHLY_FRAME),
    )

    with pytest.raises(ValueError, match="period_column.*not found"):
        E.ingest_external_series(
            "https://example.com/gold.xlsx", value_column="Fiyat (USD)", period_column="Bogus")


def test_no_date_like_column_is_refused_rather_than_guessed(monkeypatch):
    frame = pd.DataFrame({"A": ["x", "y", "z"], "B": [1, 2, 3]})
    _mock_fetch(
        monkeypatch, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _excel_bytes(frame),
    )

    with pytest.raises(ValueError, match="could not find a date-like column"):
        E.ingest_external_series("https://example.com/x.xlsx", value_column="B")


def test_non_tabular_content_is_refused(monkeypatch):
    _mock_fetch(monkeypatch, "text/html", b"<html>not a table</html>")

    with pytest.raises(ValueError, match="not a tabular"):
        E.ingest_external_series("https://example.com/page", value_column="V")


def test_empty_file_is_refused(monkeypatch):
    empty = pd.DataFrame({"Tarih": [], "V": []})
    _mock_fetch(
        monkeypatch, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _excel_bytes(empty),
    )

    with pytest.raises(ValueError, match="no rows"):
        E.ingest_external_series("https://example.com/x.xlsx", value_column="V")


def test_non_numeric_or_undated_rows_are_dropped_not_fatal(monkeypatch):
    """A handful of bad rows in an otherwise clean, larger file must not stop
    the date column from being recognised (kept well above the 90% parse-rate
    bar), and the bad rows themselves must simply be dropped, not fatal."""
    dates = pd.date_range("2021-01-01", periods=20, freq="MS").tolist()
    dates[5] = pd.NaT
    values = [float(i) for i in range(20)]
    values[10] = None
    frame = pd.DataFrame({"Tarih": dates, "V": values})
    _mock_fetch(
        monkeypatch, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _excel_bytes(frame),
    )

    result = E.ingest_external_series("https://example.com/x.xlsx", value_column="V")

    assert result.period_column == "Tarih"
    assert len(result.values) == 18  # 20 rows minus the one NaT minus the one missing value


# --- SSRF guard is genuinely shared, not re-implemented ----------------------

def test_refuses_a_private_address_exactly_like_web_url(monkeypatch):
    """No mocked _fetch here: this hits the real web_url._fetch to prove the
    SSRF guard is actually inherited, not merely assumed."""
    import socket

    monkeypatch.setattr(
        web_url.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("10.0.0.5", 0))])

    with pytest.raises(ValueError, match="non-public address"):
        E.ingest_external_series("http://internal.example/x.xlsx", value_column="V")


# --- citation shape -----------------------------------------------------------

def test_citation_marks_the_unit_as_unverified(monkeypatch):
    _mock_fetch(
        monkeypatch, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _excel_bytes(MONTHLY_FRAME),
    )

    result = E.ingest_external_series("https://example.com/gold.xlsx", value_column="Fiyat (USD)")
    citation = result.citation()

    assert citation["table"] == "external"
    assert citation["url"] == "https://example.com/gold.xlsx"
    assert citation["unit_verified"] is False
    assert citation["n_points"] == 6
