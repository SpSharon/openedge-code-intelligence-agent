"""Scoreboard pipeline: evals/results/answers_scoreboard_*.json -> pandas -> CSV.

    python -m service.pipeline evals/results --out runs.csv
    python -m service.pipeline evals/results --cases --out cases.csv

One row per run (default) or one row per case (--cases). Fields that were
added to the scoreboard format later (params.agent, cases[].steps, ...) are
read with .get(), so the two earliest scoreboards produce NaN/None there
instead of a KeyError. The retrieval scoreboards (scoreboard_*.json) have a
different shape and are not matched by the glob.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

GLOB = "answers_scoreboard_*.json"
METRICS = [
    "answer_correctness",
    "citation_recall",
    "citation_precision",
    "refusals",
    "invalid_citation_total",
    "n_cases",
]


def load_scoreboards(results_dir: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(Path(results_dir).glob(GLOB))]


def runs_table(boards: list[dict]) -> pd.DataFrame:
    rows = []
    for b in boards:
        llm = b.get("llm") or {}
        params = b.get("params") or {}
        metrics = b.get("metrics") or {}
        row = {
            "label": b.get("label"),
            "run_at": b.get("run_at"),
            "model": llm.get("model"),
            "agent": params.get("agent"),
            "calls": llm.get("calls"),
            "cost_usd": llm.get("estimated_cost_usd"),
        }
        row.update({m: metrics.get(m) for m in METRICS})
        rows.append(row)
    df = pd.DataFrame(rows, columns=["label", "run_at", "model", "agent",
                                     "calls", "cost_usd", *METRICS])
    return df.sort_values(["agent", "label"]).reset_index(drop=True)


def cases_table(boards: list[dict]) -> pd.DataFrame:
    rows = []
    for b in boards:
        for c in b.get("cases") or []:
            cit = c.get("citation") or {}
            rows.append({
                "label": b.get("label"),
                "case_id": c.get("id"),
                "category": c.get("category"),
                "n_citations": len(c.get("citations") or []),
                "n_invalid": len(c.get("invalid_citations") or []),
                "n_gold_missed": len(cit.get("gold_missed") or []),
                "steps": c.get("steps"),
            })
    df = pd.DataFrame(rows, columns=["label", "case_id", "category",
                                     "n_citations", "n_invalid",
                                     "n_gold_missed", "steps"])
    # nullable integer: whole step counts, <NA> where a board predates steps
    df["steps"] = df["steps"].astype("Int64")
    return df


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m service.pipeline",
                                 description="Scoreboard JSON -> CSV")
    ap.add_argument("results_dir", type=Path)
    ap.add_argument("--out", default="runs.csv")
    ap.add_argument("--cases", action="store_true",
                    help="one row per case instead of one row per run")
    args = ap.parse_args(argv)

    boards = load_scoreboards(args.results_dir)
    if not boards:
        print(f"error: no {GLOB} files in {args.results_dir}", file=sys.stderr)
        return 1
    df = cases_table(boards) if args.cases else runs_table(boards)
    df.to_csv(args.out, index=False)
    with pd.option_context("display.float_format", "{:.4f}".format):
        print(df.to_string(index=False))
    print(f"{len(df)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
