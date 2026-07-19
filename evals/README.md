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
