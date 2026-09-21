"""Turkish number formatting, done once in Python so the composer never does it.

The composer was handed `3423006` with unit `milyon TL` and wrote "3,42 milyar
TL" -- a thousandfold error, produced by the model rescaling a figure in its
head. Catching that after the fact with a regex is the wrong layer: the fix is
to hand the model the finished string ("3,42 trilyon TL") and forbid any
conversion. Every figure the composer may quote passes through here.
"""
import re
from typing import Optional

# Turkish groups thousands with "." and marks decimals with ",".
_UNIT_SCALE = {"bin": 1e3, "milyon": 1e6, "milyar": 1e9, "trilyon": 1e12}
_SCALE_WORDS = (("trilyon", 1e12), ("milyar", 1e9), ("milyon", 1e6), ("bin", 1e3))
_MONETARY = re.compile(r"^(?:(bin|milyon|milyar|trilyon)\s+)?(TL|USD|EUR|ABD dolar[ıi])$", re.I)


def tr_number(value: float, decimals: int = 2, signed: bool = False) -> str:
    """1234567.891 -> '1.234.567,89'; signed=True keeps a leading '+'."""
    text = f"{value:+,.{decimals}f}" if signed else f"{value:,.{decimals}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def format_quantity(value: Optional[float], unit: Optional[str], signed: bool = False) -> str:
    """A figure with its unit, at a human scale, in Turkish notation.

    Monetary units are rescaled to the largest word that keeps the number
    above one -- `3423006 milyon TL` becomes `3,42 trilyon TL`, `34.9 TL`
    stays `34,90 TL` -- so no reader and no model has to count zeros.
    Percentages keep two decimals with a leading "%"; counts are integers.
    """
    if value is None:
        return "-"
    unit = (unit or "").strip()
    if unit == "%":
        return f"%{tr_number(value, 2, signed)}"
    if unit == "puan":
        return f"{tr_number(value, 2, signed)} puan"
    money = _MONETARY.match(unit)
    if money:
        base = value * _UNIT_SCALE.get((money.group(1) or "").lower(), 1.0)
        currency = money.group(2)
        for word, factor in _SCALE_WORDS:
            if abs(base) >= factor:
                return f"{tr_number(base / factor, 2, signed)} {word} {currency}"
        return f"{tr_number(base, 2, signed)} {currency}"
    if unit == "adet":
        return f"{tr_number(value, 0, signed)} adet"
    return f"{tr_number(value, 2, signed)} {unit}".strip()


def format_change(stats: dict) -> Optional[str]:
    """The change line of a series summary: points for a rate, percent for an amount."""
    if stats.get("change_points") is not None:
        return f"{tr_number(stats['change_points'], 2, signed=True)} puan"
    if stats.get("change_pct") is not None:
        return f"%{tr_number(stats['change_pct'], 1, signed=True)}"
    return None
