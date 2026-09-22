"""Plotly figures built from an AnalysisArtifact.

The chart is derived from the artifact's lineage rather than from instructions,
which removes a class of error the model would otherwise make: columns are
assigned to axes by *unit*, so a percentage and a TL amount can never share a
scale, and every axis is labelled with the unit it carries. The reference
chart in the brief has exactly this shape -- an index on the left, a percentage
on the right.

The figure is returned as a JSON-serialisable dict (`plotly.io.to_json` shape)
so the API can hand it to any frontend without a server-side render.
"""
import copy
import json
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go

from ..agent.state import AnalysisArtifact

# Colour-blind-safe, consistent across turns so a re-rendered table keeps its
# colours when turn 3 adds a column.
PALETTE = ["#1f77b4", "#2ca02c", "#d62728", "#ff7f0e", "#9467bd", "#8c564b", "#17becf"]
DASHED_UNITS = {"%"}


class PieError(ValueError):
    """A pie was asked for and cannot honestly be drawn from this table.

    The message is Turkish and says why, so the executor can fall back to a
    line chart and the answer can carry the reason. A pie is a snapshot of
    parts of one whole; most of what this lakehouse holds is not that.
    """


NON_ADDITIVE_UNITS = {"%", "puan", "endeks"}


def resolve_pie(artifact: AnalysisArtifact, columns: Optional[List[str]] = None,
                period: Optional[str] = None):
    """(period, labels, values) for a pie, or PieError explaining why not.

    Every refusal below was reasoned through before it was written:
    - fewer than two columns: one slice is not a pie;
    - mixed units: a TL amount and a count are not parts of one whole;
    - a rate, a ratio or an index: percentages and index points do not add
      up to anything -- 18% + 37% is not "55% of something";
    - a period the table does not hold, or none where every column has a
      value: the snapshot would be of a month that does not exist;
    - a negative value: there is no negative slice;
    - all zeros: the whole is nothing.
    """
    columns = columns or artifact.column_names()
    if len(columns) < 2:
        raise PieError("pasta grafigi en az iki sutun ister; tek seri bir dilimden ibaret olur")
    units = {artifact.lineage[c].unit for c in columns}
    if len(units) > 1:
        raise PieError("pasta grafigi icin tum sutunlar ayni birimde olmali; tabloda "
                       + ", ".join(sorted(u or "birimsiz" for u in units)) + " var")
    unit = next(iter(units)) or ""
    semantics = {artifact.lineage[c].temporal_semantics for c in columns}
    if unit in NON_ADDITIVE_UNITS or semantics & {"rate", "ratio", "index"}:
        raise PieError(f"{unit or 'bu'} birimindeki seriler (oran/endeks) bir butunun dilimleri degildir; "
                       "pasta grafigi anlamsiz olur")
    frame = artifact.frame[list(columns)]
    complete = frame.dropna()
    if complete.empty:
        raise PieError("tum sutunlarin birlikte deger tasidigi bir donem yok")
    if period:
        stamp = pd.Timestamp(period)
        if stamp not in complete.index:
            raise PieError(f"{period[:7]} doneminde tum sutunlar icin deger yok")
        row = complete.loc[stamp]
    else:
        row = complete.iloc[-1]
    values = [float(v) for v in row.tolist()]
    if any(v < 0 for v in values):
        raise PieError("negatif deger iceren seri pasta grafigine cizilemez")
    if sum(values) <= 0:
        raise PieError("secilen donemde tum degerler sifir")
    stamp = row.name
    return stamp.strftime("%Y-%m"), [artifact.lineage[c].label for c in columns], values


def build_pie(artifact: AnalysisArtifact, columns: Optional[List[str]] = None,
              title: Optional[str] = None, period: Optional[str] = None) -> Dict[str, Any]:
    """A pie of the named columns at one period (default: the last one where
    every column has a value). Raises PieError when the table cannot honestly
    be drawn as a pie -- see `resolve_pie`."""
    columns = columns or artifact.column_names()
    when, labels, values = resolve_pie(artifact, columns, period)
    unit = artifact.lineage[columns[0]].unit
    figure = go.Figure(go.Pie(
        labels=labels, values=[round(v, 4) for v in values], sort=False,
        marker=dict(colors=[PALETTE[i % len(PALETTE)] for i in range(len(columns))]),
        hovertemplate=f"%{{label}}<br>%{{value:,.2f}} {unit}<br>%{{percent}}<extra></extra>",
        textinfo="percent"))
    figure.update_layout(
        title=f"{title or artifact.title} -- {when}",
        legend=dict(orientation="h", yanchor="bottom", y=-0.3),
        margin=dict(l=40, r=40, t=60, b=80), template="plotly_white")
    return json.loads(figure.to_json())


PERCENT_UNITS = {"%", "puan"}


def rebase_for_chart(artifact: AnalysisArtifact, columns: List[str]):
    """A chart-only copy of `columns` with every non-percentage column put on
    the scale "first month = 100", so a TL amount, a count and an index can
    share one axis while the percentages keep the other.

    This is how the kick-off deck's own reference chart shows four series:
    loans and house prices re-based to 2021-01 = 100 on the left, two
    percentages on the right. Nothing here touches the session's table --
    the table keeps its TL, the picture gets the index; the lineage of each
    re-based column says so in its unit and label, and the caller records it
    as a note the answer must carry.

    Returns (artifact_copy, rebased_column_names, base_period). A column
    whose first observation is zero or missing cannot be re-based and is
    left as it is (the caller's unit fallback then still applies).
    """
    frame = artifact.frame[list(columns)].copy()
    lineage = {c: copy.copy(artifact.lineage[c]) for c in columns}
    rebased: List[str] = []
    bases: List[str] = []
    for column in columns:
        line = lineage[column]
        if line.unit in PERCENT_UNITS or line.temporal_semantics in ("rate", "ratio"):
            continue
        series = frame[column].dropna()
        if series.empty or series.iloc[0] == 0:
            continue
        frame[column] = frame[column] / series.iloc[0] * 100
        bases.append(series.index[0].strftime("%Y-%m"))
        rebased.append(column)
    if not rebased:
        return artifact.subset(columns), [], None
    base = bases[0] if len(set(bases)) == 1 else "ilk gözlem"
    unit = f"endeks ({base}=100)"
    for column in rebased:
        lineage[column].unit = unit
        lineage[column].label = f"{lineage[column].label} ({base}=100)"
    return AnalysisArtifact(title=artifact.title, frame=frame, lineage=lineage), rebased, base


def build_chart(artifact: AnalysisArtifact, columns: Optional[List[str]] = None,
                title: Optional[str] = None, kind: str = "line",
                period: Optional[str] = None) -> Dict[str, Any]:
    """A Plotly figure dict for the named columns (default: all of them).

    `kind` is line, bar or pie. A pie is a snapshot at `period` (or the last
    complete one) and raises `PieError` when the data cannot be a pie; the
    caller decides whether to fall back."""
    if artifact.is_empty():
        raise ValueError("cannot chart an empty table")
    columns = columns or artifact.column_names()
    missing = [c for c in columns if c not in artifact.frame.columns]
    if missing:
        raise ValueError(f"column(s) not in the table: {missing}")
    if kind == "pie":
        return build_pie(artifact, columns, title, period)

    units = [artifact.lineage[c].unit for c in columns]
    ordered_units = list(dict.fromkeys(units))
    if len(ordered_units) > 2:
        # Three scales on one chart is unreadable; the caller should split it.
        raise ValueError(f"{len(ordered_units)} different units in one chart ({ordered_units}); "
                         "chart at most two, or index them to a common base first")
    secondary = ordered_units[1] if len(ordered_units) > 1 else None

    figure = go.Figure()
    for position, column in enumerate(columns):
        lineage = artifact.lineage[column]
        on_secondary = secondary is not None and lineage.unit == secondary
        series = artifact.frame[column]
        # A single point drawn with mode="lines" is invisible -- a line needs
        # two points to have anything to draw between. Plotly's own axis
        # autorange then zooms tight around that one value (a window a few
        # parts-per-million wide for a milyar-scale figure), which is what
        # rangemode="tozero" below guards against.
        trace = dict(
            x=[stamp.strftime("%Y-%m-%d") for stamp in series.index],
            y=[None if value != value else round(float(value), 4) for value in series],
            name=f"{lineage.label}",
            mode="lines" if kind == "line" and len(series) > 1 else "markers",
            line=dict(color=PALETTE[position % len(PALETTE)],
                      dash="dash" if lineage.unit in DASHED_UNITS else "solid"),
            hovertemplate=f"%{{x|%Y-%m}}<br>%{{y:,.2f}} {lineage.unit}<extra>{lineage.label}</extra>",
        )
        if on_secondary:
            trace["yaxis"] = "y2"
        figure.add_trace(go.Scatter(**trace) if kind == "line" else go.Bar(
            x=trace["x"], y=trace["y"], name=trace["name"], yaxis=trace.get("yaxis")))

    layout = dict(
        title=title or artifact.title,
        xaxis=dict(title="Dönem"),
        yaxis=dict(title=ordered_units[0], rangemode="tozero"),
        hovermode="x unified",
        # A long or three-plus-trace legend wraps onto a second line at
        # y=-0.3, which then sits on top of the x-axis title instead of
        # below it -- y=-0.45 and a taller bottom margin give that wrap
        # room without moving anything when the legend is short.
        legend=dict(orientation="h", yanchor="bottom", y=-0.45),
        margin=dict(l=60, r=60, t=60, b=110),
        template="plotly_white",
    )
    if secondary:
        layout["yaxis2"] = dict(title=secondary, overlaying="y", side="right", rangemode="tozero")
    figure.update_layout(**layout)
    return json.loads(figure.to_json())


def chart_summary(artifact: AnalysisArtifact, columns: Optional[List[str]] = None) -> Dict[str, Any]:
    """What the chart shows, in words the composer can quote without recomputing."""
    columns = columns or artifact.column_names()
    return {"columns": columns,
            "units": {c: artifact.lineage[c].unit for c in columns},
            "periods": f"{artifact.periods()[0]} .. {artifact.periods()[-1]}" if artifact.periods() else None,
            "n_periods": len(artifact.frame)}


BREAK_COLOURS = {"solid": "#1f7a3a", "moderate": "#c98a00", "tentative": "#9a9a9a"}


def mark_breaks(figure: Dict[str, Any], breaks_by_column: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Draw change-detection breaks on a figure built by `build_chart`: one
    vertical dashed line per break (colour = confidence: green solid, amber
    moderate, grey tentative) with a small label. Periods come as YYYY-MM
    (monthly) or YYYY-MM-DD (weekly/daily); the chart's x values are always
    YYYY-MM-DD, so a monthly period is anchored to the first of the month.
    Returns the same dict, mutated, so it can be used in an expression.
    """
    layout = figure.setdefault("layout", {})
    shapes = layout.setdefault("shapes", [])
    annotations = layout.setdefault("annotations", [])
    slot = 0
    for column, breaks in breaks_by_column.items():
        for brk in breaks:
            period = brk["period"]
            x = period if len(period) == 10 else f"{period}-01"
            colour = BREAK_COLOURS.get(brk.get("confidence", "tentative"), "#9a9a9a")
            shapes.append(dict(type="line", xref="x", yref="paper", x0=x, x1=x, y0=0, y1=1,
                               line=dict(color=colour, width=1.5, dash="dash")))
            label = f'{column}: {period} · {brk.get("confidence", "")}'
            if brk.get("recent"):
                label += " · çok yeni"
            annotations.append(dict(x=x, y=1.0 - 0.06 * (slot % 4), xref="x", yref="paper", text=label,
                                    showarrow=False, xanchor="left", font=dict(size=10, color=colour),
                                    bgcolor="rgba(255,255,255,0.7)"))
            slot += 1
    return figure