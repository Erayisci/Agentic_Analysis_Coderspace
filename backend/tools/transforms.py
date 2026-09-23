"""Deterministic column transforms over an AnalysisArtifact.

Every arithmetic operation the agent can perform lives here, in Python, tested,
and unit-aware. The model chooses *which* transform to apply and never performs
one: that is the whole premise of the system, and it is also what makes the
answer auditable, because each transform records what it did in the column's
lineage.

Two rules the functions enforce rather than trust:

- **Units must agree.** `bin TL` and `milyon TL` differ by 1000x and both appear
  in this corpus for the same quantity. Any operation combining two columns
  checks first and refuses rather than producing a plausible wrong number.
- **Differencing respects temporal semantics.** A month-over-month change on a
  stock is a net balance change, not new lending; on a year-to-date series it is
  meaningless unless the series was de-cumulated first. The functions label what
  they produced so the composer cannot describe it as something else.
"""
from typing import List, Optional

import pandas as pd

from ..agent.state import DERIVED, AnalysisArtifact, ColumnLineage

# Units that are already relative: indexing or deflating them is a category error.
RELATIVE_UNITS = {"%", "endeks", "adet", "gün", "gun", "kişi", "kisi", "adet"}


def _require(artifact: AnalysisArtifact, column: str) -> ColumnLineage:
    if column not in artifact.frame.columns:
        raise ValueError(f"column {column!r} is not in the table; have {artifact.column_names()}")
    return artifact.lineage[column]


def _base_value(series: pd.Series, base_period: Optional[str]) -> tuple:
    """The value to index against, and the period it came from."""
    clean = series.dropna()
    if clean.empty:
        raise ValueError("series has no values to index")
    if base_period:
        stamp = pd.Timestamp(base_period)
        matching = clean[clean.index == stamp]
        if matching.empty:
            matching = clean[clean.index >= stamp]
            if matching.empty:
                raise ValueError(f"no observation at or after base period {base_period}")
        return float(matching.iloc[0]), matching.index[0]
    return float(clean.iloc[0]), clean.index[0]


def index_to_base(artifact: AnalysisArtifact, column: str, base_period: Optional[str] = None,
                  as_name: Optional[str] = None) -> str:
    """Rescale a column so its base period equals 100. Returns the new column name."""
    lineage = _require(artifact, column)
    if lineage.unit == "%":
        raise ValueError(f"{column!r} is a percentage; indexing a rate to 100 is not meaningful")
    base, stamp = _base_value(artifact.frame[column], base_period)
    if not base:
        raise ValueError(f"base value for {column!r} is zero; cannot index")

    name = as_name or f"{column}_endeks"
    label = f"{lineage.label} (Endeks, {stamp:%Y-%m}=100)"
    artifact.add_column(name, 100 * artifact.frame[column] / base, ColumnLineage(
        column=name, label=label, source=DERIVED, unit="endeks",
        temporal_semantics="index", key=lineage.key,
        transform=f"index_to_base({column}, {stamp:%Y-%m})",
        derived_from=[column], citation=dict(lineage.citation)))
    return name


def deflate(artifact: AnalysisArtifact, column: str, deflator: str,
            base_period: Optional[str] = None, as_name: Optional[str] = None) -> str:
    """Express a nominal column in constant prices of the base period.

    real_t = nominal_t * (deflator_base / deflator_t). The result keeps the
    nominal column's unit, because it is still TL -- just TL of one fixed month,
    which the label states. Turn 2 of the reference scenario is exactly this,
    and it must not disturb the column it deflates.
    """
    nominal = _require(artifact, column)
    price = _require(artifact, deflator)
    if nominal.unit == "%":
        raise ValueError(f"{column!r} is a percentage; deflating a rate is not meaningful")
    if price.temporal_semantics not in ("index", "ratio", "rate"):
        raise ValueError(f"deflator {deflator!r} is {price.temporal_semantics}, expected a price index")

    base, stamp = _base_value(artifact.frame[deflator], base_period)
    name = as_name or f"{column}_reel"
    values = artifact.frame[column] * (base / artifact.frame[deflator])
    artifact.add_column(name, values, ColumnLineage(
        column=name, label=f"{nominal.label} (reel, {stamp:%Y-%m} fiyatlariyla)",
        source=DERIVED, unit=nominal.unit, temporal_semantics=nominal.temporal_semantics,
        key=nominal.key, transform=f"deflate({column}, by={deflator}, base={stamp:%Y-%m})",
        derived_from=[column, deflator], citation=dict(nominal.citation)))
    return name


def change(artifact: AnalysisArtifact, column: str, periods: int = 1,
           as_name: Optional[str] = None) -> str:
    """Percent change over `periods` rows. 1 = MoM on monthly data, 12 = YoY."""
    lineage = _require(artifact, column)
    if lineage.temporal_semantics == "cumulative_ytd":
        raise ValueError(
            f"{column!r} is year-to-date; differencing it crosses the January reset. "
            "Load it as a flow (load_series cumulative_as='flow') before differencing.")
    name = as_name or f"{column}_{'yoy' if periods == 12 else f'chg{periods}'}_pct"
    kind = "net degisim" if lineage.temporal_semantics == "stock" else "degisim"
    artifact.add_column(name, 100 * (artifact.frame[column] / artifact.frame[column].shift(periods) - 1),
                        ColumnLineage(
                            column=name, label=f"{lineage.label} ({kind}, %{periods} donem)",
                            source=DERIVED, unit="%", temporal_semantics="rate", key=lineage.key,
                            transform=f"change({column}, periods={periods})", grain=lineage.grain,
                            derived_from=[column], citation=dict(lineage.citation)))
    return name


def net_change(artifact: AnalysisArtifact, column: str, periods: int = 1,
               as_name: Optional[str] = None) -> str:
    """Absolute stock difference, in the input unit; never gross inflows."""
    lineage = _require(artifact, column)
    if lineage.temporal_semantics != "stock" or not lineage.unit or lineage.unit.lower() in RELATIVE_UNITS:
        raise ValueError(f"net_change requires a stock amount; {column!r} is "
                         f"{lineage.temporal_semantics} ({lineage.unit})")
    if isinstance(periods, bool) or not isinstance(periods, int) or periods < 1:
        raise ValueError("net_change periods must be a positive integer")
    name = as_name or f"{column}_net_change{periods}"
    values = artifact.frame[column] - artifact.frame[column].shift(periods)
    artifact.add_column(name, values, ColumnLineage(
        column=name, label=f"{lineage.label} (net bakiye degisimi, {periods} donem; brut akis degil)",
        source=DERIVED, unit=lineage.unit, temporal_semantics="net_change", key=lineage.key,
        grain=lineage.grain, transform=f"net_change({column}, periods={periods})",
        derived_from=[column], citation=dict(lineage.citation)))
    return name


def sum_columns(artifact: AnalysisArtifact, columns: List[str], as_name: Optional[str] = None) -> str:
    """Sum compatible, disjoint amount columns; missing inputs stay missing."""
    if len(columns) < 2 or len(set(columns)) != len(columns):
        raise ValueError("sum_columns needs at least two distinct columns")
    lines = [_require(artifact, column) for column in columns]
    first = lines[0]
    if len({line.unit for line in lines}) != 1:
        raise ValueError("sum_columns units differ")
    if len({line.grain for line in lines}) != 1:
        raise ValueError("sum_columns inputs have different grains")
    if (len({line.temporal_semantics for line in lines}) != 1
            or first.temporal_semantics not in ("stock", "flow", "net_change")
            or not first.unit or first.unit.lower() in RELATIVE_UNITS):
        raise ValueError("sum_columns requires compatible additive amounts")
    name = as_name or "_plus_".join(columns)
    artifact.add_column(name, artifact.frame[columns].sum(axis=1, min_count=len(columns)), ColumnLineage(
        column=name, label=" + ".join(line.label for line in lines), source=DERIVED,
        unit=first.unit, temporal_semantics=first.temporal_semantics, grain=first.grain,
        transform=f"sum_columns({', '.join(columns)})", derived_from=list(columns), citation={}))
    return name


def ratio(artifact: AnalysisArtifact, numerator: str, denominator: str,
          as_name: Optional[str] = None, as_percent: bool = True) -> str:
    """One column over another, as a share. Refuses mismatched units."""
    top, bottom = _require(artifact, numerator), _require(artifact, denominator)
    if top.unit != bottom.unit:
        raise ValueError(
            f"cannot divide {numerator!r} ({top.unit}) by {denominator!r} ({bottom.unit}): "
            "the units differ, so the ratio would be off by their scale factor")
    # Same argument, one axis over: two series resampled onto the same monthly
    # index are still incomparable when they are published at different grains.
    # The weekly bulletin runs weeks ahead of the monthly one and measures a
    # different scope, so dividing one by the other produces a plausible number
    # that means nothing. Caught here as well as in the verifier because this
    # one is cheap and exact -- the verifier catches what reaches the table by
    # another route.
    if top.grain and bottom.grain and top.grain != bottom.grain:
        raise ValueError(
            f"cannot divide {numerator!r} ({top.grain}) by {denominator!r} ({bottom.grain}): "
            "the series are published at different grains, so the ratio pairs observations "
            "that were never measured over the same period")
    name = as_name or f"{numerator}_over_{denominator}"
    values = artifact.frame[numerator] / artifact.frame[denominator]
    artifact.add_column(name, 100 * values if as_percent else values, ColumnLineage(
        column=name, label=f"{top.label} / {bottom.label}", source=DERIVED,
        unit="%" if as_percent else "oran", temporal_semantics="ratio",
        transform=f"ratio({numerator}, {denominator})", grain=top.grain,
        derived_from=[numerator, denominator], citation={}))
    return name


def in_usd(artifact: AnalysisArtifact, column: str, rate: str, as_name: Optional[str] = None) -> str:
    """A TL-denominated column divided by the USD/TRY rate: the same stock in dollars.

    An FX deposit stock is published in TL, so it rises with the exchange rate
    by construction -- "USD rose in 42 months and FX deposits rose in all 42"
    is an identity, not a finding. Dividing by the rate removes the valuation
    effect; what is left is whether dollars actually moved. The unit follows:
    milyon TL becomes milyon USD.
    """
    top, fx = _require(artifact, column), _require(artifact, rate)
    if "TL" not in (top.unit or "") or fx.unit != "TL":
        raise ValueError(
            f"in_usd needs a TL amount and a TL-per-USD rate, got {column!r} ({top.unit}) "
            f"and {rate!r} ({fx.unit})")
    name = as_name or f"{column}_usd"
    values = artifact.frame[column] / artifact.frame[rate]
    artifact.add_column(name, values, ColumnLineage(
        column=name, label=f"{top.label} (USD bazinda, {fx.label} ile)", source=DERIVED,
        unit=top.unit.replace("TL", "USD"), temporal_semantics=top.temporal_semantics,
        transform=f"in_usd({column}, {rate})", derived_from=[column, rate], citation={}))
    return name


def find_periods(artifact: AnalysisArtifact, column: str, direction: str = "down",
                 against: Optional[str] = None, against_direction: str = "up",
                 min_abs_change: float = 0.0) -> dict:
    """Periods where a column moved one way -- optionally while another did not.

    Turn 1 of the reference scenario asks, in words, "were there months where the
    rate fell but loans did not rise?". That is a question about coincident signs,
    and answering it in prose invites the model to invent months. This answers it
    by arithmetic and hands back the list.
    """
    _require(artifact, column)
    moves = artifact.frame[column].diff()
    wanted = moves < -abs(min_abs_change) if direction == "down" else moves > abs(min_abs_change)

    word = {"down": "dustugu", "up": "yukseldigi"}[direction]
    result = {"column": column, "direction": direction,
              "n_column_moves": int(wanted.fillna(False).sum()),
              "description": f"{column} {word} aylar",
              "n_periods": 0, "periods": []}
    if against is not None:
        _require(artifact, against)
        other = artifact.frame[against].diff()
        other_wanted = other > 0 if against_direction == "up" else other <= 0
        wanted = wanted & other_wanted
        # The composer reads this dict, not the code: without the description
        # it reported "the 4 months the rate fell" for what is really "the 4
        # of 27 rate-fall months in which loans did not rise".
        other_word = {"up": "yukseldigi", "down": "yukselmedigi (dustugu veya sabit kaldigi)"}[against_direction]
        result["description"] = (f"{column} {word} {result['n_column_moves']} ayin icinde "
                                 f"{against} {other_word} aylar")
        result.update({"against": against, "against_direction": against_direction})

    hits = artifact.frame.index[wanted.fillna(False)]
    result["n_periods"] = int(len(hits))
    result["periods"] = [{
        "period": stamp.strftime("%Y-%m"),
        column: None if pd.isna(artifact.frame.loc[stamp, column]) else round(float(artifact.frame.loc[stamp, column]), 4),
        f"{column}_change": None if pd.isna(moves.loc[stamp]) else round(float(moves.loc[stamp]), 4),
        **({against: round(float(artifact.frame.loc[stamp, against]), 4)} if against else {}),
    } for stamp in hits]
    return result


def decompose_growth(artifact: AnalysisArtifact, nominal: str, deflator: str) -> dict:
    """Split a nominal column's growth into price growth and real growth.

    nominal = price x real, so over any window
    (1 + g_nominal) = (1 + g_price) x (1 + g_real). The demo's third turn asks
    "faiz düştüğü halde kredilerin artmamasının sebebi fiyat artışı olabilir
    mi?", and the three numbers that answer it -- nominal +145%, house prices
    +1139%, real -80% -- were already on the table as three separate facts
    that a small model did not connect. This connects them, in Python, and
    says what the connection means.

    A fact rather than a column: the table the question protects gains no
    column, and the deliverable is the sentence.
    """
    import math

    nominal_line, price_line = _require(artifact, nominal), _require(artifact, deflator)
    if nominal_line.unit == "%":
        raise ValueError(f"{nominal!r} is a percentage; a rate has no price component to remove")
    if price_line.temporal_semantics not in ("index", "ratio", "rate"):
        raise ValueError(f"deflator {deflator!r} is {price_line.temporal_semantics}, expected a price index")

    aligned = artifact.frame[[nominal, deflator]].dropna()
    if len(aligned) < 2:
        raise ValueError("need at least two aligned observations to decompose growth")
    first, last = aligned.iloc[0], aligned.iloc[-1]
    if not first[nominal] or not first[deflator]:
        raise ValueError("first observation is zero; growth is undefined")

    g_nom = float(last[nominal] / first[nominal] - 1)
    g_price = float(last[deflator] / first[deflator] - 1)
    g_real = (1 + g_nom) / (1 + g_price) - 1
    price_share = (math.log1p(g_price) / math.log1p(g_nom)) if g_nom > 0 and g_price > -1 else None

    by_year = []
    dec = aligned[aligned.index.month == 12]
    for prev, cur in zip(dec.index[:-1], dec.index[1:]):
        yn = float(dec.loc[cur, nominal] / dec.loc[prev, nominal] - 1)
        yp = float(dec.loc[cur, deflator] / dec.loc[prev, deflator] - 1)
        by_year.append({"year": int(cur.year), "nominal_pct": round(100 * yn, 1),
                        "price_pct": round(100 * yp, 1), "real_pct": round(100 * ((1 + yn) / (1 + yp) - 1), 1)})

    span = f"{aligned.index.min():%Y-%m}..{aligned.index.max():%Y-%m}"
    if g_real < -0.02:
        reading = ("Nominal artis fiyat artisinin gerisinde kaldi: reel stok daraldi -- "
                   "'artmama fiyat artisiyla tutarli' okumasini destekler")
    elif g_real > 0.02:
        reading = "Nominal artis fiyat artisini asti: reel stok da buyudu -- artmama fiyatla aciklanamaz"
    else:
        reading = "Nominal artis fiyat artisiyla basa bas: reel stok yaklasik sabit"
    description = (f"{span}: nominal {100 * g_nom:+.1f}%, fiyat ({price_line.label}) {100 * g_price:+.1f}%, "
                   f"reel {100 * g_real:+.1f}%. {reading}.")

    return {
        "nominal": nominal, "deflator": deflator,
        "unit": nominal_line.unit, "deflator_label": price_line.label,
        "period_start": aligned.index.min().strftime("%Y-%m"),
        "period_end": aligned.index.max().strftime("%Y-%m"),
        "nominal_first": round(float(first[nominal]), 4), "nominal_last": round(float(last[nominal]), 4),
        "price_first": round(float(first[deflator]), 4), "price_last": round(float(last[deflator]), 4),
        "nominal_pct": round(100 * g_nom, 2), "price_pct": round(100 * g_price, 2),
        "real_pct": round(100 * g_real, 2),
        "price_share_of_nominal_growth": round(price_share, 3) if price_share is not None else None,
        "by_year": by_year,
        "description": description,
        "inputs": [nominal, deflator],
    }


def resample_to_monthly(series: pd.Series, rule: str = "last") -> pd.Series:
    """Weekly/daily observations onto a month-start index.

    Only correct for the rule the series' own semantics imply -- last for a
    stock, mean for a rate, sum for a flow -- which is why the caller passes it
    explicitly and `macro_series.monthly_rule` is where that answer lives.
    """
    grouped = series.groupby(pd.Grouper(freq="MS"))
    if rule == "last":
        return grouped.last().dropna()
    if rule == "avg":
        return grouped.mean().dropna()
    if rule == "sum":
        return grouped.sum(min_count=1).dropna()
    raise ValueError(f"unknown resample rule {rule!r}; expected last, avg or sum")


def window(artifact: AnalysisArtifact, start: Optional[str] = None,
           end: Optional[str] = None) -> AnalysisArtifact:
    """Restrict every column to a period range, in place."""
    frame = artifact.frame
    if start:
        frame = frame[frame.index >= pd.Timestamp(start)]
    if end:
        frame = frame[frame.index <= pd.Timestamp(end)]
    artifact.frame = frame
    return artifact


def columns_sharing_unit(artifact: AnalysisArtifact) -> List[List[str]]:
    """Columns grouped by unit -- what a chart needs to decide on a second axis."""
    groups: dict = {}
    for name in artifact.frame.columns:
        groups.setdefault(artifact.lineage[name].unit, []).append(name)
    return list(groups.values())
