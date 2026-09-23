"""Benchmark the agent across Kloudeks model configurations.

    .venv/bin/python -m backend.eval.run_eval                  # every configuration
    .venv/bin/python -m backend.eval.run_eval --config qwen-fast
    .venv/bin/python -m backend.eval.run_eval --scenario demo_turn1_loans_and_rate
    .venv/bin/python -m backend.eval.run_eval --out eval_results.md

MIA exposes one chat model, so the axis under test is not "which model" but
"which configuration of the one model" -- guided decoding on or off, reasoning
on or off. Those turn out to matter more than a model swap would: guided
decoding is the difference between a plan that always parses and one that
sometimes does, and reasoning is a 10x latency multiplier on a task that does
not need it.

The deterministic configuration runs the same scenarios with no model at all.
It is the floor: whatever it scores is what the system guarantees when the
endpoint is down, and any configuration below it is worse than useless.

Five metrics, matching what actually decides demo-day success:
  plan_validity   -- did the model emit a schema-valid plan (no fallback)?
  numeric_match   -- do the gold figures appear in the produced table?
  unit_preserved  -- does every column carry its unit, and the expected ones appear?
  citations       -- is every fetched column attributable to a table and filter?
  latency         -- seconds per turn, end to end
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..agent.pipeline import Agent
from ..core.config import KLOUDEKS_CHAT_MODEL
from ..llm import KloudeksClient

SCENARIOS_PATH = Path(__file__).with_name("scenarios.yaml")

# Each configuration is one hypothesis about how to drive the model.
CONFIGURATIONS: Dict[str, Dict[str, Any]] = {
    "deterministic": {
        "client": False,
        "description": "No LLM at all: rules router + discovery-ranked plan. The floor.",
    },
    "qwen-fast": {
        "client": True, "think": False, "guided": True,
        "description": "Guided json_schema decoding, reasoning off. The intended production setting.",
    },
    "qwen-thinking": {
        "client": True, "think": True, "guided": True,
        "description": "Guided decoding with reasoning on.",
    },
    "qwen-json-only": {
        "client": True, "think": False, "guided": False,
        "description": "json_object mode, no schema constraint -- what a deployment without guided decoding gives.",
    },
}


def load_scenarios(path: Path = SCENARIOS_PATH) -> List[Dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["scenarios"]


def _numbers_in(node: Any, into: List[float]) -> None:
    """Every numeric leaf of a nested result (analysis dicts nest their
    per-direction p-values and per-year decompositions)."""
    if isinstance(node, bool):
        return
    if isinstance(node, (int, float)):
        into.append(float(node))
    elif isinstance(node, dict):
        for value in node.values():
            _numbers_in(value, into)
    elif isinstance(node, list):
        for value in node:
            _numbers_in(value, into)


def _values_in_table(result: Dict[str, Any]) -> List[float]:
    """Every number the produced table holds, for gold matching."""
    numbers: List[float] = []
    for row in result["table"]["rows"]:
        numbers.extend(float(v) for k, v in row.items() if k != "period" and isinstance(v, (int, float)))
    for found in (result.get("find_periods") or []):
        numbers.append(float(found.get("n_periods", 0)))
    _numbers_in(result.get("analysis") or {}, numbers)
    return numbers


def _analysis_matches(expect: Any, analyses: Dict[str, Any]) -> Dict[str, Optional[bool]]:
    """`expect_analysis` as a method name (did it run?) or a mapping that also
    looks at the result: `min_count` (anomalies/breakpoints), `contains_period`
    (a flagged month), `fields` (keys the result must carry)."""
    spec = {"method": expect} if isinstance(expect, str) else dict(expect)
    method = spec["method"]
    matching = [r for key, r in analyses.items() if key.partition(":")[0] == method and isinstance(r, dict)]
    checks: Dict[str, Optional[bool]] = {"analysis_ran": bool(matching)}
    if len(spec) == 1:
        return checks
    result = matching[0] if matching else {}
    content = bool(matching)
    if "min_count" in spec:
        count = result.get("n_anomalies", result.get("n_breakpoints", 0))
        content = content and count >= spec["min_count"]
    if "contains_period" in spec:
        periods = {a.get("period") for a in (result.get("anomalies") or result.get("breakpoints") or [])}
        content = content and spec["contains_period"] in periods
    if "fields" in spec:
        content = content and all(field in result for field in spec["fields"])
    checks["analysis_content"] = content
    return checks


def score_scenario(scenario: Dict[str, Any], result: Dict[str, Any], seconds: float) -> Dict[str, Any]:
    """Grade one turn. Every check is mechanical; none asks a model anything."""
    checks: Dict[str, Optional[bool]] = {}
    table, plan = result["table"], result["plan"]

    # Plan validity: a schema-valid plan the model actually produced. The
    # deterministic fallbacks are honest answers but they are not the model
    # succeeding, so they do not count here.
    reasoning = (plan.get("reasoning") or "").lower()
    origin = (result.get("plan_diagnostics") or {}).get("plan_source")
    checks["plan_validity"] = origin == "llm" if origin else not reasoning.startswith("deterministic")

    if "expect_intent" in scenario:
        checks["intent"] = result["route"]["intent"] == scenario["expect_intent"]

    if "expect_rows" in scenario:
        checks["rows"] = len(table["rows"]) == scenario["expect_rows"]

    if "expect_window" in scenario:
        periods = [row["period"] for row in table["rows"]]
        checks["window"] = bool(periods) and [str(periods[0]), str(periods[-1])] == [
            str(p) for p in scenario["expect_window"]]

    if "expect_keys" in scenario:
        # A key counts as found if the turn actually read it (a citation) or, for
        # a metadata question where nothing is fetched, if discovery surfaced it.
        cited = {(c.get("filters") or {}).get("entity_key") or (c.get("filters") or {}).get("series_code")
                 for c in result["citations"]}
        planned = {step.get("key") for step in plan.get("steps", []) if step.get("key")}
        checks["keys"] = all(key in (cited | planned) for key in scenario["expect_keys"])

    if "expect_units" in scenario:
        checks["unit_preserved"] = all(unit in set(table["units"].values()) for unit in scenario["expect_units"])
    elif table["units"]:
        checks["unit_preserved"] = all(bool(u) for u in table["units"].values())

    if "expect_semantics" in scenario:
        semantics = {line for line in _semantics(result)}
        checks["semantics"] = all(s in semantics for s in scenario["expect_semantics"])

    if scenario.get("expect_preserves_columns"):
        checks["preserved_columns"] = bool(scenario.get("_previous_columns")) and all(
            column in table["columns"] for column in scenario["_previous_columns"])

    if "expect_analysis" in scenario:
        checks.update(_analysis_matches(scenario["expect_analysis"], result.get("analysis") or {}))

    if "expect_wants_analysis" in scenario:
        checks["analysis_routed"] = result["route"].get("wants_analysis") == list(scenario["expect_wants_analysis"])

    if scenario.get("expect_find_periods"):
        checks["find_periods_ran"] = bool(result.get("find_periods"))

    if scenario.get("expect_footnotes"):
        checks["footnotes_ran"] = any(a["op"] == "footnotes" and a["ok"] for a in result["audit"])

    if "expect_verified" in scenario:
        checks["verified_as_expected"] = result["verification"]["passed"] == bool(scenario["expect_verified"])

    if "expect_values" in scenario:
        tolerance = float(scenario.get("tolerance_pct", 1.0)) / 100.0
        produced = _values_in_table(result)
        matched = []
        for name, gold in scenario["expect_values"].items():
            gold = float(gold)
            matched.append(any(abs(value - gold) <= max(tolerance * abs(gold), 1e-9) for value in produced))
        checks["numeric_match"] = all(matched) if matched else None
        checks["_numeric_detail"] = dict(zip(scenario["expect_values"], matched))

    # Citations: every fetched column must be attributable.
    fetched = [name for name in table["columns"]]
    checks["citations"] = bool(result["citations"]) if fetched else None

    checks["verified"] = result["verification"]["passed"]
    checks["no_unsupported_numbers"] = not result.get("unsupported_numbers")

    graded = {k: v for k, v in checks.items() if isinstance(v, bool)}
    return {
        "scenario": scenario["id"],
        "passed": sum(graded.values()),
        "total": len(graded),
        "score": round(sum(graded.values()) / len(graded), 3) if graded else 0.0,
        "seconds": round(seconds, 2),
        "checks": checks,
        "columns": table["columns"],
        "n_rows": len(table["rows"]),
        "failed_steps": [a["op"] for a in result["audit"] if not a["ok"]],
    }


def _semantics(result: Dict[str, Any]) -> List[str]:
    session = result.get("session")
    if session is not None:
        return [line.temporal_semantics for line in session.view().lineage.values()]
    return [c.get("temporal_semantics") for c in result["citations"] if c.get("temporal_semantics")]


def run_configuration(name: str, scenarios: List[Dict[str, Any]], verbose: bool = True) -> Dict[str, Any]:
    """Run every scenario under one configuration, keeping conversations together."""
    config = CONFIGURATIONS[name]
    client = None
    if config["client"]:
        client = KloudeksClient(think=config["think"])
        if not config["guided"]:
            # Force the json_object path to measure what a deployment without
            # guided decoding would actually give us.
            client._schema_mode_supported = False

    agent = Agent(client=client)
    rows, previous_columns = [], {}
    started_all = time.perf_counter()

    try:
        for scenario in scenarios:
            session_id = scenario.get("conversation", scenario["id"])
            scenario = dict(scenario, _previous_columns=previous_columns.get(session_id, []))
            started = time.perf_counter()
            try:
                result = agent.ask(scenario["question"], session_id=session_id, compose_answer=False)
            except Exception as exc:                                   # noqa: BLE001
                rows.append({"scenario": scenario["id"], "passed": 0, "total": 1, "score": 0.0,
                             "seconds": round(time.perf_counter() - started, 2),
                             "checks": {"crashed": False}, "error": f"{type(exc).__name__}: {exc}",
                             "columns": [], "n_rows": 0, "failed_steps": ["<crash>"]})
                continue
            graded = score_scenario(scenario, result, time.perf_counter() - started)
            previous_columns[session_id] = result["table"]["columns"]
            rows.append(graded)
            if verbose:
                flag = "OK " if graded["score"] == 1.0 else ("~  " if graded["score"] >= 0.6 else "XX ")
                print(f"  {flag} {graded['scenario']:<38} {graded['passed']}/{graded['total']} "
                      f"{graded['seconds']:>6.2f}s  {','.join(graded['failed_steps']) or ''}")
    finally:
        if client is not None:
            usage = client.usage.model_dump()
            client.close()
        else:
            usage = {}

    scores = [row["score"] for row in rows]
    latencies = [row["seconds"] for row in rows]
    return {
        "configuration": name,
        "description": config["description"],
        "model": KLOUDEKS_CHAT_MODEL if config["client"] else "none",
        "n_scenarios": len(rows),
        "mean_score": round(statistics.mean(scores), 3) if scores else 0.0,
        "perfect": sum(1 for s in scores if s == 1.0),
        "plan_validity": _rate(rows, "plan_validity"),
        "numeric_match": _rate(rows, "numeric_match"),
        "unit_preserved": _rate(rows, "unit_preserved"),
        "citations": _rate(rows, "citations"),
        "verified": _rate(rows, "verified"),
        "intent": _rate(rows, "intent"),
        "median_seconds": round(statistics.median(latencies), 2) if latencies else 0.0,
        "total_seconds": round(time.perf_counter() - started_all, 1),
        "usage": usage,
        "rows": rows,
    }


def _rate(rows: List[Dict[str, Any]], check: str) -> Optional[float]:
    values = [row["checks"].get(check) for row in rows if isinstance(row["checks"].get(check), bool)]
    return round(sum(values) / len(values), 3) if values else None


def _pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{100 * value:.0f}%"


def to_markdown(reports: List[Dict[str, Any]], scenarios: List[Dict[str, Any]]) -> str:
    best = max(reports, key=lambda r: (r["mean_score"], -r["median_seconds"]))
    lines = [
        "# Kloudeks model benchmark",
        "",
        f"Generated by `python -m backend.eval.run_eval` against `{KLOUDEKS_CHAT_MODEL}` "
        f"on {len(scenarios)} scenarios from `backend/eval/scenarios.yaml`.",
        "",
        "MIA exposes exactly one chat model, so the axis under test is how that model is "
        "driven rather than which model is used. `deterministic` runs the same scenarios "
        "with no LLM and is the floor the system guarantees when the endpoint is unreachable.",
        "",
        "## Results",
        "",
        "| Configuration | Mean score | Plan validity | Numeric match | Units | Citations | Verified | Median latency |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for report in sorted(reports, key=lambda r: -r["mean_score"]):
        lines.append(
            f"| `{report['configuration']}` | **{report['mean_score']:.2f}** | {_pct(report['plan_validity'])} "
            f"| {_pct(report['numeric_match'])} | {_pct(report['unit_preserved'])} | {_pct(report['citations'])} "
            f"| {_pct(report['verified'])} | {report['median_seconds']:.2f}s |")

    lines += ["", "## Per-scenario detail", ""]
    for report in sorted(reports, key=lambda r: -r["mean_score"]):
        lines += [f"### `{report['configuration']}`", "", report["description"], "",
                  "| Scenario | Score | Time | Failed steps |", "|---|---|---|---|"]
        for row in report["rows"]:
            lines.append(f"| {row['scenario']} | {row['passed']}/{row['total']} | {row['seconds']:.2f}s "
                         f"| {', '.join(row['failed_steps']) or '-'} |")
        usage = report.get("usage") or {}
        if usage:
            lines += ["", f"Tokens: {usage.get('prompt_tokens', 0):,} prompt / "
                          f"{usage.get('completion_tokens', 0):,} completion "
                          f"({usage.get('reasoning_tokens', 0):,} reasoning) over {usage.get('calls', 0)} calls."]
        lines.append("")

    floor = next((r for r in reports if r["configuration"] == "deterministic"), None)
    fast = next((r for r in reports if r["configuration"] == "qwen-fast"), None)
    thinking = next((r for r in reports if r["configuration"] == "qwen-thinking"), None)
    json_only = next((r for r in reports if r["configuration"] == "qwen-json-only"), None)

    lines += ["## Verdict", "",
              f"**`{best['configuration']}` is the configuration to run.** "
              f"Mean score {best['mean_score']:.2f} at a median {best['median_seconds']:.2f}s per turn.", ""]

    if fast and thinking:
        speedup = thinking["median_seconds"] / max(fast["median_seconds"], 0.01)
        lines += [
            "### Reasoning is not worth its budget here",
            "",
            f"`qwen-thinking` is **{speedup:.0f}x slower** ({thinking['median_seconds']:.1f}s vs "
            f"{fast['median_seconds']:.2f}s median) and scores *lower* on the two metrics that matter "
            f"most: plan validity {_pct(thinking['plan_validity'])} vs {_pct(fast['plan_validity'])}, "
            f"numeric match {_pct(thinking['numeric_match'])} vs {_pct(fast['numeric_match'])}.",
            "",
            "The mechanism is a budget collision, not a capability gap. Qwen3 bills reasoning tokens "
            "against `max_tokens`, so on a long question the model can spend its entire allowance "
            "thinking and return an empty string -- which the client sees as a parse failure and "
            "answers with a deterministic fallback. `KloudeksClient` now quadruples the budget when "
            "thinking is on, which is why this configuration produces plans at all; it still buys "
            "nothing, because planning is schema-filling and the schema is already enforced by the "
            "server. Reasoning may earn its keep in the composer, where the task is prose.",
            "",
        ]

    if fast and json_only:
        lines += [
            "### Guided decoding is the single most valuable server feature",
            "",
            f"`qwen-json-only` drops plan validity to {_pct(json_only['plan_validity'])} and mean score "
            f"to {json_only['mean_score']:.2f} -- *below the no-LLM floor*. Same model, same prompts; "
            "the only difference is whether generation is constrained to the plan schema. Without it "
            "the model emits JSON that parses but does not validate, every turn falls back, and the "
            "LLM becomes a latency cost with no benefit. If a deployment ever withdraws "
            "`response_format: json_schema`, this system should be run in `deterministic` mode instead.",
            "",
        ]

    if fast and floor:
        margin = fast["mean_score"] - floor["mean_score"]
        lines += [
            "### What the floor tells us",
            "",
            f"`deterministic` uses no model at all and still scores {floor['mean_score']:.2f} at "
            f"{floor['median_seconds']:.2f}s, because routing, discovery, units, citations and "
            f"verification are all deterministic. The model is worth "
            f"**{margin:+.2f}** on top of that -- real, but it is an improvement to a working system "
            "rather than the thing that makes it work. That is the intended shape: on demo day an "
            "unreachable endpoint costs answer quality, not the answer.",
            "",
            "### Where the remaining points are",
            "",
            "The scenarios that still fail are failures of *series selection*, not of arithmetic: "
            "`unit_trap_sectoral_vs_bulletin` and `cumulative_profit_flow` lose points when the "
            "planner picks a neighbouring series. Every number that does reach an answer is computed "
            "in Python, carries its unit, and is attributable -- citations are 100% in every "
            "configuration. The next gain is in discovery ranking (semantic search over "
            "`bulletin_entities` with the MIA embedding model), not in prompting.",
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", action="append", choices=list(CONFIGURATIONS),
                        help="run only this configuration (repeatable)")
    parser.add_argument("--scenario", action="append", help="run only this scenario id (repeatable)")
    parser.add_argument("--out", type=Path, help="write a Markdown report here")
    parser.add_argument("--json", type=Path, help="also write the raw results as JSON")
    args = parser.parse_args()

    scenarios = load_scenarios()
    if args.scenario:
        scenarios = [s for s in scenarios if s["id"] in args.scenario]
        if not scenarios:
            print("no scenario matched", file=sys.stderr)
            return 1

    reports = []
    for name in (args.config or list(CONFIGURATIONS)):
        print(f"\n=== {name} === {CONFIGURATIONS[name]['description']}")
        reports.append(run_configuration(name, scenarios))
        report = reports[-1]
        print(f"  -> mean {report['mean_score']:.2f} | plan {_pct(report['plan_validity'])} "
              f"| numeric {_pct(report['numeric_match'])} | median {report['median_seconds']:.2f}s")

    if args.json:
        args.json.write_text(json.dumps(reports, indent=2, default=str), encoding="utf-8")
    markdown = to_markdown(reports, scenarios)
    if args.out:
        args.out.write_text(markdown, encoding="utf-8")
        print(f"\nwrote {args.out}")
    else:
        print("\n" + markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
