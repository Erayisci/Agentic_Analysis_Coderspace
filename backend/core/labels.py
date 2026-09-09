"""Stable keys for regulator row labels.

Both regulators publish tables whose rows are identified only by a Turkish
label, and those labels drift in ways that are cosmetic rather than semantic:

    'Ortaklık Finansmanı'          -> 'Ortaklık Finansmanı*'        (footnote added)
    "Menkul Değerler (2 den 24'e)" -> "Menkul Değerler (2 den 26'ya)"  (formula shifts
                                       as rows are inserted above)

Keying on the raw label therefore invents a new series every time BDDK adds a
footnote marker or a line item. Keying on row position is worse: three bulletin
tables reshuffle their rows mid-history (see CLAUDE.md).

`canonical_key` strips exactly the two decorations above and nothing else, so a
genuinely renamed line item still reads as a new series -- which is what we want
the continuity check to catch.
"""
import re
import unicodedata

TURKISH_TO_ASCII = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")

# A trailing '(...)' that states how the row is aggregated, in either of the two
# forms BDDK uses: '(2+3+4)' / '(10+27+28)' and "(2 den 26'ya)" / '(2 den 24 e)'.
_FORMULA = re.compile(
    r"\s*\(\s*(?:\d+(?:\s*[+\-]\s*\d+)+"
    r"|\d+\s*(?:den|dan)\s*\d+\s*['’]?\s*(?:ya|ye|a|e)\s*)\)\s*$"
)

# Footnote markers BDDK appends to a label to point at a table footnote.
_FOOTNOTE = re.compile(r"\s*[*†‡]+\s*$")


def strip_decorations(label: str) -> tuple:
    """Split a raw label into (name, formula, footnote).

    The formula and the footnote are metadata about the row, not part of its
    identity: the formula is a per-period validation rule and the footnote
    points at prose. Only the name may be used as a key.
    """
    text = str(label).strip()
    formula = ""
    footnote = ""

    # A label can carry both, in either order, so peel until stable.
    for _ in range(4):
        before = text
        match = _FOOTNOTE.search(text)
        if match:
            footnote = match.group().strip() + footnote
            text = text[: match.start()].rstrip()
        match = _FORMULA.search(text)
        if match:
            formula = match.group().strip() or formula
            text = text[: match.start()].rstrip()
        if text == before:
            break

    return text, formula, footnote


def slugify(name: str) -> str:
    """Stable ASCII key, e.g. 'Otel ve Restoranlar (Turizm)' -> 'otel_ve_restoranlar_turizm'."""
    text = str(name).strip().translate(TURKISH_TO_ASCII).lower()
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def canonical_key(label: str) -> str:
    """Slug of a label with its formula and footnote decorations removed."""
    name, _, _ = strip_decorations(label)
    return slugify(name)


def qualified_key(parent_key: str, own_key: str) -> str:
    """Compose a row key that is unique within one period.

    Bulletin tables 09, 10 and 11 repeat child labels under several parents --
    'a) Gerçek Kişiler' appears six times in the deposit tables, once per
    deposit type -- so a child's key is only unique when qualified by its
    parent. Parent rows keep their own bare key.
    """
    return f"{parent_key}/{own_key}" if parent_key else own_key
