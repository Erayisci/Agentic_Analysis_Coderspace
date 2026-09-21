"""Validate BIST's annual gold-trading PDF before exposing numeric columns.

This is an explicit source adapter, not a guess at arbitrary PDF tables. The
published bilingual title, year, currency order, units and row width must match.
Blank future months and the annual total are not monthly observations.
"""
import hashlib
from io import BytesIO
import re

import pandas as pd
from pypdf import PdfReader

COLUMNS = ["tl_volume", "tl_quantity_kg", "tl_transactions", "usd_volume", "usd_quantity_kg",
           "usd_transactions", "eur_volume", "eur_quantity_kg", "eur_transactions", "total_quantity_kg"]
UNITS = dict(zip(COLUMNS, ["TL", "kg", "adet", "USD", "kg", "adet", "EUR", "kg", "adet", "kg"]))
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
ROW = re.compile(r"^\s*\S+\s*/\s*(" + "|".join(MONTHS) + r")\b(.*)$", re.I)
NUMBER = re.compile(r"(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?$")


def parse_gold_pdf(content: bytes) -> pd.DataFrame:
    reader = PdfReader(BytesIO(content))
    if len(reader.pages) != 1:
        raise ValueError("Unsupported PDF table: expected the single-page annual BIST gold report")
    text = reader.pages[0].extract_text(extraction_mode="layout") or ""
    titles = re.findall(r"PRECIOUS METALS MARKET GOLD TRADING DATA\s*\((20\d{2})\)", text)
    if len(titles) != 1:
        raise ValueError("Unsupported PDF table: annual gold-trading title/year not found")
    year = int(titles[0])
    if not re.search(r"AY\s*/\s*MONTH\s+TL\s+USD\s+EUR\s+TOPLAM\s*/\s*TOTAL", text):
        raise ValueError("Gold PDF currency order/header changed")
    for header in ("Hacim/Volume (TL)", "Hacim/Volume (USD)", "(EUR)"):
        if header not in text:
            raise ValueError("Gold PDF volume units changed")
    if text.count("(KG)") != 4 or text.count("Number of Trans.") != 3:
        raise ValueError("Gold PDF quantity/count headers changed")
    rows, seen = [], set()
    for line in text.splitlines():
        match = ROW.match(line)
        if not match:
            continue
        month = MONTHS.index(match[1].capitalize()) + 1
        if month in seen:
            raise ValueError("Gold PDF contains duplicate months")
        seen.add(month)
        tokens = match[2].split()
        # The source prints a total zero alongside otherwise blank future months.
        if tokens in ([], ["0"]):
            continue
        if len(tokens) != len(COLUMNS) or any(not NUMBER.fullmatch(token) for token in tokens):
            raise ValueError(f"Gold PDF month {month}: incomplete or ambiguous numeric row")
        values = [float(token.replace(".", "").replace(",", ".")) for token in tokens]
        if any(values[i] != int(values[i]) for i in (2, 5, 8)):
            raise ValueError("Gold PDF transaction counts are not integers")
        # Quantities are independently rounded to kilograms in the publication.
        if abs(sum(values[i] for i in (1, 4, 7)) - values[9]) > 2:
            raise ValueError("Gold PDF quantity columns do not reconcile with its total")
        rows.append({"period": f"{year}-{month:02d}-01", **dict(zip(COLUMNS, values))})
    if seen != set(range(1, 13)) or not rows:
        raise ValueError("Gold PDF monthly grid is incomplete")
    frame = pd.DataFrame(rows)
    frame.attrs["source_metadata"] = {
        "format": "pdf", "parser": "bist_gold_v1", "document_year": year, "page": 1,
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "units": UNITS, "temporal_semantics": "flow",
        "validation": "title_year_headers_row_width_numeric_types_quantity_totals",
    }
    return frame
