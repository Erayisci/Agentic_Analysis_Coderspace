"""Best-effort unit and temporal-semantics labels for an external series.

The lakehouse's own sources declare both (a bulletin caption states its unit,
the EVDS registry states semantics per group). An external file states
neither in a machine-readable way, so these are read from the column header
and the table caption with the same markers `core.labels` recognises, and the
result is labelled as a heuristic: `unit_verified=False`,
`semantics_source="heuristic"`. The verifier turns that into a caveat; the
Phase D cross-check against the base corpus is what upgrades it.
"""
import re
from typing import Tuple

from ...core.labels import strip_decorations

# Ordered: the first pattern that matches wins, so "milyon TL" is tried before "TL".
UNIT_PATTERNS = [
    (r"milyar\s*(tl|₺)", "milyar TL"),
    (r"milyon\s*(tl|₺)", "milyon TL"),
    (r"\bbin\s*(tl|₺)", "bin TL"),
    (r"\bmilyar\s*(abd\s*)?dolar|\bbillion\s*usd", "milyar ABD doları"),
    (r"\bmilyon\s*(abd\s*)?dolar|\bmillion\s*usd", "milyon ABD doları"),
    (r"\bbin\s*ki[şs]i", "bin kişi"),
    (r"\btl\b|₺|t[üu]rk\s*liras", "TL"),
    (r"\busd\b|\$|\bdolar|\bdollar", "USD"),
    (r"\beur\b|€|\beuro\b", "EUR"),
    (r"%|\byüzde\b|\byuzde\b|\bpercent|\bpct\b", "%"),
    (r"\bendeks\b|\bindex\b|=\s*100", "endeks"),
    (r"\badet\b|\baded[iı]\b|\bsay[ıi]s[ıi]\b|\bcount\b|\bunits?\b", "adet"),   # Adet, Adedi, Sayısı
    (r"\bton\b", "ton"),
    (r"\bkg\b", "kg"),
    (r"\bgram\b|\bgr\b", "gr"),
    (r"\bons\b|\bounce", "ons"),
    (r"\bm2\b|m²", "m2"),
    (r"\bg[üu]n\b|\bdays?\b", "gün"),
    (r"\bki[şs]i\b|\bpersons?\b", "kişi"),
]

UNKNOWN_UNIT = "bilinmiyor"

# Semantics from vocabulary. Order is the priority when several fire.
RATE_WORDS = (r"\bfaiz|\boran|\bgetiri|\byield|\brate\b|\byüzde|\byuzde|\bde[ğg]i[şs]im|\bchange\b|"
              r"\bpay[ıi]?\b|\bshare\b|\bratio\b|\brasyo")
INDEX_WORDS = r"\bendeks|\bindex\b|=\s*100"
FLOW_WORDS = (r"\bsat[ıi][şs]|\bsales?\b|\bhacim|\bvolume\b|\bi[şs]lem|\btransaction|\bihracat|\bexport|"
              r"\bmiktar|\bamount\b|"
              r"\bithalat|\bimport|\b[üu]retim|\bproduction|\bgelir|\brevenue|\bgider|\bexpense|"
              r"\bk[âa]r\b|\bprofit|\bkullandır|\bdisbursement|\byeni\b|\bnew\b|\badet\b|\bcount\b")

MONTHLY_RULE = {"stock": "last", "flow": "sum", "rate": "avg", "index": "last", "ratio": "avg", "unknown": "last"}


def clean_name(header: str) -> Tuple[str, str]:
    """(name without decorations, unit the label itself states or '')."""
    name, _formula, _footnote, unit = strip_decorations(header or "")
    return name.strip() or str(header or "").strip(), unit or ""


def _match_unit(text: str) -> str:
    lowered = (text or "").lower()
    for pattern, unit in UNIT_PATTERNS:
        if re.search(pattern, lowered):
            return unit
    return ""


_TRAILING_PAREN_RE = re.compile(r"\(([^()]{1,30})\)\s*$")


def infer_unit(header: str, caption: str = "") -> Tuple[str, str]:
    """(unit, where it came from): the header's own marker, then the caption --
    a BDDK-style title row '(Milyon TL)' applies to the whole table -- else unknown.
    A parenthesised unit at the end of the header ('Miktar/Amount (KG)') beats
    a currency word earlier in it ('TL Miktar/Amount (KG)' is kilograms under a
    TL group heading, not lira)."""
    _, stated = clean_name(header)
    if stated:
        return stated, "header"
    trailing = _TRAILING_PAREN_RE.search(header or "")
    if trailing:
        found = _match_unit(trailing.group(1))
        if found:
            return found, "header"
    found = _match_unit(header)
    if found:
        return found, "header"
    found = _match_unit(caption)
    if found:
        return found, "caption"
    return UNKNOWN_UNIT, "none"


def infer_semantics(header: str, caption: str = "", unit: str = "") -> Tuple[str, str]:
    """(temporal_semantics, source). A percentage is a rate; an index is an
    index; sales/volumes/production are flows; everything else defaults to a
    period-end stock, which is what most banking tables publish."""
    text = f"{header or ''} {caption or ''}".lower()
    if not (header or "").strip():
        return "unknown", "heuristic"
    if unit == "%" or re.search(RATE_WORDS, header.lower()):
        return "rate", "heuristic"
    if unit == "endeks" or re.search(INDEX_WORDS, text):
        return "index", "heuristic"
    if re.search(FLOW_WORDS, header.lower()) or (unit == "adet"):
        return "flow", "heuristic"
    if re.search(FLOW_WORDS, text):
        return "flow", "heuristic"
    return "stock", "default"          # nothing in the words said otherwise; the model may override


def monthly_rule_for(semantics: str) -> str:
    return MONTHLY_RULE.get(semantics, "last")
