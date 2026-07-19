# OpenEdge Code-Intelligence Agent

An agent that understands a legacy Progress 4GL / OpenEdge (ABL) codebase and
answers questions about it with citations. This repo is **Stage 1** of a staged
build: the retrieval foundation and the evaluation set — no LLM answer agent
yet. The measure exists before the thing it measures.

## How this is built (read this first)

Two things are true and stated plainly:

- **The corpus is synthetic.** Everything under `corpus/` is a generated
  Progress 4GL codebase (order management for a fictional distributor),
  created to exercise this tool. It is not, and does not derive from, any real
  or employer system. See `corpus/README.md`.
- **Human-directed, AI-implemented.** Sharon Paul specifies the requirements,
  directs the build, and verifies the results — including reading the
  generated ABL with a working knowledge of Progress 4GL. Implementation is by
  Claude (Anthropic's Fable/Claude Code tooling). Verification additionally
  uses fresh-context AI review passes, and every reported retrieval number
  comes from an actual run of the scoreboard, never from self-assessment.

## Stage 1 retrieval score

Run of 2026-07-19 over the frozen 20-case eval set (35 gold targets), local
LSA embedding backend (see honesty notes below); saved reports in
`evals/results/`:

| configuration | micro R@5 | micro R@10 | strict case R@5 | P@5 | MRR |
|---|---|---|---|---|---|
| **hybrid + structure (shipped defaults)** | **0.857** | **1.000** | **0.850** | 0.580 | 0.713 |
| bm25 + structure | 0.857 | 1.000 | 0.850 | 0.580 | 0.727 |
| embed(LSA) + structure | 0.771 | 0.943 | 0.700 | 0.530 | 0.625 |
| hybrid, text-only (no structure features) | 0.743 | 0.857 | 0.700 | 0.470 | 0.580 |

These are the values a fresh-context reproduction audit obtained by running
the commands below from a clean index rebuild (the audit caught an earlier
stale-index version of this table — see `docs/BUILD_MEMORY.md`); two
back-to-back passes produce identical numbers.

Reading it honestly: every gold target lands in the top 10, and 4 of 5
categories sit at 0.86–1.00 recall@5 — call-graph is the laggard at 0.667.
The **structure features** (score propagation over the ingest call graph +
table-touch query routing) are worth **+0.114 recall@5** over text-only
ranking — the call graph is retrieval signal, not just Stage 2 groundwork.
With the LSA fallback the embedding leg adds nothing over BM25 (same R@5;
MRR marginally better without it); the hybrid's value is contingent on a
real neural encoder, which this build environment could not download
(below). Remaining top-5 misses, at ranks 6–10: S4, C2, F4 — the
outbound-call question (C2) is exactly the class the Stage 2 graph-walking
agent exists for.

Metric definitions, gold-vs-acceptable semantics, and the per-case reports
live in `evals/README.md` and `evals/results/*.json`.

### Honesty notes on the score

- **Embedding backend is LSA, not a neural model.** The build environment's
  package registry was unreachable (PyPI 403), so `sentence-transformers`
  could not be installed. The embed leg is a hand-rolled numpy LSA (TF-IDF +
  SVD) fitted on the corpus — a real local embedding, honestly weaker on pure
  paraphrase. The backend that produced every index and score is recorded in
  `index/embed_meta.json` and in each scoreboard JSON. Installing
  `sentence-transformers` and re-running `ingest` + `score` swaps in the
  neural encoder with no code changes.
- **The 20 cases are a development set, not a held-out test set.** Retrieval was
  tuned against them by design, so read 0.857 as an in-sample, dev-tuned figure —
  not an estimate of held-out performance. Crucially, only the eight scalar knobs
  (BM25 k1/b, RRF k, source weights, exact-name bonus, graph-propagation weight,
  table-touch bonus — all disclosed in each scoreboard's `params`, frozen as
  defaults) are eval-tuned; the retrieval *logic* is general, driven by static
  analysis of the corpus with no access to the eval questions or answers. The
  questions and gold were written and independently verified *before* any
  retrieval code existed and never changed. The tuning-independent result is the
  ablation above — structure adds +0.114 R@5, and text plateaus regardless of
  parameters. A held-out split and a larger corpus are the next rigor step.
- The corpus is small (35 retrievable units). Absolute numbers on a corpus
  this size say "the machinery works and is measured," not "this generalizes
  to 2M lines of ABL."

## What Stage 1 contains

| Piece | Where |
|---|---|
| Synthetic ABL corpus: 6 tables + 2 sequences (.df), 10 `.p`, 1 `.cls`, 2 includes | `corpus/` |
| Frozen eval set: 20 questions, 35 gold targets, sub-agent-verified pre-build | `evals/retrieval.jsonl` |
| Ingest: comment-aware ABL chunking (27 units), structured schema, call graph (28 edges, `RUN VALUE` left unresolved on purpose), table-touch analysis | `openedge_agent/abl.py`, `ingest.py` |
| Hybrid retrieval: identifier-aware BM25 + local embeddings + RRF + structure features | `openedge_agent/retrieve.py` |
| Scoreboard: recall@k / strict case recall / precision@5 / MRR, per-category, saved JSON | `openedge_agent/score.py` |
| Tests: 40 stdlib-unittest cases (parsing, ingest integration, retrieval, metrics) | `tests/` |

## Running it

Requires Python ≥ 3.10 and numpy (`pip install -r requirements.txt`). No
install step — run from the repo root:

```
python -m openedge_agent.ingest        # corpus -> index/  (~1s)
python -m openedge_agent.score         # eval -> printed report + evals/results/*.json
python -m openedge_agent.score --all   # bm25 / embed / hybrid ablation
python -m openedge_agent.retrieve "what calls ar-invoice.p?"   # ad-hoc query
python -m unittest discover -s tests   # test suite
```

`python -m openedge_agent.score` with no flags reproduces the headline hybrid
row above (deterministic on the same machine/numpy).

Optional neural embeddings: `pip install sentence-transformers`, then re-run
`ingest` and `score`; the backend recorded in the outputs will change from
`lsa-numpy` to the model name.

## Layout

```
corpus/              synthetic ABL codebase (PROPATH root; corpus/README.md
                     is meta-documentation and is NOT indexed)
evals/               frozen eval set + conventions + saved score reports
                     (the answer key lives here, never under corpus/)
openedge_agent/      the Python package (abl, ingest, retrieve, score)
tests/               stdlib unittest suite
docs/                staged plan, Stage 1 brief, build memory
index/               generated artifacts (gitignored; rebuilt by ingest)
HANDOFF.md           state + exact next step for Stage 2
```

## Roadmap

Stage 1 (this repo): measured retrieval foundation — done. Stage 2: a
Claude-powered agent over this retrieval that answers with citations
(file + procedure), scored for answer and citation accuracy; `HANDOFF.md`
has the exact first step. Stage 3 (optional): agentic tools — read-file,
search, call-graph walk — plus tracing. See `docs/OPENEDGE_AGENT_PLAN.md`.
