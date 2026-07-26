# Stage 3 design — the tool-using graph-walk agent

> **Outcome note (2026-07-26):** kept as the design-time spec. Stage 3 was built and measured, and the result **differed from this design** — §0 predicted that reading the three callee `#main` units would close C2, but on the metered run C2 did **not** move (its gold was already in the retrieved context and the model declined to cite it). The real result — a reliability gain on held-out, neutral on dev, recovery via id-inference-and-read rather than `search` — is in the README "Stage 3 — the tool-using agent" section and `docs/BUILD_MEMORY.md`.

**Status: design only, nothing built** (as originally written). Spec for a builder. Written against
`agent.py`, `retrieve.py`, `ingest.py`, `llm.py`, `evals/answer_score.py`,
README "Stage 2 score", HANDOFF "Stage 3 — first step".

## 0. What is actually broken, measured

Dev call-graph citation recall 0.667 = **6 of 9 gold targets over 5 cases**,
and the entire gap is **one case, C2** ("Which other programs does order entry
run?"). The agent named `oe-credit.p`, `oe-price.p`, `inv-alloc.p` correctly
(answer-correctness 1.000) but cited `oe-entry.p#main` and
`#add-order-line` — the caller units where the `RUN` statements sit. C1, C3,
C4 and held-out H3 are already 1.000.

Two consequences the build must respect:

- The mechanical fix is small: read the three callee `#main` units and cite
  them. `target_matches` treats file-level gold `corpus/oe/oe-credit.p` as hit
  by `corpus/oe/oe-credit.p#main`.
- **The headline is small-n.** 0.667 → 1.000 is one case. Report it as
  "5 cases / 9 gold targets, one case carries the whole gap", never as a
  category average alone. The second, more generalizable win to look for is
  breaking the held-out retrieval ceiling (0.846 — H1/H2 gold
  `rpt-repsales.p#main` never reaches top-10) via `search`.

And the trap: on C1/C3/C4/H3 the **caller** is gold and the callee is only
`acceptable`. A rule like "cite the callee" would fix C2 and break four cases.
The discipline in §2 is therefore about *where the evidence lives*, never
about call direction.

## 1. The agent loop

New module `openedge_agent/agent3.py`, class `ToolAgent`. `agent.py` stays
frozen as the baseline. `ToolAgent` reuses `Retriever`, `build_context`,
`parse_response`, `REFUSAL_PHRASE` and `llm.py` unchanged, and exposes the
same public surface as `Agent` (`.retriever`, `.k`, `.build_prompt`, `.ask`)
so `evals/answer_score.py` runs as a drop-in.

`llm.complete(system, user)` is stateless and stays that way: **each step is
one `complete()` call whose `user` block is question + seed context +
the transcript of prior tool calls and results.** No message-history API, no
edit to `llm.py`.

| State | Does | Next |
|---|---|---|
| `SEED` | `Retriever.search(q, k=10)` → identical Stage 2 context block. Seed ids enter the read set (their text is in the prompt). | `DECIDE` |
| `DECIDE` | one `complete()`. First line matching `^\s*TOOL:` wins → `ACT`; else a `CITATIONS:` line → `FINAL`; else → `NUDGE`. | |
| `ACT` | validate + run the tool, append a `TOOL RESULT (step n)` block, `steps += 1`. | `DECIDE` |
| `NUDGE` | append a format reminder. Charged as a step. Two consecutive nudges, or two consecutive tool-arg errors → `FORCE`. | `DECIDE` / `FORCE` |
| `FORCE` | one final `complete()` with the tool menu removed: "answer now from what you have read, or refuse." | `FINAL` |
| `FINAL` | `parse_response(text, allowed_ids=list(read_set))`. | return |

**Step budget: `max_steps = 6` tool executions**, ≤ 1 forced-final call,
≤ 2 nudges ⇒ ≤ 9 LLM calls per question, and exactly `max_steps + 1` calls in
the pathological never-stop case. Six is derived from the graph, not guessed:
C2's chain is `walk_calls(oe-entry.p, out)` → read `oe-credit.p#main` → read
`oe-price.p#main` → read `inv-alloc.p#main` = 4, leaving headroom for one
`search` and one second-hop walk. The corpus is 27 chunks / ~30k chars; depth
beyond 6 is thrashing, not research.

**Stopping criteria**, stated in the prompt as rules the model self-applies:

- *Enough to answer*: every claim is supported by text you have read, every
  unit you intend to cite is in your read set, and the last tool call added no
  new evidence.
- *Keep walking*: your answer would **name** a program, procedure or table
  whose text you have not read (the C2 trigger — "you can name it but you
  can't cite it, so go read it"), or the question is "who calls X / what does
  X run" and you have seen only the `RUN` statement, not the target unit.
- *Refuse*: after **at least one `search` probe with different vocabulary**,
  nothing you have read supports the claim → answer with the unchanged phrase
  `not in the retrieved code` and `CITATIONS: none`. The ≥1-probe rule is
  enforced as a single soft nudge (never a loop), and turns a refusal from
  "not in the top-10" into "not in the corpus."

Tool-call grammar — one call per step, whitespace-delimited, first `TOOL:`
line only (anything after it is ignored, which blocks two-tools-and-a-half-
answer replies):

```
TOOL: read_unit corpus/oe/oe-credit.p#main
TOOL: walk_calls corpus/oe/oe-entry.p out
TOOL: search persistent procedure handle allocation
```

JSON args were considered and rejected: quoting breaks on free-text `search`
queries far more often than whitespace splitting breaks on ids.

## 2. Citation discipline — the mechanism

Three layers, only the first of which is a prompt.

**(a) Prompt.** Stage 2 `SYSTEM_PROMPT` rules 1–5 are carried **verbatim** (so
the output contract, refusal phrase and CITATIONS format are provably
unchanged), plus:

- *4b — read-set rule.* "Cite only units whose full text you have seen, in the
  retrieved context or via `read_unit`. A unit that a tool merely *listed* —
  a `walk_calls` edge, a `search` hit — is not readable evidence."
- *4c — name-then-read.* "If your answer names a program or procedure **as an
  answer to the question** (not as passing context), read that unit and cite
  it. The unit holding the `RUN` statement is evidence that the call exists;
  the unit you name is evidence of what it is. Cite the ones your claim is
  about." ← this is the sentence that closes C2 without flipping C1/C3/C4/H3.
- *4d — read ≠ cite.* "A unit you read and did not use must not be cited.
  Typical answers cite 1–4 units."

**(b) State.** Two disjoint structures, and the distinction is the whole
design:

| Structure | Filled by | Citable |
|---|---|---|
| `read_set: OrderedDict[unit_id → {source: seed\|read_unit, step, chars}]` | seed context; `read_unit` | **yes** |
| `seen_ids: set` | `walk_calls` edges, `search` hits | no |

`walk_calls` and `search` deliberately return **no body text**. Walking to a
unit cannot authorize citing it; you must spend a `read_unit` step. That is
what makes "cited = actually read" true by construction rather than by
instruction.

**(c) Validation.** `FINAL` calls the existing `parse_response(text,
allowed_ids=list(read_set))` — same function, no edit, just a wider allowed
list than Stage 2's `retrieved`. Anything outside the read set lands in
`invalid_citations`, never in `citations`. Unchanged semantics, unchanged
visibility of hallucinated cites.

**Return contract** (`ask`) — Stage 2 keys byte-identical so the frozen scorer
consumes it untouched; `retrieved` **stays the seed top-10** so
`gold_in_context_rate` remains the Stage-2-comparable ceiling:

```
{answer, citations, invalid_citations, retrieved, raw_reply,   # Stage 2, unchanged
 read, steps, trace}                                            # new; scorer ignores extra keys
```

## 3. Tool contracts

Shared: every tool returns a dict with `ok`. On failure
`{ok: False, error, did_you_mean: [≤5 ids]}` — appended to the transcript,
charged a step, no mutation of `read_set`. Id resolution reuses Stage 2's
rules (exact → case-insensitive → restore missing `corpus/` prefix); a bare
file path resolves to `<file>#main` when that unit exists, else errors with
the file's unit ids listed.

**`read_unit(unit_id: str) -> dict`**
Source: `Retriever.units` (chunks + schema units), i.e. the same text Stage 2
puts in context.
Returns `{ok, unit_id, kind, name, file, text, xref, chars}`.
Side effect: adds to `read_set`. Re-reading a unit returns
`{ok, unit_id, note: "already in your context, step n"}` without re-pasting
the text — costs a step, saves tokens, discourages loops.

**`walk_calls(unit_id: str, direction: str) -> dict`**
`direction ∈ {"out", "in", "both"}`; anything else errors with the allowed
values. `unit_id` accepts a unit id **or a bare file path**, in which case the
result is the union over that file's units (this is what lets C2 resolve in
one call). Source: `index/callgraph.json`.
Normalization must mirror `ingest.py` exactly: `run_external` targets become
`<resolved>#main` when that unit exists; `run_internal` / `function_call` /
`run_in_handle` are already unit ids; `include` edges are reported separately
from calls.
Returns
`{ok, unit_id, direction, calls_out: [{unit_id, type, target_raw, from_unit, persistent?, handle?}], called_by: [...], includes: [...], included_by: [...], unresolved: [{from_unit, type, target_raw, reason}], note}`.
**Invariant (testable): every `unit_id` returned is accepted by `read_unit`.**
`unresolved` always surfaces the `RUN VALUE` edge in `oe-post.p#main` with
reason "target not statically known", so the model can say a list may be
incomplete instead of implying completeness. No text; no `read_set` entry.

**`search(query: str) -> dict`**
Runs the same frozen `Retriever` instance, `k=5` fixed (the seed's `k=10`
stays the only tunable). Query must be non-empty after strip, ≤ 200 chars.
Returns `{ok, query, hits: [{unit_id, kind, name, file, header, xref, score, already_read}]}`
— headers and x-refs only, **no body text**, no `read_set` entry. To cite a
hit you must read it.

## 4. Offline test surface (FakeLLM, zero API spend)

`FakeLLM(list_of_strings)` already returns one scripted reply per call — a
loop test is just a list. No new test infrastructure.

| # | Scripts | Asserts |
|---|---|---|
| T1 | The C2 chain: walk `oe-entry.p out` → read the 3 callee `#main`s → final citing them | 5 calls, `steps == 4`, read set holds the 3 callees, `invalid == []`, and — the money assertion — `score_citations(C2_case, citations)["n_gold_cited"] == 3`, importing the **frozen** scorer offline. This is the proof the design closes the gap, before any spend. |
| T2 | one walk, then finalize | the walk result text appears in `FakeLLM.prompts[i+1][1]`; separately, an index-wide invariant loop: every `unit_id` from `walk_calls(u, "both")` over all 35 units is accepted by `read_unit` |
| T3 | reply is always `TOOL: read_unit …` | exactly `max_steps` tool executions; a forced-final call whose prompt omits the tool menu; `llm.meter.calls == max_steps + 1`; output still has the contract shape |
| T4 | walk to a unit, never read it, then cite it — **on a question whose seed top-10 provably excludes that unit** (assert the precondition, else the test is vacuous) | `citations == []`, `invalid_citations == [that unit]` |
| T5 | `search` probe → refusal reply | `citations == []`, refusal phrase present, `invalid == []`; plus: refusal attempted with zero probes fires exactly one nudge and then completes |
| T6 | reply with neither `TOOL:` nor `CITATIONS:` | one nudge then completion; two consecutive → `FORCE`, contract intact |
| T7 | table-driven bad args: unknown id, bad direction, empty query, bare file path, wrong case, missing `corpus/` prefix | each `ok is False`, has `error`, leaves `read_set` unchanged |
| T8 | `python evals/answer_score.py --fake --agent tool` | drop-in works against the frozen scorer end-to-end; separately `python -m openedge_agent.score` still prints R@5 0.857 |

The only edit to `evals/answer_score.py` is two lines in `main()` — an
`--agent {single,tool}` argparse entry and the class it constructs. Every
scoring function, rubric and metric is untouched; run with
`--label stage3_dev` / `stage3_heldout` so the Stage 2 scoreboards are not
overwritten. Note in the run log that `estimate_cost` now reports a **lower
bound** (it prices the seed prompt = a 1-step run); the honest pre-spend
figure is roughly that × steps — expect ~$1.5–2 dev and ~$0.8 held-out at
`claude-sonnet-4-5` versus Stage 2's $0.34/$0.16, with the `Meter` recording
the truth afterwards.

## 5. Risks

**R1 — precision regression by over-citation.** Reading more units creates
more chances to cite an unused one; Stage 2 already lost 4 cites this way
(S4, C5, G3, G4 → dev precision 0.926). The read-set filter does *not* help
here — it only blocks unread units. Defenses: rule 4d, the forced-final
prompt re-listing the read set under "read ≠ cite", and a reporting rule —
**never publish recall without precision beside it.** Ship gate: precision
≥ 0.906 (Stage 2 − 0.02) and answer-correctness ≥ Stage 2 on both sets.

**R2 — direction bias.** A "prefer the callee" heuristic fixes C2 and breaks
C1/C3/C4/H3, where the caller is gold. Prevention: the rule is
evidence-location (4c), never direction; and the report must carry a per-case
C1–C5 + H3 table, since a category average can hide a 3-gain/3-loss wash.

**R3 — an unread unit entering citations.** Structurally prevented: `citations
⊆ read_set`, `walk_calls`/`search` return no text and no read-set entry, and
`parse_response` is the same validated funnel as Stage 2. T4 is the guard.

**R4 — refusal erosion.** With `search` available the model may hunt until it
attaches a weak citation rather than refuse; H9 already emitted 2 bad cites on
a refusal case. Defenses: budget exhaustion forces "answer or refuse from what
you read", the refusal phrase is carried verbatim, and the `refusals` count in
both scoreboards is a watched regression metric.

**R5 — overselling the headline.** One case (C2) is the whole dev gap, and
held-out call-graph is a single case already at 1.000, so there is *no*
held-out headroom for the headline metric. Report the delta with its n, and
promote the second measurement — held-out citation recall above the 0.846
retrieval ceiling, which `search` can genuinely break — as the
generalization evidence.

**R6 — non-determinism and cost.** Temperature 0 does not make tool choice
stable; step counts and cost vary per run and re-runs will not be
bit-identical. Stamp `steps`, the full `trace`, and the `Meter` into every
scoreboard row.

**R7 — collateral regression on the other 24 cases.** Mitigated by holding the
seed context and rules 1–5 identical, so tools are strictly additive. If dev
answer-correctness drops below 0.99, the tool prompt sections are the first
suspect, not the loop.
