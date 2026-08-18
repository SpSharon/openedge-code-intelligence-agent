# HANDOFF — Stages 2 & 3 complete (built, independently verified, measured) (Stage 2: 2026-07-19; Stage 3: 2026-07-26)

## State

Stage 1 (retrieval + frozen eval + scoreboard) is done, committed, and
unchanged: micro R@5 0.857 / R@10 1.000 over the frozen 20 cases. It is the
regression harness — `python -m openedge_agent.score` still prints exactly
that (re-checked after all Stage 2 work).

Stage 2 is built, independently verified, and **measured on a real metered
run** (2026-07-19, `claude-sonnet-4-5`, temp 0, ~$0.51 total): dev
answer-correctness **0.99** / citation recall 0.886 / precision 0.926;
held-out **0.889** / 0.846 / 0.867; 0 hallucinated citations across 29
answers. The full honest read is in the README "Stage 2 score" section. What
exists:

**Increment A — the committable core (already delivered + committed to disk):**
- `openedge_agent/agent.py` — `Agent.ask(q) -> {answer, citations,
  invalid_citations, retrieved, raw_reply}`. Single-shot RAG: top-10 via the
  frozen Stage 1 `Retriever`, grounded context block (unit text + x-refs,
  each labelled with its unit id), ONE LLM call, citations parsed from a
  final `CITATIONS:` line (inline `[id]` fallback), validated against the
  retrieved set — unretrieved cites go to `invalid_citations`, never into
  `citations`. Refusal contract: "not in the retrieved code".
- `openedge_agent/llm.py` — pluggable client (ChurchReach pattern):
  `AnthropicLLM` (key from env only; model via `OE_AGENT_MODEL`, default
  claude-sonnet-4-5), scripted `FakeLLM`, shared `Meter` stamped into every
  scoreboard.
- `evals/answers.jsonl` — the frozen 20 questions; gold/acceptable identical
  to `retrieval.jsonl` (test-enforced); per-case `must_mention` any-of regex
  facts. Fact-audited by a fresh-context sub-agent (all true; 6 pattern
  fixes applied — see BUILD_MEMORY).
- `evals/answer_score.py` — citation P/R via `target_matches`, answer
  correctness = macro must_mention coverage (JUDGE-FREE; no LLM grader).
  `--dry-run` (offline ceiling + cost), `--fake` (offline plumbing), real
  mode confirms cost before spending and stamps model + meter into
  `evals/results/answers_scoreboard_<label>.json`.

**Increment B — the held-out set:**
- `evals/answers_heldout.jsonl` — 9 fresh questions (H1–H9), authored by a
  fresh-context sub-agent from the corpus only (not from the prompt or dev
  set), spanning schema / call-graph / flow / legacy-pattern / table-touch
  plus one refusal case (cash receipts — genuinely absent from the corpus).
  Same schema as `answers.jsonl`. Verified against the corpus (see caveat
  below).
- Machine checks (in the test suite): every held-out target resolves against
  the index, no pattern echoes its own question, exactly one refusal case,
  no question overlaps the dev set.

**Tests:** 72 green offline (`python -m unittest discover -s tests`); 32 new
for Stage 2 (parsing, grounding, refusal, hallucinated-cite filtering,
metric math, dev + held-out rubric guards, markdown-robust matching).

## Verification status (read this honestly)

- Dev rubric (`answers.jsonl`): fresh-context sub-agent audited every fact vs
  corpus — all 20 factually correct; 6 pattern fixes applied pre-run.
- Held-out rubric (`answers_heldout.jsonl`): the independent fresh-context
  audit that a credit/API error cut off mid-run was **redone and completed** —
  a fresh-context sub-agent re-derived all 9 answers from the corpus. Verdict:
  8/9 sound, refusal genuinely unanswerable, one real defect — H2's fact-2/3
  matchers accepted the bare tokens `InvoiceDate`/`ArHist`, which a
  non-diagnostic answer also contains. **Fixed**: tightened to the index-defeat
  idiom + scan verbs (a correct answer still scores 3/3; a wrong/non-diagnostic
  one drops to 1/3 or 0/3), disclosed in the case notes.
- Real run, hand-checked: reading the saved answers caught a **scorer bug** —
  two correct answers (H9's `**not**`, S2's backtick-wrapped `Customer`) were
  under-counted because markdown broke the literal `must_mention` match. Fixed
  with markdown-normalization (rubric patterns UNCHANGED) + a regression test;
  both saved runs re-scored offline (no new spend; scoreboards carry a
  `rescore_note`). One dev under-count (F2, `{&LIN-BACKORD}` vs "backorder")
  left as-is on purpose — loosening a content pattern after seeing output is
  the gaming direction.
- Retrieval ceilings (real): dev gold-in-context 35/35 = 1.000; held-out
  11/13 = 0.846 (the two misses are `rpt-repsales.p#main` on H1/H2 — a genuine
  retrieval limit, not a rubric error). Ran on Windows; ABL compile still
  unverified (no OpenEdge runtime).

## Stage 2 is closed — reproduce

Real run + hand-check + README "Stage 2 score" section: done. To reproduce
(offline scoring is free; only generation needs a key):

```
$env:ANTHROPIC_API_KEY = "..."       # session only, never a file
pip install anthropic
python evals/answer_score.py                                  # dev 20, ~$0.34, asks y/N
python evals/answer_score.py --cases evals/answers_heldout.jsonl --label heldout   # ~$0.16
```

The committed scoreboards were re-scored offline from their stored answers
after the markdown-robustness fix (patterns unchanged; see each file's
`rescore_note`).

## Stage 3 — built and measured (2026-07-26)

Tool-using agent (`openedge_agent/agent3.py`): tools `read_unit`, `walk_calls`,
`search` over the Stage 1 index, an agent loop with a bounded step budget, the
same answer/citation contract as Stage 2, scored via `--agent tool` with the
frozen scorer unchanged. Designed and adversarially reviewed by Opus 5, built by
Fable, verified by a fresh-context subagent (invariant `citations ⊆ read_set`,
26/26) and 98/98 tests.

**Result** (same-day, model-held-constant, `claude-sonnet-4-5`, temp 0; held-out
3x): the tools deliver *reliability* where retrieval is incomplete and nothing
where it isn't. Held-out — single-shot unstable (correctness 0.74–0.85, 2–4
spurious refusals/run, mis-answers the refusal case); the tool agent is stable
(1.000 correctness/recall/precision all 3 runs, refuses only H9, 0 hallucinated
cites). Dev — no gain (recall 0.829 vs 0.886), C2 unmoved (0.667; its gold was
already in context — a citation-selection limit no tool touches). Out-of-retrieval
recovery (H1/H2) came via id-inference-and-read, **not `search`**. Full read in
the README "Stage 3" section; trail in `BUILD_MEMORY`.

**Open next steps (optional):** force/exercise `search` on the retrieval-gap
cases to test content-based recovery independent of naming; a held-out split
larger than 9; a real neural encoder (the embed leg is LSA today).

## Rules that carry over

Synthetic corpus only. Honest framing. Frozen sets never move. Nothing
pushed — Sharon reviews and commits from PowerShell.
