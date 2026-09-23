"""Grids from OCR output and from HTML tables.

MIA's `Unlimited-OCR` (measured on the Borsa İstanbul gold report rendered to
PNG, 1.7 s) returns layout tags -- `<|det|>title [x, y, x, y]<|/det|>` -- and
each table as HTML with `rowspan`/`colspan`, so the same span-aware
table-to-grid routine serves OCR output and static HTML pages. Text outside
the tables (the title, with its year) becomes the caption.
"""
import re
from typing import List, Tuple

from bs4 import BeautifulSoup

_LAYOUT_TAG_RE = re.compile(r"<\|/?(?:det|ref)\|>(?:[a-z_]+ \[[\d, ]+\]<\|/(?:det|ref)\|>)?", re.I)
_MARKDOWN_RULE_RE = re.compile(r"^:?-{2,}:?$")


def strip_layout_tags(text: str) -> str:
    return _LAYOUT_TAG_RE.sub("\n", text or "")


def html_tables_to_grids(html: str) -> List[List[List[str]]]:
    """Every <table> as a rectangular grid of strings, spans expanded: a
    `colspan=3` header cell is repeated across its three columns and a
    `rowspan=2` label is repeated down, which is what header detection expects."""
    soup = BeautifulSoup(html or "", "html.parser")
    grids = []
    for table in soup.find_all("table"):
        occupied: dict = {}
        rows_out: List[List[str]] = []
        for r, tr in enumerate(table.find_all("tr")):
            c = 0
            for cell in tr.find_all(["td", "th"]):
                while (r, c) in occupied:
                    c += 1
                text = cell.get_text(" ", strip=True)
                rowspan = _span(cell.get("rowspan"))
                colspan = _span(cell.get("colspan"))
                for dr in range(rowspan):
                    for dc in range(colspan):
                        occupied[(r + dr, c + dc)] = text
                c += colspan
            rows_out.append([])
        if not occupied:
            continue
        height = max(r for r, _ in occupied) + 1
        width = max(c for _, c in occupied) + 1
        grid = [[occupied.get((r, c), "") for c in range(width)] for r in range(height)]
        grid = [row for row in grid if any(cell for cell in row)]
        if len(grid) >= 2:
            grids.append(grid)
    return grids


def _span(value) -> int:
    try:
        return max(1, min(int(value or 1), 50))
    except (TypeError, ValueError):
        return 1


def _markdown_or_tab_grid(lines: List[str]) -> List[List[List[str]]]:
    piped = [line for line in lines if line.count("|") >= 2]
    if len(piped) >= 4:
        rows = []
        for line in piped:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(_MARKDOWN_RULE_RE.match(c) or c == "" for c in cells):
                continue
            rows.append(cells)
        return [rows] if len(rows) >= 2 else []
    tabbed = [line for line in lines if line.count("\t") >= 1]
    if len(tabbed) >= 4:
        return [[line.split("\t") for line in tabbed]]
    return []


def grids_from_ocr_text(text: str) -> Tuple[List[List[List[str]]], str]:
    """(grids, caption) from one OCR/text section. HTML tables first, then
    markdown or tab-separated ones; the caption is the tag-free text outside
    the tables."""
    cleaned = strip_layout_tags(text)
    grids = html_tables_to_grids(cleaned) if "<table" in cleaned.lower() else []
    outside = re.sub(r"<table.*?</table>", " ", cleaned, flags=re.I | re.S)
    outside = BeautifulSoup(outside, "html.parser").get_text(" ", strip=True)
    if not grids:
        grids = _markdown_or_tab_grid([line.strip() for line in cleaned.splitlines() if line.strip()])
        if grids:
            outside = " ".join(line for line in cleaned.splitlines() if "|" not in line and "\t" not in line)
    return grids, re.sub(r"\s+", " ", outside).strip()[:500]
