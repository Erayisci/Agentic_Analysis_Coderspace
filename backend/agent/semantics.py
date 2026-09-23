"""Interpret intent once; data identities and arithmetic belong to later stages."""
import json
import logging
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..llm import LLMError


class QuerySemantics(BaseModel):
    # Keep convenient Python defaults, but guided model output must explicitly
    # decide every field instead of returning {} and silently taking defaults.
    model_config = ConfigDict(extra="forbid", json_schema_extra={"required": [
        "interaction", "requested_output", "requested_basis", "frequency",
        "currencies", "preserve_existing_window",
    ]})

    interaction: Literal["new_analysis", "extend_previous", "compare_previous", "presentation_only"] = "new_analysis"
    requested_output: Literal["level", "absolute_change", "percent_change", "flow", "unspecified"] = "unspecified"
    requested_basis: Literal["stock", "flow", "unspecified"] = "unspecified"
    frequency: Optional[Literal["daily", "weekly", "monthly", "quarterly", "annual"]] = None
    currencies: List[Literal["TL", "FX"]] = Field(default_factory=list)
    preserve_existing_window: bool = False


class SemanticInterpretation(BaseModel):
    """A brief statement of the user's constraint before the structured decision.

    Keeping the constraint explicit prevents a long list of source titles from
    drowning out an output restriction. Only `semantics` reaches the planner
    and validator; this text is never matched or used as an execution trigger.
    """
    model_config = ConfigDict(extra="forbid")

    output_requirement: str = Field(min_length=1, max_length=400, description=(
        "One short sentence stating the requested output and explicit exclusions. "
        "Identify the output constraint even inside a long question; do not describe source availability."))
    semantics: QuerySemantics


SEMANTICS_SYSTEM = """Interpret the user's analytical intent as QuerySemantics.
Understand meaning across languages and paraphrases, rather than matching words.
Do not choose data sources, dataset keys, series codes, tools, or calculations.
Return every field. Read the entire question, including constraints following
the initial topic. Explicit output constraints take priority over generic
requests to study development or evolution.

requested_output describes the requested RESULT:
- level: the balance, outstanding amount, or observed value itself.
- absolute_change: the difference between successive balances, in the original
  unit. Rejecting stock levels in favour of each period's balance movement also
  means absolute_change, unless gross transactions are explicitly requested.
- percent_change: relative growth expressed as a percentage, not an absolute
  difference. An interest rate's level being a percentage does not imply growth.
- flow: transactions occurring during a period, such as newly placed deposits,
  new lending or gross inflows. A balance difference is NEVER gross flow:
  withdrawals, repayments and valuation changes can also change a balance.
- unspecified: no clear output preference. Do not guess an unstated transform.

Source titles describe available evidence, not the desired output. If a user
explicitly rejects balances/stock levels, requested_output CANNOT be level.
Choose absolute_change for a request to show only that period's movement of a
balance, and flow only for an explicit request for gross transactions or new
business. Do not let a long list of source titles override that distinction.

requested_basis describes the requested SOURCE measurement basis, independently
of the output transform: stock for outstanding balances or stock-based rates;
flow for period transactions or flow-based rates; unspecified if not established.
Do not infer flow just because a user rejects levels and wants balance changes.
frequency is the observation frequency explicitly requested, or null.
currencies normalizes local currency (including TP) to TL and foreign currency
(including YP) to FX. Leave empty when unspecified.

Use the existing table context to resolve references and additions. interaction:
new_analysis for an independent request; extend_previous for adding to or
modifying the existing analysis; compare_previous for comparison with it;
presentation_only for changing only its display. Without an existing table use
new_analysis. preserve_existing_window is true when the current period should
be kept, including an addition/comparison that requests no replacement period.
Interpret the current question; previous context is not a new instruction.
Return output_requirement followed by the interpreted semantics object.
"""


def parse_query_semantics(question: str, client=None, *, context: Optional[dict] = None) -> Optional[QuerySemantics]:
    """None means unavailable, so the caller may use conservative router fallback.

    No text-based output fallback: an unavailable interpretation must not turn
    an uncertain request for flows into a balance difference.
    """
    if client is None:
        return None
    messages = [
        {"role": "system", "content": SEMANTICS_SYSTEM},
        {"role": "user", "content": json.dumps(
            {"question": question, "existing_context": context or {}}, ensure_ascii=False)},
    ]
    try:
        result = SemanticInterpretation.model_validate(client.structured(
            messages, SemanticInterpretation, max_tokens=500, think=False))
        if result.semantics.model_fields_set != set(QuerySemantics.model_fields):
            raise LLMError("semantic response omitted required decisions")
        return result.semantics
    except (LLMError, ValidationError) as exc:
        logging.getLogger("kkb.agent").warning("semantic parse unavailable: %s", exc)
        return None
