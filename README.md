# OpenEdge Code-Intelligence Agent

_Built by **Sharon Paul** · [github.com/SpSharon](https://github.com/SpSharon)_

An agent that understands a legacy Progress 4GL / OpenEdge (ABL) codebase and
answers plain-English questions about it with citations — and refuses to guess
when the answer isn't in the retrieved code. Legacy 4GL is hard to navigate
and poorly served by modern tooling, so an accurate, *grounded* code assistant
is genuinely useful.

The repo is built in three measured stages, each with its own scoreboard:

- **Stage 1** — the retrieval foundation, and the evaluation set it is scored
  against.
- **Stage 2** — the citation-bearing answer agent over that retrieval, with an
  answer/citation scoreboard of its own.
- **Stage 3** — a tool-using agent (`read_unit` / `walk_calls` / `search`) over
  the frozen Stage 1 index. It emits the same answer/citation contract as
  Stage 2, so the unchanged scorer runs both head-to-head.

The measure exists before the thing it measures — at every stage the eval set
was written and independently verified before the code it scores. The Stage 3
comparison is reported the way it came out rather than the way it was hoped:
the tools deliver reliability where single-shot retrieval is incomplete, and
nothing where it is already complete. See
[Stage 3 — the tool-using agent](#stage-3--the-tool-using-agent).

## How this is built (read this first)

Two things are true and stated plainly:

- **The corpus is synthetic.** Everything under `corpus/` is a generated
  Progress 4GL codebase (order management for a fictional distributor),
  created to exercise this tool. It is not, and does not derive from, any real
  or employer system. See `corpus/README.md`.
- **Human-directed, AI-implemented.** Sharon Paul specifies the requirements,
  directs the build, and verifies the results — including reading the
  generated ABL with a working knowledge of Progress 4GL. Implementation is by
  Claude Code (Anthropic's AI coding agent). Verification additionally
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

## Stage 2 — the citation-bearing agent

`openedge_agent/agent.py` is the simplest thing that answers with receipts:
retrieve top-10 with the frozen Stage 1 retriever, build a context block of
the retrieved units' text + x-refs each labelled with its unit id, make ONE
LLM call, and parse the citations back out. No agent loop, no tools, no UI —
that is Stage 3.

The grounding contract, enforced by the prompt and checked by the parser and
the eval: answer only from the retrieved units; cite exactly the unit ids the
answer draws on; if it isn't in the retrieved context, say "not in the
retrieved code" rather than invent a procedure, table, or behavior. Citations
naming units that were *not* retrieved are stripped from `citations` and
surfaced in `invalid_citations` — hallucinated cites become a visible count,
not a silently repaired output.

Scoring (`evals/answer_score.py`, over `evals/answers.jsonl` — the same
frozen 20 questions, gold citations identical to the retrieval gold,
test-enforced):

- **Citation recall / precision** — did the answer cite the gold targets, and
  were its citations gold-or-acceptable rather than noise (same
  `target_matches` semantics as Stage 1).
- **Answer correctness** — per-case `must_mention` facts (any-of
  case-insensitive regex per fact, macro-averaged). Deliberately judge-free:
  no LLM grader touches the headline number; per-fact misses are saved in the
  scoreboard for human review. The fact list was audited by a fresh-context
  AI reviewer against the corpus before any agent run.

The LLM client is pluggable (`openedge_agent/llm.py`): a real Anthropic
backend (key from the environment only) and a scripted `FakeLLM`, so the
whole pipeline — retrieval, grounding, parsing, scoring — builds and tests
offline. A real run prints a cost estimate and asks before spending, and
stamps the model + metered token usage into the saved scoreboard.

The agent is scored on two sets: the frozen 20-case **dev** set
(`evals/answers.jsonl`, the prompt was written against it) and a 9-case
**held-out** set (`evals/answers_heldout.jsonl`, authored fresh from the
corpus and never used to shape the agent — the number that generalizes). The
held-out set includes one refusal case (cash-receipts posting, genuinely
absent from the corpus) to check the grounding contract fires.

### Stage 2 score

Real metered run of 2026-07-19, `claude-sonnet-4-5` at temperature 0 (model,
token usage and cost are stamped into
`evals/results/answers_scoreboard_{dev,heldout}.json`):

| set | answer correctness | citation recall | citation precision | retrieval ceiling |
|---|---|---|---|---|
| **dev (frozen 20)** | **0.99** (19/20 fully covered) | 0.886 | 0.926 | 1.000 |
| **held-out (fresh 9)** | **0.889** (7/9 fully covered) | 0.846 | 0.867 | 0.846 |

Answer correctness is judge-free `must_mention` coverage (macro); citation
recall/precision use the Stage 1 `target_matches` semantics. Read every number
against the retrieval **ceiling** — the share of gold that even reaches the
top-10 context, the hard cap on a single-shot agent. Zero hallucinated
citations across all 29 answers.

Reading it honestly:

- **Held-out 0.889 was a single-run figure — a favorable sample, not a stable
  estimate.** The prompt was written against the dev set, never these 9, so it
  is the more generalizable *kind* of number; but a same-day 3x re-run (for the
  Stage 3 comparison) later showed the single-shot held-out is **variable,
  0.74–0.85** (see the "Stage 3 — the tool-using agent" section) — 0.889 was
  the high end. Its shortfall is retrieval, not
  reasoning: the two imperfect cases are the gold units that never reach the
  top-10 (the 0.846 ceiling), and the agent *refused to fabricate* — it said
  the report code is "not in the retrieved code" instead of inventing a
  diagnosis. The held-out refusal case (cash receipts, genuinely absent from
  the corpus) is answered correctly, grounded in the one comment that
  establishes the absence.
- **Citation recall trails answer correctness by design.** On call-graph
  questions the agent *names* the right callees but *cites* the caller where it
  read the `RUN` statements (call-graph citation-recall 0.667 vs
  answer-correctness 1.000) — an attribution limit a Stage 3 graph-walk was built to close but, on the metered run, did **not** —
  C2's gold was already in the retrieved context and the model declined to cite
  it (see the "Stage 3 — the tool-using agent" section). It is an attribution
  limit, not a wrong answer.
- **A scorer bug was found and fixed in the honest direction.** Two correct
  answers were first under-counted because they wrapped a key term in markdown
  (`**not** in the retrieved code`; the `` `Customer` `` table), which broke the
  literal matcher. The fix normalizes markdown before matching — **the rubric
  patterns are unchanged** — and both saved runs were re-scored offline with no
  new spend (see each scoreboard's `rescore_note`). By the same rule, one dev
  answer (F2) that stated a fact via the constant `{&LIN-BACKORD}` rather than
  the word "backorder" was **left as a miss** (dev 0.99, not 1.0): fixing a
  matcher bug is fair; loosening a content pattern after seeing the model's
  output is not.

Reproduce (offline scoring is free; only generation needs a key):

```
python evals/answer_score.py --dry-run   # ceiling + cost, no LLM
python evals/answer_score.py             # dev; confirms cost before spending
python evals/answer_score.py --cases evals/answers_heldout.jsonl --label heldout
```

Per-case answers, citations, and `must_mention` misses are saved in the two
scoreboard JSONs.

## Stage 3 — the tool-using agent

`openedge_agent/agent3.py` gives the model three tools over the frozen Stage 1
index — `read_unit(unit_id)`, `walk_calls(unit_id, direction)`, `search(query)`
— and an agent loop with a bounded step budget (max 6 tool executions, 9 LLM
calls). It emits the **same** answer/`CITATIONS` contract as Stage 2, so the
frozen scorer runs it unchanged via `--agent tool`. The grounding guarantee is
structural: the citable set is the seed context plus whatever `read_unit` has
read; `walk_calls` and `search` return no unit body and add nothing to it, so
a unit outside that set cannot be cited — `citations ⊆ read_set` by
construction, not by instruction
(fuzz-verified: 1500 adversarial reply streams, 0 violations). The refusal
contract and `parse_response` are Stage 2's, unchanged.

### Stage 3 score — a same-day, model-held-constant comparison

Both agents run on `claude-sonnet-4-5`, temperature 0, same day, against the
frozen dev (20) and held-out (9) sets. Held-out was run **3x each** to show
variance; every scoreboard stamps its `agent`, `max_steps`, per-case `trace`,
and metered `usage`.

Held-out (9 cases = 8 answerable + 1 genuinely-unanswerable refusal), 3 runs each:

| held-out | single-shot (Stage 2) | tool agent (Stage 3) |
|---|---|---|
| answer correctness | 0.815 / 0.852 / 0.741 | **1.000 / 1.000 / 1.000** |
| citation recall | 0.769 / 0.846 / 0.846 | **1.000 / 1.000 / 1.000** |
| citation precision | 0.857 / 0.867 / 0.867 | **1.000 / 1.000 / 1.000** |
| refusals | 4 / 4 / 2 — all on *answerable* cases | **1 / 1 / 1 — only the unanswerable one** |
| invalid citations | 0 | 0 |

Dev (20 cases; retrieval already at ceiling 1.000), 1 run each:

| dev | single-shot | tool agent |
|---|---|---|
| answer correctness | 1.000 | 1.000 |
| citation recall | 0.886 | 0.829 |
| citation precision | 0.907 | 0.915 |
| call-graph cite-recall (C2) | 0.667 | 0.667 |
| invalid citations | 0 | 1 (caught, excluded from `citations`) |

### Reading it honestly

The result is **reliability where retrieval falls short**, and it is one-sided by design.

- **Where retrieval is incomplete (held-out), the tools matter — a lot.** The
  single-shot agent is *unstable* on held-out: correctness swings 0.74–0.85
  across three runs, it spuriously refuses two-to-four of the eight answerable
  questions each run, and it answers the one genuinely-unanswerable question (H9)
  *with* citations instead of refusing. The tool agent is stable and correct —
  1.000 correctness, recall, and precision on all three runs, refusing only H9,
  cleanly, with zero hallucinated citations. Three runs each is what earns the
  word "stable": the single-shot is chronically variable, the tool agent isn't.

- **How it recovered the out-of-retrieval gold — honestly.** Held-out H1/H2's
  gold (`rpt-repsales.p#main`) never enters the top-10 (the 0.846 retrieval
  ceiling). The tool agent cited it anyway — but the trace shows it did so by
  **inferring the unit id from the corpus naming convention and reading it
  directly** (a single `read_unit corpus/rpt/rpt-repsales.p#main`, no `search`),
  then citing what it read (grounded by the read-set invariant). That is a real,
  useful capability the single-shot cannot do — but it leans on the corpus having
  clean, predictable names. `search`, the tool that would recover missed units by
  *content* on a messy real codebase, was available and **not exercised** on
  these cases. So the honest claim is "the agent reads units retrieval never
  surfaced," not "the agent searches to beat retrieval."

- **Where retrieval is already complete (dev), the tools add nothing — and some
  cost.** Every dev gold target is already in the top-10, so there is no gap to
  close. On this run the tool agent's citation recall came in *below* the
  single-shot's (0.829 vs 0.886), it produced one invalid-citation attempt (the
  read-set filter caught it and kept it out of `citations`), and the call-graph
  case the tools were originally aimed at — C2 — **did not move** (0.667). C2's
  three gold callees were already in the seed context; the model named them and
  declined to cite them. That is a citation-*selection* limit, not a
  retrieval-depth one, and no tool addresses it.

**In one sentence:** the tools pay off precisely where single-shot retrieval
falls short — turning an unreliable, over-refusing agent into a stable, correct
one on held-out questions whose evidence isn't in the top-10 — and add nothing
where retrieval is already complete; the originally-targeted dev call-graph case
(C2) is a citation-selection limit the tools do not touch.

### Honesty notes on the Stage 3 score

- **Small n, single model.** Held-out is 9 cases (8 answerable + 1 refusal), and
  the recovery gain concentrates in the two `rpt-repsales.p#main` cases. Read the
  held-out numbers as a *stability* finding on this set, not a benchmark.
- **Controlled but temperature-0-noisy.** Same day, same model, model held
  constant so the delta is the architecture, not a model swap — but temperature 0
  does not make tool choice deterministic, hence three held-out runs. Dev was run
  once (near-deterministic outside the few cases where a tool fires).
- **The result was corrected by its own instrumentation.** An adversarial review
  found the first Stage 3 run saved no traces and had misdiagnosed C2 as a
  "non-walk," and found a refusal-integrity defect (a refusal could emit an
  inline citation). All three were fixed — traces stamped, C2 re-read from the
  data, the refusal path guarded (red-then-green tests) — before these numbers.
  `docs/BUILD_MEMORY.md` carries the trail.

## What Stage 1 contains

| Piece | Where |
|---|---|
| Synthetic ABL corpus: 6 tables + 2 sequences (.df), 10 `.p`, 1 `.cls`, 2 includes | `corpus/` |
| Frozen eval set: 20 questions, 35 gold targets, sub-agent-verified pre-build | `evals/retrieval.jsonl` |
| Ingest: comment-aware ABL chunking (27 units), structured schema, call graph (28 edges, `RUN VALUE` left unresolved on purpose), table-touch analysis | `openedge_agent/abl.py`, `ingest.py` |
| Hybrid retrieval: identifier-aware BM25 + local embeddings + RRF + structure features | `openedge_agent/retrieve.py` |
| Scoreboard: recall@k / strict case recall / precision@5 / MRR, per-category, saved JSON | `openedge_agent/score.py` |
| Tests: 40 stdlib-unittest cases (parsing, ingest integration, retrieval, metrics) | `tests/` |

Stage 2 adds:

| Piece | Where |
|---|---|
| The answer agent: grounded single-call RAG, citation parsing + validation | `openedge_agent/agent.py` |
| Pluggable LLM client (Anthropic / FakeLLM) with metered usage | `openedge_agent/llm.py` |
| Answer eval set: frozen 20 questions + audited `must_mention` rubric | `evals/answers.jsonl` |
| Answer scoreboard: citation P/R + judge-free answer correctness | `evals/answer_score.py` |
| 32 more tests (parsing, grounding, refusal, metric math, rubric guards, markdown-robust matching) | `tests/test_agent.py`, `tests/test_answer_score.py` |

## Running it

Requires Python ≥ 3.10 and numpy (`pip install -r requirements.txt`). No
install step — run from the repo root:

```
python -m openedge_agent.ingest        # corpus -> index/  (~1s)
python -m openedge_agent.score         # eval -> printed report + evals/results/*.json
python -m openedge_agent.score --all   # bm25 / embed / hybrid ablation
python -m openedge_agent.retrieve "what calls ar-invoice.p?"   # ad-hoc query
python -m unittest discover -s tests   # test suite (needs index/ — run the
                                       #   ingest line above once, first)
```

`python -m openedge_agent.score` with no flags reproduces the headline hybrid
row above (deterministic on the same machine/numpy).

Stage 2 (offline parts need nothing extra; real answers need
`pip install anthropic` + `ANTHROPIC_API_KEY` as an env var):

```
python evals/answer_score.py --dry-run          # offline: ceiling + cost estimate
python evals/answer_score.py --fake             # offline: pipeline smoke (FakeLLM)
python evals/answer_score.py                    # real run; confirms cost first
python -m openedge_agent.agent "what calls ar-invoice.p?"          # one question
python -m openedge_agent.agent --fake "what calls ar-invoice.p?"   # offline demo
```

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
docs/                build memory + the Stage 3 design doc
index/               generated artifacts (gitignored; rebuilt by ingest)
HANDOFF.md           Stage 2 + Stage 3 state, verification, open next steps
```

## Roadmap

Stage 1: measured retrieval foundation — done. Stage 2: the
citation-bearing answer agent + answer/citation scoreboard — built,
offline-verified; measured — dev answer-correctness 0.99, held-out 0.889 (a
fresh, sub-agent-verified set — a single-run figure, a favorable sample rather
than a stable estimate; the same-day 3x re-run puts single-shot held-out at
0.74–0.85, per the Stage 2 section above).
Stage 3: tool-using agent — read-unit, walk-calls, search — built and
measured. It delivers reliability where single-shot retrieval is incomplete
(held-out) and nothing where it is already complete (dev); the
originally-targeted call-graph case (C2) is unmoved. See the "Stage 3 — the
tool-using agent" section above and `HANDOFF.md`.
