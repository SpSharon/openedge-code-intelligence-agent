# Eval sets

## Stage 2: answer eval set (`answers.jsonl`)

The same frozen 20 questions with the same gold/acceptable targets
(test-enforced identical to `retrieval.jsonl`), plus per-case
`must_mention`: the facts a correct answer has to state, each as a list of
alternative case-insensitive regexes (covered if ANY matches). Scored by
`answer_score.py`: citation recall/precision reuse `target_matches` below;
answer correctness = macro-averaged fact coverage, with no LLM judge in the
loop. Both agents are scored the same way: `--agent {single,tool}` selects the
Stage 2 single-shot RAG (default) or the Stage 3 tool-using agent, against this
same frozen rubric — the scoring logic is untouched. Facts were audited by a fresh-context AI reviewer against the corpus
(all true; six patterns tightened/loosened after the audit). These 20 are a
DEV set — the prompt was written against them; `answers_heldout.jsonl`
carries the held-out questions that measure generalization.

### Held-out answer set (`answers_heldout.jsonl`)

9 fresh questions (H1–H9) authored from the corpus only — not derived from
the dev set or the answer prompt — spanning schema, call-graph, flow,
legacy-pattern, and table-touch, plus one `refusal` case whose answer is
genuinely absent from the corpus (cash-receipts posting, which the code
comments explicitly place in the GL package). Same case schema as
`answers.jsonl`. This is the honest generalization number; the dev 20 are
tuned-against. Retrieval ceiling on this set is 0.846 (11/13 gold targets
reach the top-10), vs 1.000 on the dev set — reported so a lower answer
score reads as retrieval-bounded, not agent failure.

### Re-deriving a saved scoreboard offline (`--rescore`)

Every `evals/results/answers_scoreboard_*.json` archives, per case, the
model's answer, citations, and retrieved unit ids. `--rescore` re-runs the
same scoring functions (`score_citations`, `score_must_mention`,
`gold_in_context`, `aggregate`) over those archived rows and compares the
recomputed metrics with the ones stored in the file, exiting non-zero on
any difference:

    python evals/answer_score.py --rescore evals/results/answers_scoreboard_stage3_heldout_r1.json

No LLM call, no API key, no network, no index; it writes and modifies
nothing. What it proves: the published metrics follow from the archived
answers under the current scorer — the scoring is checkable by anyone.
What it does not prove: the answers themselves; regenerating those still
needs `ANTHROPIC_API_KEY` and a real run.

# Retrieval eval set

`retrieval.jsonl` is the ruler for Stage 1: 20 natural-language questions about
the synthetic corpus, each with known-correct retrieval targets. It was written
**before any retrieval code existed** and is frozen against tuning — retrieval
gets tuned *against* it, the questions never get tuned *toward* retrieval. It
lives outside `corpus/` so the indexer can never ingest the answer key.

Each case was verified by a fresh-context AI reviewer that read the corpus
independently and re-derived every answer before retrieval was built.

## Case format

```json
{"id": "C1", "category": "call-graph", "question": "...",
 "gold": ["corpus/oe/oe-entry.p#add-order-line"],
 "acceptable": ["corpus/inv/inv-alloc.p#allocate-item"],
 "notes": "why these targets, and any trap the case sets"}
```

- **gold** — targets a correct retrieval must return. Recall is measured
  against these.
- **acceptable** — targets that are reasonable context for the question;
  returning them is not required and is **not penalized** in precision.
  Anything in neither list counts against precision.

## Unit-ID convention

| Form | Meaning | Example |
|---|---|---|
| `path#name` | one internal procedure / function / method chunk | `corpus/oe/oe-credit.p#check-credit` |
| `path#main` | a compile unit's main block | `corpus/oe/oe-post.p#main` |
| `path` (no `#`) | file-level target: *any* chunk of the file counts | `corpus/oe/oe-ship.p` |
| `schema:Name` | a table or sequence from `db/ordermgmt.df` | `schema:OrderLine`, `schema:next-ord-num` |

Field-level questions target the owning table's schema object. The schema
namespace is flat (tables and sequences share it) — fine here because no names
collide; a real system would need a `schema:table:` / `schema:seq:` split.

## Scoring intent (implemented after the checkpoint)

Headline metric: **micro recall@5** — over all gold targets in all cases, the
fraction that appear in the retriever's top 5. Also reported: recall@1 and
recall@10, strict case-level recall@5 (every gold target of a case in the top
5), precision@5 (share of top-5 results that are gold or acceptable), and MRR
of the first gold hit. A file-level gold target is satisfied by any chunk of
that file; a `#unit` target only by that unit; a `schema:` target only by that
schema object.

## Deliberately hard cases (known traps, kept on purpose)

- **W1** — `oe-post.p` contains Item-update code *only inside a comment*
  (pre-2007 relief logic). Retrieval that returns it for "which procedures
  update Item" is wrong, and keyword methods will be tempted.
- **G2** — `sb-cust` is *defined* in `include/oeshared.i` but *used* in three
  `.p` files; include-blind chunking sees the usages, not the definition.
- **C1 / F4** — allocation calls go through a `GLOBAL SHARED` handle to a
  persistent procedure (`RUN allocate-item IN gh-alloc`), so the callee's
  filename never appears at most call sites.
- **Call-graph limits stated up front**: `oe-post.p`'s `RUN VALUE(c-exit-proc)`
  is dynamic and its target (`oe/oe-exit.p`) deliberately does not exist in the
  corpus; no static extractor can resolve it. The eval set never requires
  resolving it.
