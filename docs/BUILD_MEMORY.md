# Build memory — one lesson per entry

- **Degrade loudly, and record the substitution inside the measurements.**
  PyPI returned 403 in the build environment, killing sentence-transformers,
  torch, and pytest. The embedder became a pluggable backend (neural if
  importable, numpy LSA otherwise) and the active backend is stamped into
  `index/embed_meta.json` and every scoreboard JSON — so no number can ever
  be quoted without its provenance. Tests moved to stdlib unittest.

- **The eval set caught tokenizer bugs before any user could.** First scored
  run exposed that "cancelled" stemmed to `cancell` (never matching `cancel`)
  and "procedures" to `procedur` (never matching `procedure`). A measured
  ruler turns silent retrieval degradation into a visible, fixable diff.

- **Structure is retrieval signal, not just Stage 2 groundwork.** Text
  ranking alone plateaued at 0.714 recall@5 no matter the parameters; the
  wins came from feeding ingest's *structure* back into ranking — x-ref
  footers, one-step score propagation over the call graph, and table-touch
  query routing — landing at 0.857 (+0.114 over text-only, ablation saved).
  The call-graph questions that remain short (C2) are the ones that need a
  graph *walk*, i.e. the Stage 2 agent.

- **Rebuild derived artifacts whenever the code that derives them changes —
  and let a fresh-context run be the number of record.** The tokenizer was
  improved after the last ingest, so the embedding index on disk was built
  with one tokenizer while queries used another; the drafted README table
  carried P@5/MRR values ~0.01 off. The reproduction audit (clean rebuild,
  docs-only instructions) caught it, plus two documentation miscounts. The
  published numbers are the audit-reproduced values, verified identical
  across two passes.

- **Tuning on the eval set is fine only if you disclose every knob.** All
  tuned parameters (BM25 k1/b, RRF k, weights, name bonus, graph weight,
  touch bonus) are recorded in each scoreboard's `params` and frozen as code
  defaults; the questions and gold answers predate all retrieval code and
  never moved. The one protection that matters: the ruler doesn't bend.

- **Keep the answer key out of the corpus.** Early design had a rich
  `corpus/README.md` mapping calls and table writes; that would let retrieval
  answer eval questions from documentation instead of code (or worse, let the
  README outrank the gold target). The ground-truth map moved to `evals/`,
  which the indexer never ingests, and the corpus README stays semantically
  thin.

- **Gold vs acceptable is what makes flow questions scoreable.** "What happens
  when an order is posted?" has 2 required targets and several
  reasonable-context ones; without an `acceptable` list, precision would
  punish retrieval for being helpfully complete. Split the answer key into
  required (recall) and non-penalized (precision) up front.

- **PROPATH-relative RUN targets need mapping, not joining.** The corpus calls
  `RUN oe/oe-credit.p`; the file lives at `corpus/oe/oe-credit.p`. The call
  graph must resolve run-targets against a PROPATH root rather than treating
  the literal string as a path — decided before ingest is written, so the eval
  IDs already use real repo paths.

- **No OpenEdge compiler exists in this environment.** The ABL is verified by
  careful authorship plus a fresh-context reviewer pass plus Sharon's own read
  — not by compilation. Said plainly wherever the corpus is described; claiming
  compiler-verified would be false.

- **Derived metadata must be computed, not typed.** The `.df` trailer carries
  a byte count of the file; the hand-typed placeholder was wrong (reviewer
  caught it — a Data Dictionary load would reject the file as truncated). Now
  computed programmatically from the actual file size. Anything a machine
  validates, a machine should generate.

- **Cross-file behavior claims need tracing, not reading.** The corpus's 2009
  "retry on lock failure" story was structurally impossible as first written:
  `ar-invoice.p` swallowed the lock failure (returned 0, no ERROR raised) while
  `oe-post.p`'s retry block only fired on ERROR — so the documented retry could
  never happen. A fresh-context reviewer caught it by tracing the pair, and the
  fix (explicit `UNDO inv-blk, RETRY inv-blk` on the 0 return) makes the
  claimed behavior real. This is precisely the class of cross-procedure
  reasoning the Stage 2 agent exists to do.

- **A regex rubric needs the same adversarial audit as an answer key.**
  (Stage 2) The `must_mention` facts for all 20 dev cases were written from
  the corpus, then a fresh-context auditor re-derived every fact — all 20
  cases were factually correct, but 6 had pattern problems the author
  couldn't see: too strict (a correct "stored in Customer.CreditLimit"
  missed S2's patterns; "released back to inventory" missed F4's
  `deallocate`-only pattern) and too loose (bare `status` matched a wrong
  answer, `\+` matched any quoted code). Test rubric patterns against
  phrasings a correct answer and a wrong answer would actually use.

- **Design the judge out of the primary metric.** (Stage 2) Answer
  correctness is any-of regex coverage of `must_mention` facts — no LLM
  grader anywhere in the headline number, so the ChurchReach leaky-judge
  failure mode has nothing to leak through. Per-fact misses are stored in
  the scoreboard so a human can tell a real miss from a phrasing gap.

- **Surface hallucinated citations; don't silently clean them.** (Stage 2)
  The citation parser strips ids that weren't in the retrieved context from
  `citations` but records them in `invalid_citations`, and the scoreboard
  totals them. A grounding failure becomes a visible number instead of a
  quietly repaired output.

- **The held-out set reveals the retrieval ceiling the dev set hid.**
  (Stage 2) On the frozen 20 dev cases every gold target sits in the top-10
  context (gold-in-context 1.000), so any answer/citation miss there is the
  agent's. On the 9 fresh held-out cases it drops to 0.846 — two gold units
  (`rpt-repsales.p#main`) never reach the top-10. The dev set, tuned against,
  flattered retrieval; the held-out set is where the single-shot agent's hard
  ceiling (you can't cite what wasn't retrieved) actually shows up. Report the
  held-out ceiling next to the answer number so a low score reads as
  retrieval-bounded, not agent-failure.

- **Say which verifications actually ran.** (Stage 2) The dev rubric got a
  full independent fresh-context audit; the held-out rubric's independent
  audit was cut off by a credit/API error mid-run, so it was re-verified
  in-context by the main session instead, with the machine structural checks
  (targets resolve, no question-echo, refusal present) still passing. That
  distinction is written into HANDOFF rather than smoothed over — "verified"
  with an asterisk beats "verified" that isn't. (Later resolved: the
  independent held-out audit was redone to completion by a fresh-context
  sub-agent — 8/9 sound, one defect fixed, H2's bare-token matchers.)

- **Citation gold measures "cited the answer files", not "cited the
  evidence".** (Stage 2, C2) For "which programs does order entry run?" the
  frozen gold targets are the three callees; the file containing the RUN
  statements (oe-entry.p) is only `acceptable`. An agent citing exactly the
  evidence scores 0 citation recall on that case. Kept as-is — the frozen
  ruler doesn't bend mid-stage — but it's a known semantic wrinkle to
  reconsider when the Stage 3 eval is authored.

- **Plant traps the ruler can see.** The commented-out Item update in
  `oe-post.p` (W1), the persistent-handle call sites (C1), and the
  shared-buffer include (G2) were designed *into* the corpus at the same time
  as the eval cases that test them. Realistic difficulty you didn't measure is
  just noise; measured traps are signal.

- **When a spec's test table contradicts its contract section, the contract
  wins — and you test both directions.** (Stage 3) The design's T7 row listed
  "bare file path, wrong case, missing corpus/ prefix" under bad args
  asserting `ok is False`, but §3's tool contract says exactly those forms
  RESOLVE (bare file → `#main`; C2's one-call walk depends on it). Built to
  §3; the test asserts the genuinely-bad variants fail and the resolvable
  variants succeed, so the contradiction is pinned down instead of silently
  picked through.

- **Implement the arithmetic, not the adjective.** (Stage 3) The design says
  a NUDGE is "charged as a step", but its own budget line — ≤ 9 LLM calls =
  6 tool executions + 2 nudges + 1 forced final — only adds up if nudges do
  NOT consume the tool budget. The arithmetic is the testable statement
  (T3 asserts `max_steps + 1` calls, T6 asserts the nudge counts), so the
  loop charges nudges as LLM calls with their own hard cap of 2, never as
  tool steps. Deviation disclosed here rather than smoothed over.

- **The frozen parser's id-form strictness carries into Stage 3 — on
  purpose.** A model that reads `oe-credit.p#main` via read_unit but cites
  the bare file form `corpus/oe/oe-credit.p` gets an invalid citation,
  because `parse_response` (unchanged, per design §2c) resolves only
  exact/case/prefix against the read-set ids — identical to Stage 2's
  behavior against `retrieved`. Left unfixed: widening the allowed list with
  file aliases would change the frozen funnel's semantics mid-design, and
  the tool result echoes the resolved `#main` id (the citable form), which
  models copy. If it ever bites, it lands in `invalid_citations` — visible,
  not silent. (Independent verifier finding, 2026-07-25.)

- **An offline rule-check can verify a rule's direction — it cannot
  predict a citation outcome.** (Stage 3; corrected 2026-07-26) An earlier
  version of this entry claimed the fresh-context verifier "confirmed rule
  4c closes C2's 3 gold callees." The real metered run refuted that: C2's
  gold callees were already in the seed context and the model still
  declined to cite them. What the verifier actually established stands —
  rule 4c steers no caller-gold case (C1/C3/C4/C5/H3) toward callee-only
  citations, the C3/C5 unit-granularity risk is real, the §3 invariant
  holds (all 24 walk-returned ids readable), and walk/ingest normalization
  parity is exact (0 mismatches over 27 units × 3 directions). The sharper
  lesson: those are checks of rules and invariants; "the model will cite
  X" is a behavioral claim, and only the real run gets to make it.

- **A refusal that forgets its CITATIONS line was one fallback away from
  citing.** (Stage 3, adversarial review 2026-07-26) The forced-final path
  fed replies straight to parse_response, whose inline-[id] fallback turns
  a refusal's "the closest unit was [id]" mention into a citation — H9
  stayed clean only by luck. Same review: _classify treated any line-start
  TOOL: mention as a tool call, so a complete final answer quoting one was
  discarded and re-dispatched, burning budget. Both fixed in agent3's loop
  with red→green tests (a refusal with no CITATIONS: line parses as
  CITATIONS: none; a TOOL: line only counts when it leads the reply or no
  CITATIONS: line exists); parse_response itself stays frozen. The loop's
  refusal gate now also normalizes markdown first, so it detects exactly
  the refusals the frozen scorer counts.

- **The transcript path is injection-safe by serialization; the seed path
  only by corpus hygiene.** (Stage 3) Tool results enter the transcript
  via json.dumps, so a read unit whose source text contains line-start
  TOOL:/CITATIONS: lines cannot surface those tokens at line start in the
  next prompt (pinned by test). The SEED context path is NOT sanitized —
  the frozen build_context pastes unit text raw into the prompt — and is
  safe only because the corpus is clean and synthetic; any future
  non-synthetic corpus must revisit this before ingest.

- **Fix a grader bug; don't game a grader pattern.** (Stage 2, post-run)
  Hand-checking the real answers found two correct ones under-counted because
  markdown broke the literal `must_mention` match (`**not** in the retrieved
  code`; a backtick-wrapped `Customer` table). That is a scorer bug — normalize
  markdown before matching, patterns unchanged, re-score the saved runs offline
  (no new spend). But one answer (F2) that expressed a fact via the constant
  `{&LIN-BACKORD}` instead of the word "backorder" was LEFT as a miss: fixing a
  matcher bug is fair; loosening a content pattern after seeing the model's
  output is tuning-to-output. The direction of the change is the tell — a
  bug-fix can raise a score honestly, a post-hoc pattern-loosening cannot.

- **Tools help where retrieval is short, not where it's saturated — and the
  held-out gain is reliability, not search.** (Stage 3, metered 2026-07-26)
  Same-day, model-held-constant, held-out run 3x: the single-shot agent is
  unstable on held-out (correctness 0.74–0.85, 2–4 spurious refusals of 8
  answerable cases per run, and it answers the one unanswerable case H9 *with*
  citations); the tool agent is stable — 1.000 correctness/recall/precision every
  run, refuses only H9 cleanly, 0 hallucinated cites. On dev, retrieval is
  already at ceiling, so the tools add nothing: recall 0.829 vs 0.886, one caught
  invalid, C2 unmoved (0.667). The out-of-retrieval recovery on H1/H2 is real but
  modest and naming-dependent: the trace shows the agent **guessed
  `rpt-repsales.p#main`'s id from the naming convention and read it directly — no
  `search`** — so the generalizing tool was not exercised. Honest headline: the
  tools convert an unreliable, over-refusing agent into a reliable one where
  retrieval falls short, and are net-neutral-to-negative where it doesn't; C2
  stays a citation-selection limit, reported unchanged, not tuned.
