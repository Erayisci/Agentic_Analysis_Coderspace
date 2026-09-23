"""Benchmark discovery against paraphrases of the concepts the corpus holds.

    .venv/bin/python -m backend.eval.run_discovery_eval
    .venv/bin/python -m backend.eval.run_discovery_eval --failures
    .venv/bin/python -m backend.eval.run_discovery_eval --family npl_orani

Why this exists: every ranking rule in `tools/lakehouse.py` was written after a
specific failure, and each was verified only against the failure that prompted
it. That makes the ranking a pile of anecdotes -- a change that fixes one
phrasing and quietly breaks three others looks exactly like a change that fixes
one phrasing. This prints a number instead.

Two numbers, and the gap between them is the point:

**recall@k** is what the planner actually sees -- `discover(phrasing, limit=k)`,
the truncated, source-balanced list that reaches the prompt.

**pool rank** is where the correct key sits in the full scored ranking. A key at
pool rank 40 was found by the search and lost by the ranker; a key with no pool
rank at all was never a candidate, which is a different defect with a different
fix (the search text, or recall). Reporting them apart is what keeps the two
from being debugged as one.

`absent` cases are scored separately: the corpus genuinely does not hold them,
so there is no right key, and what is measured instead is the score of whatever
came back -- evidence for the confidence threshold a "we do not have this" reply
would need.
"""
import argparse
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..core.config import DUCKDB_PATH
from ..tools.lakehouse import discover

CASES_PATH = Path(__file__).with_name("discovery_cases.yaml")

# The full ranked list, for the pool rank. Larger than any candidate set the
# agent is shown: `discover` appends everything once the per-source seats are
# filled, so a limit this size returns the whole scored ranking in score order.
POOL_LIMIT = 300


def load_cases(path: Path = CASES_PATH) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _rank_of(candidates: List[Dict[str, Any]], accepted: set) -> Optional[int]:
    for position, candidate in enumerate(candidates, start=1):
        if candidate["key"] in accepted:
            return position
    return None


def evaluate_phrasing(phrasing: str, accepted: set) -> Dict[str, Any]:
    """One phrasing against the ranker, at each k the caller might use."""
    result: Dict[str, Any] = {"phrasing": phrasing}
    for k in (1, 3, 8):
        result[f"hit@{k}"] = _rank_of(discover(phrasing, limit=k)["candidates"], accepted) is not None
    pool = discover(phrasing, limit=POOL_LIMIT)["candidates"]
    result["pool_rank"] = _rank_of(pool, accepted)
    result["pool_size"] = len(pool)
    result["top_key"] = pool[0]["key"] if pool else None
    result["top_score"] = pool[0]["score"] if pool else 0.0
    return result


def run(cases: Dict[str, Any], only_family: Optional[str] = None) -> Dict[str, Any]:
    families, rows = [], []
    for case in cases.get("cases") or []:
        if only_family and case["family"] != only_family:
            continue
        accepted = {case["expect"], *(case.get("accept") or [])}
        results = [evaluate_phrasing(p, accepted) for p in case["phrasings"]]
        for result in results:
            result["family"] = case["family"]
            result["expect"] = case["expect"]
        families.append({"family": case["family"], "expect": case["expect"], "results": results})
        rows += results

    absent = []
    if not only_family:
        for phrasing in cases.get("absent") or []:
            found = discover(phrasing, limit=1)["candidates"]
            absent.append({"phrasing": phrasing,
                           "top_key": found[0]["key"] if found else None,
                           "top_score": found[0]["score"] if found else 0.0})

    total = len(rows) or 1
    return {
        "families": families,
        "rows": rows,
        "absent": absent,
        "n": len(rows),
        "recall@1": sum(r["hit@1"] for r in rows) / total,
        "recall@3": sum(r["hit@3"] for r in rows) / total,
        "recall@8": sum(r["hit@8"] for r in rows) / total,
        # Found by the search at all -- the ceiling any reranking change can reach.
        "in_pool": sum(r["pool_rank"] is not None for r in rows) / total,
    }


def report(summary: Dict[str, Any], show_failures: bool = False) -> None:
    print(f"\ndiscovery eval -- {summary['n']} phrasings, "
          f"{len(summary['families'])} concepts\n")
    print(f"  recall@1  {summary['recall@1']:6.1%}   (the planner's first choice)")
    print(f"  recall@3  {summary['recall@3']:6.1%}")
    print(f"  recall@8  {summary['recall@8']:6.1%}   (what reaches the prompt)")
    print(f"  in pool   {summary['in_pool']:6.1%}   (found by search; ceiling for any reranking)")

    print(f"\n{'family':28s} {'@1':>5s} {'@3':>5s} {'@8':>5s}  worst pool rank")
    print("-" * 66)
    for family in summary["families"]:
        results = family["results"]
        n = len(results)
        ranks = [r["pool_rank"] for r in results]
        worst = "MISS" if any(r is None for r in ranks) else str(max(ranks))
        flag = "  <--" if sum(r["hit@1"] for r in results) < n else ""
        print(f"{family['family']:28s} {sum(r['hit@1'] for r in results)}/{n:<3d} "
              f"{sum(r['hit@3'] for r in results)}/{n:<3d} "
              f"{sum(r['hit@8'] for r in results)}/{n:<3d}  {worst:>8s}{flag}")

    if show_failures:
        print("\nphrasings that miss the top 8:")
        for row in summary["rows"]:
            if row["hit@8"]:
                continue
            rank = row["pool_rank"]
            where = f"pool rank {rank}/{row['pool_size']}" if rank else "NOT IN POOL"
            print(f"  {row['family']:26s} {row['phrasing'][:44]:44s} {where:22s} "
                  f"got: {row['top_key']}")
        print("\nphrasings that reach the top 8 but not the top 1:")
        for row in summary["rows"]:
            if row["hit@1"] or not row["hit@8"]:
                continue
            print(f"  {row['family']:26s} {row['phrasing'][:44]:44s} "
                  f"pool rank {row['pool_rank']:<3d} got: {row['top_key']}")

    if summary["absent"]:
        scores = [a["top_score"] for a in summary["absent"]]
        print("\nconcepts the corpus does NOT hold -- what comes back anyway:")
        for entry in summary["absent"]:
            print(f"  {entry['phrasing'][:36]:36s} -> {str(entry['top_key'])[:40]:40s} "
                  f"score {entry['top_score']:.2f}")
        present = [r["top_score"] for r in summary["rows"] if r["pool_rank"] == 1]
        if present:
            print(f"\n  absent  top score: median {statistics.median(scores):.2f}")
            print(f"  present top score: median {statistics.median(present):.2f}"
                  "   (a gap here is what a confidence threshold could use)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--failures", action="store_true", help="list every phrasing that misses")
    parser.add_argument("--family", help="run one concept family only")
    args = parser.parse_args(argv)

    if not DUCKDB_PATH.exists():
        print(f"{DUCKDB_PATH} not found -- run `python -m backend.lakehouse.build` first")
        return 1

    report(run(load_cases(), only_family=args.family), show_failures=args.failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
