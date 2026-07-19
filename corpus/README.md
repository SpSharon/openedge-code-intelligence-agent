# Synthetic ABL Corpus — `ordermgmt`

**This is a synthetic Progress 4GL / OpenEdge (ABL) codebase, generated in 2026
to exercise a code-intelligence tool. It is not, and does not derive from, any
real system, employer codebase, schema, or business logic.** File headers carry
fictional authors and dates (1996–2019) to simulate the accretion of a real
legacy application.

Domain: order management for a small distributor. The directory root doubles as
the PROPATH root — `RUN oe/oe-credit.p` and `{include/oeshared.i}` resolve
relative to `corpus/`.

## Inventory

| File | What it is |
|---|---|
| `db/ordermgmt.df` | Data Dictionary dump: 6 tables, 2 sequences |
| `include/oeshared.i` | Shared order-entry variables and shared Customer buffer |
| `include/ordstat.i` | Order / order-line status preprocessor constants |
| `oe/oe-entry.p` | Order entry (main entry point) |
| `oe/oe-credit.p` | Credit check |
| `oe/oe-price.p` | Pricing and discounts |
| `oe/oe-ship.p` | Ship confirmation |
| `oe/oe-post.p` | Order posting batch |
| `oe/oe-cancel.p` | Order cancellation |
| `inv/inv-alloc.p` | Inventory allocation manager (persistent procedure) |
| `ar/ar-invoice.p` | Invoice creation (AR) |
| `rpt/rpt-backord.p` | Backorder report |
| `rpt/rpt-repsales.p` | Sales-rep monthly sales vs quota report |
| `service/OrderService.cls` | OO facade added for a 2019 integration project |

`oe-ship.p`, `oe-post.p`, and the two reports are roots — nothing in the
corpus calls them (menus in the notional full system do), which is normal for
a batch/menu-driven legacy app. `oe-entry.p` and `oe-cancel.p` are reached
through `service/OrderService.cls` as well as from the notional menus.

The corpus deliberately includes legacy patterns that make static analysis and
retrieval hard: a shared-buffer include, a deeply nested transaction block that
spans an external `RUN`, a `PERSISTENT` procedure reached through a
`GLOBAL SHARED` handle, a dynamic `RUN VALUE()` call, and code that survives
only in comments. See `../evals/README.md` for how these are used as test
cases.
