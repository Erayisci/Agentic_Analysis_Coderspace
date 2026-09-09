"""The agent-facing schema card.

Written for a model's context window, not for humans: keep it token-efficient.
The query rules here are the agent's only defence against the traps documented
in CLAUDE.md, so they must stay in sync with `backend.domain.canonical`.
"""
from ..core.config import SCHEMA_CARD_PATH


def write_schema_card(tables: dict, tbb_footnotes: list) -> None:
    """Compact, token-efficient description of the lakehouse for the agent's context."""
    lines = [
        "# Lakehouse schema card",
        "",
        "All monetary values: bin TL (thousands of Turkish lira), period-end outstanding",
        "balances (stocks). A month-over-month change is a NET balance change (new lending",
        "minus repayments, plus FX revaluation / write-offs), never 'new lending'.",
        "BDDK and TBB_RM are methodologically different sources: compare, never merge.",
        "BDDK 'follow-up' and TBB 'liquidation' are DIFFERENT concepts (persistent ~19-21% gap,",
        "documented scope differences; see reconciliation_monitor before explaining it).",
        "",
        "## Tables",
    ]
    for name, frame in tables.items():
        periods = ""
        if "period" in frame.columns:
            periods = f", periods {frame.period.min()}..{frame.period.max()}"
        lines.append(f"- **{name}** ({len(frame):,} rows{periods}): {', '.join(frame.columns)}")
    lines += [
        "",
        "## Query rules",
        "- Sector hierarchy: BDDK parents already contain their children; sum only rows",
        "  with relation='top_level' for national aggregates, or use the TOPLAM/TOTAL row.",
        "  BDDK sector 46 is a non-additive detail of 45. TBB sub-sectors sit under parent_code.",
        "- Cross-source questions: read reconciliation_monitor (bddk_value, tbb_value,",
        "  divergence_pct, out_of_band). If out_of_band=True, report a divergence regime",
        "  change instead of the standard methodology explanation.",
        "",
        "## TBB methodology footnotes (primary source, June 2026 report)",
    ]
    lines += [f"> {note}" for note in tbb_footnotes]
    SCHEMA_CARD_PATH.write_text("\n".join(lines), encoding="utf-8")
