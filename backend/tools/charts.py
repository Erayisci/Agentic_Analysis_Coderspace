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
import json
from typing import Any, Dict, List, Optional

import plotly.graph_objects as go

from ..agent.state import AnalysisArtifact

# Colour-blind-safe, consistent across turns so a re-rendered table keeps its
# colours when turn 3 adds a column.
PALETTE = ["#1f77b4", "#2ca02c", "#d62728", "#ff7f0e", "#9467bd", "#8c564b", "#17becf"]
DASHED_UNITS = {"%"}


def build_chart(artifact: AnalysisArtifact, columns: Optional[List[str]] = None,
                title: Optional[str] = None, kind: str = "line") -> Dict[str, Any]:
    """A Plotly figure dict for the named columns (default: all of them)."""
    if artifact.is_empty():
        raise ValueError("cannot chart an empty table")
    columns = columns or artifact.column_names()
    missing = [c for c in columns if c not in artifact.frame.columns]
    if missing:
        raise ValueError(f"column(s) not in the table: {missing}")

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
        trace = dict(
            x=[stamp.strftime("%Y-%m-%d") for stamp in series.index],
            y=[None if value != value else round(float(value), 4) for value in series],
            name=f"{lineage.label}",
            mode="lines" if kind == "line" else "markers",
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
        yaxis=dict(title=ordered_units[0]),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=-0.3),
        margin=dict(l=60, r=60, t=60, b=80),
        template="plotly_white",
    )
    if secondary:
        layout["yaxis2"] = dict(title=secondary, overlaying="y", side="right")
    figure.update_layout(**layout)
    return json.loads(figure.to_json())


def chart_summary(artifact: AnalysisArtifact, columns: Optional[List[str]] = None) -> Dict[str, Any]:
    """What the chart shows, in words the composer can quote without recomputing."""
    columns = columns or artifact.column_names()
    return {"columns": columns,
            "units": {c: artifact.lineage[c].unit for c in columns},
            "periods": f"{artifact.periods()[0]} .. {artifact.periods()[-1]}" if artifact.periods() else None,
            "n_periods": len(artifact.frame)}
