# HANDOFF — Stage 1 complete (2026-07-19)

## State

Stage 1 is done and measured: synthetic ABL corpus (labeled synthetic,
checkpoint-reviewed), frozen 20-case eval set (sub-agent-verified against the
corpus **before** retrieval existed), ingest (procedure-level chunks,
structured schema, call graph with `RUN VALUE` honestly unresolved,
table-touch analysis), hybrid local retrieval (identifier-aware BM25 + numpy
LSA embeddings + RRF + structure features), and a saved, re-runnable
scoreboard. 40 unittest cases green. Nothing pushed; Sharon commits from
PowerShell.

## The score (real runs, 2026-07-19, saved in evals/results/)

Headline — hybrid with structure features, shipped defaults:
**micro recall@5 = 0.857, micro recall@10 = 1.000, strict case recall@5 =
0.850, precision@5 = 0.580, MRR = 0.713** over 20 cases / 35 gold targets.
Ablations: bm25+structure 0.857 R@5 (MRR 0.727), LSA-embed+structure 0.771,
text-only hybrid 0.743 (structure features = +0.114 R@5). Remaining top-5
misses at ranks 6–10: S4, C2, F4.

Caveats that must travel with the number: the embedding backend is numpy LSA
(PyPI was unreachable — HTTP 403 — in the build environment), the corpus is
35 retrievable units, and retrieval parameters were tuned against the eval
set by design with every knob disclosed in the scoreboard `params` and frozen
as code defaults. The eval questions/gold never changed after pre-build
verification. ABL is expert-review-verified, not compiler-verified (no
OpenEdge runtime available).

## How to run the eval

From the repo root (Python ≥ 3.10, numpy):

```
python -m openedge_agent.ingest     # rebuild index/ (~1s, deterministic)
python -m openedge_agent.score      # reproduces the headline hybrid row
python -m openedge_agent.score --all
python -m unittest discover -s tests
```

Optional: `pip install sentence-transformers` on a normal-network machine,
re-run ingest + score → neural embed leg; outputs record the backend.

## Verification status

- Fresh-context sub-agent audited all 20 eval answers against the corpus
  (20/20) and a second one reviewed the ABL for realism — pre-retrieval.
- A fresh-context sub-agent reproduced the scoreboard from the docs alone
  (post-build milestone). It CAUGHT a stale-index error: the tokenizer had
  changed after the last ingest, so the previously drafted P@5/MRR values
  were off by ~0.01. The index was rebuilt, all four scoreboards re-run
  (two identical passes), and the numbers above are the reproduced values.
  It also caught two documentation miscounts (".p" file count, category
  count), both fixed. All recall metrics, counts, and honesty disclosures
  reproduced exactly as claimed.
- NOT verified: behavior on Sharon's machine (Windows), the
  sentence-transformers path (unreachable here), ABL compilation.

## Stage 2 — exact first step

Stage 2 is the citation-bearing knowledge agent over this retrieval. First
step, concretely:

1. Create `openedge_agent/agent.py` with one function
   `ask(question: str) -> {answer, citations}`:
   retrieve top-10 via `Retriever()` (frozen defaults), assemble a context
   block of chunk texts + x-refs *with their unit ids*, prompt Claude with a
   template that REQUIRES every claim to cite unit ids it was given, parse
   citations back out.
2. Before writing the prompt, write `evals/answers.jsonl` — reuse the same 20
   questions; gold citations = the existing gold targets; add a short
   free-text "must mention" list per case. Same eval-first discipline.
3. Score: citation precision/recall against gold (the Stage 1 matcher in
   `score.py` already implements the id-matching semantics — reuse
   `target_matches`), plus answer-correctness judged against "must mention".
4. Budget note: the agent needs an Anthropic API key (first external
   dependency). Keep the scoreboard runnable without it (retrieval-only mode
   stays offline).

The retrieval scoreboard is the regression harness for all of Stage 2: any
agent change that degrades retrieval shows up as a moved number.
