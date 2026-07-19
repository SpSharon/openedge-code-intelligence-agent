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

- **Plant traps the ruler can see.** The commented-out Item update in
  `oe-post.p` (W1), the persistent-handle call sites (C1), and the
  shared-buffer include (G2) were designed *into* the corpus at the same time
  as the eval cases that test them. Realistic difficulty you didn't measure is
  just noise; measured traps are signal.
