"""The analysis artifact: a table that survives across turns, with its lineage.

The reference scenario is three chained turns -- build a table, then "konut
kredisi tutarlarini bozmadan enflasyondan arindir", then "bu tabloyu hic
bozmadan yeni bir sutun olarak konut fiyat endeksini getir". Read literally,
that is not a chat-history requirement: it says the table is an object with an
identity, and later turns extend it rather than recompute something that looks
similar. A pipeline that rebuilds from the question each turn will quietly
change a number the user asked to leave alone.

So the artifact is state, held outside the prompt, and every column carries
where it came from: source, key, unit, temporal semantics, and the transform
chain that produced it. That lineage is not bookkeeping -- it is what lets the
executor refuse to divide `bin TL` by `milyon TL`, the verifier notice a column
nobody can cite, and the composer state the unit of every figure it quotes.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

DERIVED = "derived"


@dataclass
class ColumnLineage:
    """Provenance for one column. `citation` is the row the trust layer quotes."""

    column: str
    label: str
    source: str                      # bulletin | weekly | macro | derived
    unit: str
    temporal_semantics: str
    key: Optional[str] = None
    transform: Optional[str] = None            # e.g. "index_to_base(2021-01)"
    derived_from: List[str] = field(default_factory=list)
    citation: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"column": self.column, "label": self.label, "source": self.source,
                "unit": self.unit, "temporal_semantics": self.temporal_semantics,
                "key": self.key, "transform": self.transform,
                "derived_from": self.derived_from, "citation": self.citation}


@dataclass
class AnalysisArtifact:
    """A period-indexed table plus one `ColumnLineage` per column."""

    title: str = "Analiz"
    frame: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(index=pd.DatetimeIndex([], name="period")))
    lineage: Dict[str, ColumnLineage] = field(default_factory=dict)

    # -- construction ------------------------------------------------------

    def add_column(self, name: str, values: pd.Series, lineage: ColumnLineage,
                   how: str = "outer") -> "AnalysisArtifact":
        """Add or replace one column, aligning on the period index.

        `how` governs what happens to the index, and the default is deliberate:
        "outer" keeps every period either side already had, so adding a column
        can never silently shorten the table a previous turn produced. A plan
        that wants the intersection asks for it.
        """
        values = values.copy()
        values.name = name
        if self.frame.empty and not len(self.frame.columns):
            self.frame = values.to_frame()
            self.frame.index.name = "period"
        else:
            self.frame = self.frame.join(values, how=how) if name not in self.frame.columns \
                else self.frame.assign(**{name: values})
        self.frame = self.frame.sort_index()
        self.lineage[name] = lineage
        return self

    def units(self) -> Dict[str, str]:
        return {name: line.unit for name, line in self.lineage.items() if name in self.frame.columns}

    def subset(self, columns: List[str]) -> "AnalysisArtifact":
        """A view of this table holding only `columns` -- the artifact itself
        is untouched.

        The artifact accumulates across turns by design, but a *turn* is about
        a few of its columns. Handing the whole thing to the payload made the
        answer to "konut kredilerini aylik goster" arrive with the previous
        question's NPL and rate columns beside it, and its coverage caveat
        ("npl ve faiz serilerinde %20 eksik") was about columns nobody had
        asked about in that turn -- the earlier series simply span fewer
        months than the new window. `Session.focus` decides which columns a
        turn is about; this produces the table for them.

        Rows that hold nothing in the selection are dropped: the index is the
        union of every column's periods, so a narrower selection legitimately
        spans fewer months.
        """
        keep = [name for name in self.frame.columns if name in set(columns)]
        if not keep or len(keep) == len(self.frame.columns):
            return self
        return AnalysisArtifact(
            title=self.title,
            frame=self.frame[keep].dropna(how="all"),
            lineage={name: self.lineage[name] for name in keep if name in self.lineage})

    def column_names(self) -> List[str]:
        return list(self.frame.columns)

    def is_empty(self) -> bool:
        return self.frame.empty or not len(self.frame.columns)

    # -- serialisation -----------------------------------------------------

    def periods(self) -> List[str]:
        return [period.strftime("%Y-%m-%d") for period in self.frame.index]

    def to_records(self) -> List[Dict[str, Any]]:
        out = []
        for period, row in self.frame.iterrows():
            record = {"period": period.strftime("%Y-%m-%d")}
            record.update({k: (None if pd.isna(v) else float(v)) for k, v in row.items()})
            out.append(record)
        return out

    def to_markdown(self, max_rows: int = 12) -> str:
        """A compact preview for a model's context window: head and tail only.

        A 60-row table costs more context than it informs; the shape of the
        series is in the first and last few rows plus the summary the composer
        is given separately.
        """
        if self.is_empty():
            return "(bos tablo)"
        frame = self.frame
        if len(frame) > max_rows:
            head, tail = frame.head(max_rows // 2), frame.tail(max_rows // 2)
            frame = pd.concat([head, tail])
            note = f"\n... ({len(self.frame)} satirin {max_rows} tanesi gosteriliyor)"
        else:
            note = ""
        header = " | ".join(["period"] + [f"{c} ({self.lineage[c].unit})" for c in frame.columns])
        rows = [" | ".join([period.strftime("%Y-%m")] +
                           ["" if pd.isna(v) else f"{v:,.2f}" for v in row])
                for period, row in frame.iterrows()]
        return "\n".join([header, "-" * len(header), *rows]) + note

    def summary(self) -> Dict[str, Any]:
        """Per-column facts the composer may quote: first, last, change, extremes.

        The composer is given this instead of the raw table precisely so it
        never has to compute anything -- "krediler %X artti" comes from here or
        it does not get said.
        """
        out = {}
        for name in self.frame.columns:
            series = self.frame[name].dropna()
            if series.empty:
                out[name] = {"unit": self.lineage[name].unit, "n": 0}
                continue
            first, last = float(series.iloc[0]), float(series.iloc[-1])
            # A rate moves in points, not percent-of-percent: 18.4% -> 37.3% is
            # "+18.9 puan", and "+102.8%" invites the reader to misread it.
            # An exchange rate is declared `rate` too, but 13.53 TL -> 34.90 TL
            # is a +158% move, not "+21 puan": points are for percentages.
            unit = self.lineage[name].unit or ""
            is_rate = unit == "%" or (self.lineage[name].temporal_semantics == "rate" and unit in ("", "puan"))
            out[name] = {
                "label": self.lineage[name].label,
                "unit": self.lineage[name].unit,
                "temporal_semantics": self.lineage[name].temporal_semantics,
                "first_period": series.index[0].strftime("%Y-%m"),
                "first_value": round(first, 4),
                "last_period": series.index[-1].strftime("%Y-%m"),
                "last_value": round(last, 4),
                **({"change_points": round(last - first, 4)} if is_rate
                   else {"change_pct": round(100 * (last / first - 1), 2) if first else None}),
                "min_value": round(float(series.min()), 4),
                "min_period": series.idxmin().strftime("%Y-%m"),
                "max_value": round(float(series.max()), 4),
                "max_period": series.idxmax().strftime("%Y-%m"),
                "n": int(len(series)),
            }
        return out


@dataclass
class AuditStep:
    """One executed plan step: what ran, what it produced, whether it worked."""

    index: int
    op: str
    arguments: Dict[str, Any]
    ok: bool
    detail: str = ""
    seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "op": self.op, "arguments": self.arguments,
                "ok": self.ok, "detail": self.detail, "seconds": round(self.seconds, 3)}


@dataclass
class Session:
    """Everything that persists between turns of one conversation."""

    session_id: str = "default"
    artifact: AnalysisArtifact = field(default_factory=AnalysisArtifact)
    citations: List[Dict[str, Any]] = field(default_factory=list)
    audit: List[AuditStep] = field(default_factory=list)
    facts: Dict[str, Any] = field(default_factory=dict)
    turns: List[Dict[str, Any]] = field(default_factory=list)
    # Which columns THIS turn wrote or read, and which ones the answer is
    # about. The artifact is the whole conversation; these two are the turn.
    turn_columns: List[str] = field(default_factory=list)
    visible_columns: List[str] = field(default_factory=list)

    def cite(self, citation: Dict[str, Any]) -> None:
        """Record provenance once. Turn 3 re-reads turn 1's series; the answer
        should carry one citation for it, not three."""
        if citation and citation not in self.citations:
            self.citations.append(citation)

    def start_turn(self, question: str) -> None:
        """A turn's audit, facts and touched columns are its own; the
        artifact is not."""
        self.audit = []
        self.facts = {}
        self.turn_columns = []
        self.turns.append({"question": question, "n": len(self.turns) + 1})

    def touch_column(self, name: Optional[str]) -> Optional[str]:
        """Record that this turn wrote or read `name`.

        Every column the executor creates, and every one it resolves as a
        step's input, passes through here. That is what makes the turn's own
        scope a by-product of execution rather than a guess reconstructed
        from the plan afterwards -- a fetch whose key discovery had to repair
        lands under the name the executor actually assigned.
        """
        if name and name not in self.turn_columns:
            self.turn_columns.append(name)
        return name

    def focus(self, keep_previous: bool = False) -> List[str]:
        """Decide which columns this turn's answer is about; remember them.

        The rule is one turn deep, not cumulative: a new question is about
        the columns it touched, and a follow-up ("bozmadan", "tabloya ekle")
        is about those plus the ones the *previous* turn showed. Carrying the
        whole session forward instead is what put four columns under a
        question that named two, and three turns in it would have been six.

        A derived column's inputs come along: the table must be able to
        explain the numbers it shows, and `verifier` checks exactly that.
        """
        existing = self.artifact.column_names()
        scope = {c for c in self.turn_columns if c in existing}
        if keep_previous:
            scope |= {c for c in self.visible_columns if c in existing}
        for _ in range(len(existing)):
            parents = {p for c in scope for p in self.artifact.lineage[c].derived_from
                       if c in self.artifact.lineage and p in existing}
            if parents <= scope:
                break
            scope |= parents
        if not scope:
            # A turn that produced nothing (metadata, a failed plan) leaves
            # the table it found in place rather than blanking the panel.
            scope = {c for c in self.visible_columns if c in existing} or set(existing)
        self.visible_columns = [c for c in existing if c in scope]
        return self.visible_columns

    def view(self) -> AnalysisArtifact:
        """The artifact as this turn should present it -- see `focus`."""
        return self.artifact.subset(self.visible_columns) if self.visible_columns else self.artifact

    def has_artifact(self) -> bool:
        return not self.artifact.is_empty()
