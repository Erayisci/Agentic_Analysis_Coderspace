"""Stable keys for regulator row labels.

Both regulators publish tables whose rows are identified only by a Turkish
label, and those labels carry three kinds of decoration that are metadata
about the row rather than part of its identity:

    'Ortaklık Finansmanı'          -> 'Ortaklık Finansmanı*'        (footnote added)
    "Menkul Değerler (2 den 24'e)" -> "Menkul Değerler (2 den 26'ya)"  (formula shifts
                                       as rows are inserted above)
    'Toplam KOBİ Niteliğindeki Müşteri Sayısı (6+7+8) (Adet)'      (row states its unit)

Keying on the raw label therefore invents a new series every time BDDK adds a
footnote marker or a line item. Keying on row position is worse: three bulletin
tables reshuffle their rows mid-history (see CLAUDE.md).

`canonical_key` strips exactly these three and nothing else, so a genuinely
renamed line item still reads as a new series -- which is what we want the
continuity check to catch. The guards that keep the stripping narrow:

    formula  the trailing group must be free of letters and must contain an
             operator, so '(Net)' and a hypothetical '(2023)' are both kept
    unit     only the six markers actually measured across the 17 tables are
             recognised; '(Sukuk)', '(Dövize Endeksli)' and friends are names
"""
import re
import unicodedata

TURKISH_TO_ASCII = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")

# Footnote markers BDDK appends to a label to point at a table footnote.
_FOOTNOTE = re.compile(r"\s*[*†‡]+\s*$")

# A bracketed group, allowing one level of nesting so '((6/7)*100)' is one
# group and '[(26+34+50)-45]' is one group.
_GROUP = r"(?:\[[^\[\]]*\]|\((?:[^()]|\([^()]*\))*\))"

# A trailing arithmetic expression: one or more groups chained by + or -, as in
# '(1+...+14)-(2+3+4+5)'. Validated afterwards, because the pattern alone would
# also match '(Net)'.
_FORMULA = re.compile(rf"\s*({_GROUP}(?:\s*[+\-]\s*(?:{_GROUP}|\d+))*)\s*$")
_ARITHMETIC_ONLY = re.compile(r"^[\[\]()\d+\-*/.…'’\s]+$")
_HAS_OPERATOR = re.compile(r"[+\-*/]|\.{2,}|…")

# The other form BDDK uses for an aggregation: "(2 den 26'ya)", '(2 den 24 e)'.
_RANGE = re.compile(
    r"\s*(\(\s*\d+\s*(?:den|dan)\s*\d+\s*['’]?\s*(?:ya|ye|a|e)\s*\))\s*$"
)

# Trailing groups that state the row's own unit instead of naming it. Measured
# across all 17 tables at 2026-06: only these appear, in tables 6, 12, 13 and
# 15. Everything else in a trailing group -- '(Net)', '(Zararı)', '(Sukuk)',
# '(Dövize Endeksli)' -- is part of the name and stays in the key.
UNIT_MARKERS = {
    "%": "%",
    "yüzde": "%",
    "adet": "adet",
    "bin tl": "bin TL",
    "gün": "gün",
    "kişi": "kişi",
}
_UNIT = re.compile(r"\s*\(([^()]*)\)\s*$")


def _peel_footnote(text: str):
    match = _FOOTNOTE.search(text)
    if not match:
        return text, None
    return text[: match.start()].rstrip(), match.group().strip()


def _peel_unit(text: str):
    match = _UNIT.search(text)
    if not match:
        return text, None
    unit = UNIT_MARKERS.get(match.group(1).strip().lower())
    if unit is None:
        return text, None
    return text[: match.start()].rstrip(), unit


def _peel_formula(text: str):
    match = _RANGE.search(text)
    if match:
        return text[: match.start()].rstrip(), match.group(1).strip()

    match = _FORMULA.search(text)
    if not match:
        return text, None
    body = match.group(1).strip()
    if not (_ARITHMETIC_ONLY.match(body) and _HAS_OPERATOR.search(body) and re.search(r"\d", body)):
        return text, None
    return text[: match.start()].rstrip(), body


def strip_decorations(label: str) -> tuple:
    """Split a raw label into (name, formula, footnote, unit).

    The formula is a per-period validation rule, the footnote points at prose
    and the unit belongs in the unit column. Only the name may be used as a key.
    """
    text = str(label).strip()
    formula = footnote = unit = None

    # A label can carry all three in either order, so peel until stable.
    for _ in range(6):
        before = text
        text, found = _peel_footnote(text)
        footnote = (found + footnote) if (found and footnote) else (found or footnote)
        text, found = _peel_unit(text)
        unit = found or unit
        text, found = _peel_formula(text)
        formula = found or formula
        if text == before:
            break

    return text, formula or "", footnote or "", unit


def ascii_fold(text: str) -> str:
    """Lowercase ASCII, punctuation and spacing preserved.

    `slugify` collapses everything to underscores, which is right for a key and
    wrong for search: discovery scores substrings against a published *name*
    ('Takipteki Tüketici Krd.') and needs it folded the same way the query is,
    without losing the word boundaries. Turkish is why this cannot be
    `str.lower()` -- 'İ'.lower() is not 'i', so a name carrying one silently
    fails to match an ASCII query term. The transliteration runs before the
    lowercase for the same reason.
    """
    folded = str(text or "").translate(TURKISH_TO_ASCII).lower()
    return unicodedata.normalize("NFKD", folded).encode("ascii", "ignore").decode("ascii")


def slugify(name: str) -> str:
    """Stable ASCII key, e.g. 'Otel ve Restoranlar (Turizm)' -> 'otel_ve_restoranlar_turizm'."""
    text = str(name).strip().translate(TURKISH_TO_ASCII).lower()
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def fold(text: str) -> str:
    """ASCII-lowercase for searching, with spacing and punctuation left alone.

    `slugify` exists because a key must be a stable identifier; this exists
    because a *search* must match across the same alphabet gap without becoming
    one. Turkish case folding is the reason both are needed: 'İ'.lower() is not
    'i', so a question written "TGA orani" does not contain, and is not
    contained by, a published "oranı" -- and a ranker comparing the two finds
    nothing while looking like it searched. Measured: this alone is why the
    published NPL ratio, whose row says "oranı" in every spelling a user would
    reach for, lost to a balance-sheet stock that says nothing of the kind.
    """
    text = str(text).translate(TURKISH_TO_ASCII).lower()
    # The circumflex is not in TURKISH_TO_ASCII and is not a Turkish letter:
    # BDDK writes "Dönem Net Kârı" and a question writes "kari". Decomposing
    # and dropping the combining marks is what `slugify` already does one line
    # later, for the same reason.
    return "".join(ch for ch in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(ch))


def canonical_key(label: str) -> str:
    """Slug of a label with its formula, footnote and unit decorations removed."""
    name, _, _, _ = strip_decorations(label)
    return slugify(name)


def qualified_key(parent_key: str, own_key: str) -> str:
    """Compose a row key that is unique within one period.

    Bulletin tables 09, 10 and 11 repeat child labels under several parents --
    'a) Gerçek Kişiler' appears six times in the deposit tables, once per
    deposit type -- so a child's key is only unique when qualified by its
    parent. Parent rows keep their own bare key.
    """
    return f"{parent_key}/{own_key}" if parent_key else own_key
