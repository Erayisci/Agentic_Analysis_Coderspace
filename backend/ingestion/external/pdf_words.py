"""Tables from a text PDF's word positions, for pages with no ruled table.

Regulators' PDFs are mostly *typeset* tables: numbers right-aligned in
columns, no cell borders, a two- or three-line bilingual header, the year in
the title. pdfplumber's line-based `extract_table` returns nothing usable for
those (measured on Borsa İstanbul's gold trading report: a 13x1 fragment),
but every word carries its x-range, and that is enough:

1. group words into lines by their `top`;
2. a data line is one whose first token is text and which holds three or
   more numeric tokens; the line with the most numeric tokens is the template
   and its numeric tokens' x-ranges are the column anchors;
3. every other data line assigns each numeric token to the nearest anchor, so
   a short line ("Eylül / September 0") fills only the column it sits under;
4. the header is the block of lines directly above the data (stopping at the
   first vertical gap much larger than the data's line spacing); a column's
   label is every header word whose x-range overlaps the column's, joined top
   to bottom -- so "Hacim/Volume (TL)" and "Miktar/Amount (KG)" come out
   whole. Lines above that block are the caption (the title, with the year).

The result is the same `rows` grid every other extractor section carries.
"""
import re
import statistics
from io import BytesIO
from typing import List, Optional, Tuple

_NUMBER = re.compile(r"^[-+−]?\(?\d[\d.,]*\)?%?$")
LINE_TOLERANCE = 3.0
MIN_DATA_LINES = 3
MIN_NUMERIC_TOKENS = 3


def _is_number(token: str) -> bool:
    return bool(_NUMBER.match(token)) and any(ch.isdigit() for ch in token)


def _lines(words) -> List[Tuple[float, list]]:
    ordered = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines: List[Tuple[float, list]] = []
    for word in ordered:
        if lines and abs(lines[-1][0] - word["top"]) <= LINE_TOLERANCE:
            lines[-1][1].append(word)
        else:
            lines.append((word["top"], [word]))
    return [(top, sorted(ws, key=lambda w: w["x0"])) for top, ws in lines]


def _centre(word) -> float:
    return (word["x0"] + word["x1"]) / 2


def page_grid(page) -> Optional[Tuple[List[List[str]], str]]:
    """(rows, caption) for one pdfplumber page, or None when it holds no table."""
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    if not words:
        return None
    lines = _lines(words)

    data_indices = [i for i, (_, ws) in enumerate(lines)
                    if ws and not _is_number(ws[0]["text"])
                    and sum(_is_number(w["text"]) for w in ws) >= MIN_NUMERIC_TOKENS]
    if len(data_indices) < MIN_DATA_LINES:
        return None

    template = max(data_indices, key=lambda i: sum(_is_number(w["text"]) for w in lines[i][1]))
    anchors = [[w["x0"], w["x1"]] for w in lines[template][1] if _is_number(w["text"])]
    # Widen each anchor with what the other data lines put under it.
    for i in data_indices:
        for word in lines[i][1]:
            if not _is_number(word["text"]):
                continue
            nearest = min(range(len(anchors)), key=lambda k: abs(_centre(word) - (anchors[k][0] + anchors[k][1]) / 2))
            anchors[nearest][0] = min(anchors[nearest][0], word["x0"])
            anchors[nearest][1] = max(anchors[nearest][1], word["x1"])

    # Column bounds: half-way to the neighbouring anchor on each side.
    bounds = []
    for k, (x0, x1) in enumerate(anchors):
        left = (anchors[k - 1][1] + x0) / 2 if k else x0 - 20
        right = (x1 + anchors[k + 1][0]) / 2 if k + 1 < len(anchors) else x1 + 20
        bounds.append((left, right))

    def column_of(word) -> Optional[int]:
        centre = _centre(word)
        for k, (left, right) in enumerate(bounds):
            if left <= centre <= right:
                return k
        return None

    rows: List[List[str]] = []
    for i in data_indices:
        cells = [""] * len(anchors)
        label = []
        for word in lines[i][1]:
            if _is_number(word["text"]):
                k = column_of(word)
                if k is not None:
                    cells[k] = (cells[k] + " " + word["text"]).strip()
            elif not any(cells):
                label.append(word["text"])
        rows.append([" ".join(label)] + cells)

    # The header block: lines directly above the first data line, separated
    # from the caption by a gap clearly larger than the data's line spacing.
    tops = [lines[i][0] for i in data_indices]
    spacing = statistics.median([b - a for a, b in zip(tops, tops[1:])]) if len(tops) > 1 else 12.0
    header_lines = []
    cursor = data_indices[0]
    previous_top = lines[cursor][0]
    for i in range(cursor - 1, -1, -1):
        top = lines[i][0]
        if previous_top - top > max(1.4 * spacing, 14.0):
            break
        header_lines.insert(0, lines[i][1])
        previous_top = top
    caption_lines = lines[:cursor - len(header_lines)]

    labels = ["" for _ in anchors]
    first_label = []
    for ws in header_lines:
        for word in ws:
            k = column_of(word)
            if k is None:
                if word["x1"] <= bounds[0][0]:
                    first_label.append(word["text"])
                continue
            labels[k] = (labels[k] + " " + word["text"]).strip()
    header = [" ".join(first_label) or "label"] + [label or f"col_{k + 2}" for k, label in enumerate(labels)]
    caption = " ".join(w["text"] for _, ws in caption_lines for w in ws)
    return [header] + rows, caption


def grids_from_pdf(content: bytes, max_pages: int = 50) -> List[dict]:
    """Extractor-shaped sections (`method="pdf_words"`) for every page that
    yields a grid. Pages are named as the extractor names them."""
    import pdfplumber
    sections = []
    with pdfplumber.open(BytesIO(content)) as pdf:
        for index, page in enumerate(pdf.pages[:max_pages]):
            try:
                found = page_grid(page)
            except Exception:                                    # noqa: BLE001 -- one page, not the document
                found = None
            if not found:
                continue
            rows, caption = found
            sections.append({"location": f"Page {index + 1}, words", "method": "pdf_words",
                             "rows": rows, "text": caption})
    return sections
