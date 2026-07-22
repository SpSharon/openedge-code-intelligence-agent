"""Stage 2 answer scoreboard: run the agent over an answer eval set and
score citation accuracy + must-mention answer correctness.

Metrics
    citation recall     fraction of gold targets matched by >=1 citation
                        (micro, over all gold targets; target_matches
                        semantics from openedge_agent.score)
    citation precision  fraction of emitted citations that match a gold
                        or acceptable target (micro, over all citations)
    answer correctness  fraction of each case's must_mention facts the
                        answer states (case-insensitive regex, any-of
                        per fact), macro-averaged over cases

The must_mention matcher is deliberately judge-free: each fact carries a
list of alternative patterns and counts as covered if ANY matches the
answer text. There is NO LLM grader in the primary metric — per-fact
misses are stored in the scoreboard so a human can eyeball whether a
miss is real or a phrasing gap.

Modes
    --dry-run   OFFLINE. No LLM. Reports the retrieval ceiling (which
                gold targets are in the top-k context) and the cost
                estimate for a real run.
    --fake      OFFLINE pipeline smoke test with a scripted FakeLLM.
                The saved scoreboard is labelled fake; its "scores"
                validate plumbing, never answer quality.
    (default)   Real run via the Anthropic API. Prints the cost
                estimate and asks for confirmation before spending
                (skip the prompt with --yes). Model + meter usage are
                stamped into the saved scoreboard.

Usage (from the repo root):
    python evals/answer_score.py --dry-run
    python evals/answer_score.py                # real; needs ANTHROPIC_API_KEY
    python evals/answer_score.py --cases evals/answers_heldout.jsonl --label heldout
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openedge_agent.agent import REFUSAL_PHRASE, Agent  # noqa: E402
from openedge_agent.llm import (  # noqa: E402
    AnthropicLLM,
    DEFAULT_MODEL,
    FakeLLM,
    approx_tokens,
    price_for,
)
from openedge_agent.score import target_matches  # noqa: E402


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def score_citations(case: dict, citations: list[str]) -> dict:
    gold = case["gold"]
    relevant = gold + case.get("acceptable", [])
    gold_cited = [
        g for g in gold if any(target_matches(g, c) for c in citations)
    ]
    good = [c for c in citations if any(target_matches(t, c) for t in relevant)]
    return {
        "n_gold": len(gold),
        "n_gold_cited": len(gold_cited),
        "gold_missed": [g for g in gold if g not in gold_cited],
        "n_citations": len(citations),
        "n_good_citations": len(good),
        "bad_citations": [c for c in citations if c not in good],
    }


def _normalize(text: str) -> str:
    """Strip markdown emphasis / code ticks and collapse whitespace so the
    matcher is not fooled by **bold** or `backticks` around the literal terms
    a fact looks for (e.g. `Customer` table, **not** in the retrieved code)."""
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def score_must_mention(case: dict, answer: str) -> dict:
    text = _normalize(answer)
    hits, misses = [], []
    for item in case["must_mention"]:
        if any(re.search(pat, text, re.IGNORECASE) for pat in item["any"]):
            hits.append(item["note"])
        else:
            misses.append(item["note"])
    total = len(case["must_mention"])
    return {
        "n_facts": total,
        "n_covered": len(hits),
        "coverage": len(hits) / total if total else None,
        "missed": misses,
    }


def gold_in_context(case: dict, retrieved_ids: list[str]) -> dict:
    present = [
        g for g in case["gold"]
        if any(target_matches(g, rid) for rid in retrieved_ids)
    ]
    return {
        "n_gold_in_context": len(present),
        "gold_absent": [g for g in case["gold"] if g not in present],
    }


def aggregate(rows: list[dict]) -> dict:
    n_gold = sum(r["citation"]["n_gold"] for r in rows)
    n_gold_cited = sum(r["citation"]["n_gold_cited"] for r in rows)
    n_cites = sum(r["citation"]["n_citations"] for r in rows)
    n_good = sum(r["citation"]["n_good_citations"] for r in rows)
    coverages = [r["must_mention"]["coverage"] for r in rows
                 if r["must_mention"]["coverage"] is not None]
    n_gold_ctx = sum(r["context"]["n_gold_in_context"] for r in rows)
    agg = {
        "citation_recall": round(n_gold_cited / n_gold, 4) if n_gold else None,
        "citation_precision": round(n_good / n_cites, 4) if n_cites else None,
        "answer_correctness": round(sum(coverages) / len(coverages), 4)
        if coverages else None,
        "perfect_answer_cases": sum(
            1 for r in rows if r["must_mention"]["coverage"] == 1.0
        ),
        "gold_in_context_rate": round(n_gold_ctx / n_gold, 4) if n_gold else None,
        "refusals": sum(1 for r in rows if REFUSAL_PHRASE in _normalize(r["answer"]).lower()),
        "invalid_citation_total": sum(len(r["invalid_citations"]) for r in rows),
        "n_cases": len(rows),
        "n_gold_targets": n_gold,
        "n_citations_total": n_cites,
    }
    by_cat: dict[str, dict] = {}
    for cat in sorted({r["category"] for r in rows}):
        cr = [r for r in rows if r["category"] == cat]
        cg = sum(r["citation"]["n_gold"] for r in cr)
        cgc = sum(r["citation"]["n_gold_cited"] for r in cr)
        ccov = [r["must_mention"]["coverage"] for r in cr
                if r["must_mention"]["coverage"] is not None]
        by_cat[cat] = {
            "cases": len(cr),
            "citation_recall": round(cgc / cg, 4) if cg else None,
            "answer_correctness": round(sum(ccov) / len(ccov), 4)
            if ccov else None,
        }
    return {"metrics": agg, "by_category": by_cat}


# ---------------------------------------------------------------------------
# cost estimate
# ---------------------------------------------------------------------------


def estimate_cost(agent: Agent, cases: list[dict], model: str,
                  out_tokens_per_case: int = 350) -> dict:
    total_in = 0
    for case in cases:
        hits = agent.retriever.search(case["question"], k=agent.k)
        system, user = agent.build_prompt(case["question"], [h["id"] for h in hits])
        total_in += approx_tokens(system) + approx_tokens(user)
    total_out = out_tokens_per_case * len(cases)
    pi, po = price_for(model)
    return {
        "model": model,
        "calls": len(cases),
        "est_input_tokens": total_in,
        "est_output_tokens": total_out,
        "est_cost_usd": round((total_in * pi + total_out * po) / 1_000_000, 4),
        "note": "chars/4 token estimate; actual metered usage is stamped "
                "into the scoreboard after the run",
    }


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()]


def run_cases(agent: Agent, cases: list[dict]) -> list[dict]:
    rows = []
    for case in cases:
        out = agent.ask(case["question"])
        row = {
            "id": case["id"],
            "category": case["category"],
            "question": case["question"],
            "answer": out["answer"],
            "citations": out["citations"],
            "invalid_citations": out["invalid_citations"],
            "retrieved": out["retrieved"],
            "citation": score_citations(case, out["citations"]),
            "must_mention": score_must_mention(case, out["answer"]),
            "context": gold_in_context(case, out["retrieved"]),
        }
        rows.append(row)
        mm = row["must_mention"]
        cov = "-" if mm["coverage"] is None else f"{mm['coverage']:.2f}"
        print(f"  {case['id']:<4} facts {mm['n_covered']}/{mm['n_facts']} "
              f"({cov})  gold cited {row['citation']['n_gold_cited']}"
              f"/{row['citation']['n_gold']}  cites {row['citation']['n_citations']}")
    return rows


def print_summary(agg: dict, meter: dict | None) -> None:
    m = agg["metrics"]

    def fmt(v):
        return "-" if v is None else f"{v:.3f}"

    print()
    print(f"  answer correctness (must-mention, macro): {fmt(m['answer_correctness'])}"
          f"  ({m['perfect_answer_cases']}/{m['n_cases']} cases fully covered)")
    print(f"  citation recall  (micro over gold)      : {fmt(m['citation_recall'])}")
    print(f"  citation precision (micro over cites)   : {fmt(m['citation_precision'])}")
    print(f"  gold-in-context rate (retrieval ceiling): {fmt(m['gold_in_context_rate'])}")
    print(f"  refusals: {m['refusals']}   invalid citations: "
          f"{m['invalid_citation_total']}")
    for cat, cm in agg["by_category"].items():
        print(f"    {cat:<15} cite-recall {fmt(cm['citation_recall'])}  "
              f"correctness {fmt(cm['answer_correctness'])}  ({cm['cases']} cases)")
    if meter:
        print(f"  usage: {meter}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases", default=str(ROOT / "evals/answers.jsonl"))
    ap.add_argument("--index", default=str(ROOT / "index"))
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--label", default=None,
                    help="scoreboard filename suffix (default: dev|fake|heldout "
                         "from the cases filename)")
    ap.add_argument("--dry-run", action="store_true",
                    help="offline: retrieval ceiling + cost estimate only")
    ap.add_argument("--fake", action="store_true",
                    help="offline plumbing smoke test with FakeLLM")
    ap.add_argument("--yes", action="store_true",
                    help="skip the pre-spend confirmation prompt")
    args = ap.parse_args()

    cases = load_cases(Path(args.cases))
    print(f"{len(cases)} cases from {args.cases}")

    if args.fake:
        def scripted(system: str, user: str) -> str:
            ids = re.findall(r"^\[((?:corpus/|schema:)[^\]]+)\]", user,
                             flags=re.MULTILINE)
            return ("FAKE plumbing answer citing the top units.\n"
                    f"CITATIONS: {'; '.join(ids[:2])}")
        llm = FakeLLM(scripted)
    elif args.dry_run:
        llm = FakeLLM()  # never called
    else:
        llm = AnthropicLLM(model=args.model)

    agent = Agent(index_dir=args.index, llm=llm, k=args.k)

    est = estimate_cost(agent, cases, args.model)
    print(f"cost estimate for a real run: ~${est['est_cost_usd']} "
          f"({est['calls']} calls, ~{est['est_input_tokens']} in / "
          f"~{est['est_output_tokens']} out tokens, {est['model']})")

    if args.dry_run:
        print("\n-- dry run: retrieval ceiling only (no LLM calls) --")
        n_gold = n_ctx = 0
        for case in cases:
            hits = agent.retriever.search(case["question"], k=agent.k)
            ctx = gold_in_context(case, [h["id"] for h in hits])
            n_gold += len(case["gold"])
            n_ctx += ctx["n_gold_in_context"]
            absent = (" MISSING: " + ", ".join(ctx["gold_absent"])
                      if ctx["gold_absent"] else "")
            print(f"  {case['id']:<4} gold in top-{agent.k}: "
                  f"{ctx['n_gold_in_context']}/{len(case['gold'])}{absent}")
        print(f"\n  gold-in-context rate: {n_ctx}/{n_gold} "
              f"= {n_ctx / n_gold:.3f}  <- the answer/citation ceiling")
        return

    if not args.fake and not args.yes:
        reply = input("proceed and spend? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("aborted before spending.")
            return

    rows = run_cases(agent, cases)
    agg = aggregate(rows)
    meter = agent.llm.meter.as_dict()
    print_summary(agg, meter)

    label = args.label
    if label is None:
        stem = Path(args.cases).stem  # answers | answers_heldout
        label = "heldout" if "heldout" in stem else "dev"
        if args.fake:
            label = f"fake_{label}"
    report = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": "answers",
        "label": label,
        "cases_file": str(Path(args.cases).name),
        "llm": meter,
        "fake": bool(args.fake),
        "params": {"k": agent.k, "retriever": "frozen Stage 1 defaults"},
        "cost_estimate_before_run": est,
        **agg,
        "cases": rows,
    }
    out = ROOT / "evals/results" / f"answers_scoreboard_{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))
    print(f"\nsaved {out.relative_to(ROOT)}")
    if args.fake:
        print("NOTE: --fake run. Plumbing only; these numbers say nothing "
              "about answer quality.")


if __name__ == "__main__":
    main()
