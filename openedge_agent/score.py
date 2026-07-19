"""Retrieval scoreboard: run the frozen eval set through retrieval and
report recall/precision/MRR. The saved JSON is the re-runnable Stage 1
"ruler" that Stage 2 builds on.

Usage:
    python -m openedge_agent.score                 # hybrid (default)
    python -m openedge_agent.score --mode bm25
    python -m openedge_agent.score --mode embed
    python -m openedge_agent.score --all           # all three, one report each

Match semantics (per evals/README.md):
    schema:X    matches only the schema unit itself
    path#unit   matches only that exact chunk
    path        (file-level) matches any chunk of that file
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .retrieve import Retriever

KS = (1, 5, 10)


def target_matches(target: str, retrieved_id: str) -> bool:
    if target.startswith("schema:") or "#" in target:
        return retrieved_id == target
    return retrieved_id == target or retrieved_id.startswith(target + "#")


def score_case(case: dict, hits: list[dict]) -> dict:
    ids = [h["id"] for h in hits]
    gold_ranks: dict[str, int | None] = {}
    for g in case["gold"]:
        rank = next(
            (i + 1 for i, rid in enumerate(ids) if target_matches(g, rid)), None
        )
        gold_ranks[g] = rank
    relevant = case["gold"] + case.get("acceptable", [])
    top5 = ids[:5]
    p5_hits = sum(1 for rid in top5 if any(target_matches(t, rid) for t in relevant))
    ranks_found = [r for r in gold_ranks.values() if r is not None]
    return {
        "id": case["id"],
        "category": case["category"],
        "question": case["question"],
        "gold_ranks": gold_ranks,
        "first_gold_rank": min(ranks_found) if ranks_found else None,
        "precision_at_5": p5_hits / 5,
        "top10": ids,
    }


def aggregate(details: list[dict], cases: list[dict]) -> dict:
    total_gold = sum(len(c["gold"]) for c in cases)
    metrics: dict = {}
    for k in KS:
        hit = sum(
            1
            for d in details
            for r in d["gold_ranks"].values()
            if r is not None and r <= k
        )
        metrics[f"micro_recall@{k}"] = round(hit / total_gold, 4)
    metrics["strict_case_recall@5"] = round(
        sum(
            1
            for d in details
            if all(r is not None and r <= 5 for r in d["gold_ranks"].values())
        )
        / len(details),
        4,
    )
    metrics["precision@5"] = round(
        sum(d["precision_at_5"] for d in details) / len(details), 4
    )
    metrics["mrr"] = round(
        sum(1 / d["first_gold_rank"] for d in details if d["first_gold_rank"])
        / len(details),
        4,
    )
    by_cat: dict[str, dict] = {}
    for cat in sorted({d["category"] for d in details}):
        cat_details = [d for d in details if d["category"] == cat]
        cat_gold = sum(
            len(c["gold"]) for c in cases if c["category"] == cat
        )
        cat_hit5 = sum(
            1
            for d in cat_details
            for r in d["gold_ranks"].values()
            if r is not None and r <= 5
        )
        by_cat[cat] = {
            "cases": len(cat_details),
            "micro_recall@5": round(cat_hit5 / cat_gold, 4),
        }
    return {"metrics": metrics, "by_category": by_cat}


def run(mode: str, root: Path, name_bonus: float = 0.005, w_bm25: float = 1.5,
        w_embed: float = 1.0, rrf_k: int = 30, bm25_k1: float = 1.2,
        bm25_b: float = 0.3, graph_weight: float = 0.3,
        touch_bonus: float = 0.04, label: str | None = None) -> dict:
    cases = [
        json.loads(line)
        for line in (root / "evals/retrieval.jsonl").read_text().splitlines()
        if line.strip()
    ]
    r = Retriever(
        index_dir=root / "index",
        mode=mode,
        rrf_k=rrf_k,
        w_bm25=w_bm25,
        w_embed=w_embed,
        name_bonus=name_bonus,
        bm25_k1=bm25_k1,
        bm25_b=bm25_b,
        graph_weight=graph_weight,
        touch_bonus=touch_bonus,
    )
    details = [score_case(c, r.search(c["question"], k=max(KS))) for c in cases]
    agg = aggregate(details, cases)
    report = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "embed_backend": r.embed_backend,
        "params": {
            "rrf_k": rrf_k,
            "w_bm25": w_bm25,
            "w_embed": w_embed,
            "name_bonus": name_bonus,
            "bm25_k1": bm25_k1,
            "bm25_b": bm25_b,
            "graph_weight": graph_weight,
            "touch_bonus": touch_bonus,
        },
        "n_cases": len(cases),
        "n_gold_targets": sum(len(c["gold"]) for c in cases),
        **agg,
        "cases": details,
    }
    out = root / "evals/results" / f"scoreboard_{label or mode}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))
    return report


def print_report(rep: dict) -> None:
    m = rep["metrics"]
    print(
        f"[{rep['mode']}] backend={rep['embed_backend'] or '-'} "
        f"params={rep['params']}"
    )
    print(
        f"  micro recall@1/5/10 : {m['micro_recall@1']:.3f} / "
        f"{m['micro_recall@5']:.3f} / {m['micro_recall@10']:.3f}"
    )
    print(f"  strict case recall@5: {m['strict_case_recall@5']:.3f}")
    print(f"  precision@5         : {m['precision@5']:.3f}")
    print(f"  MRR                 : {m['mrr']:.3f}")
    for cat, cm in rep["by_category"].items():
        print(f"    {cat:<15} recall@5 {cm['micro_recall@5']:.3f} "
              f"({cm['cases']} cases)")
    misses = [
        d["id"]
        for d in rep["cases"]
        if any(r is None or r > 5 for r in d["gold_ranks"].values())
    ]
    if misses:
        print(f"  cases with a gold target outside top-5: {', '.join(misses)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", default="hybrid", choices=["bm25", "embed", "hybrid"])
    ap.add_argument("--all", action="store_true", help="run bm25, embed, hybrid")
    ap.add_argument("--root", default=".")
    ap.add_argument("--name-bonus", type=float, default=0.005)
    ap.add_argument("--w-bm25", type=float, default=1.5)
    ap.add_argument("--w-embed", type=float, default=1.0)
    ap.add_argument("--rrf-k", type=int, default=30)
    ap.add_argument("--bm25-k1", type=float, default=1.2)
    ap.add_argument("--bm25-b", type=float, default=0.3)
    ap.add_argument("--graph-weight", type=float, default=0.3)
    ap.add_argument("--touch-bonus", type=float, default=0.04)
    ap.add_argument("--label", default=None,
                    help="output file suffix (default: the mode)")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    modes = ["bm25", "embed", "hybrid"] if args.all else [args.mode]
    for mode in modes:
        rep = run(mode, root, args.name_bonus, args.w_bm25, args.w_embed,
                  args.rrf_k, args.bm25_k1, args.bm25_b, args.graph_weight,
                  args.touch_bonus, args.label if not args.all else None)
        print_report(rep)


if __name__ == "__main__":
    main()
