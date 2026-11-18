# v0.14.1 — review follow-ups: dead code removal and buyer-review navigation

Patch release 0.14.1 (healthcheck package; `@verovo/mcp` SDK versioning
unchanged). buyer-ledger is 237 tests, healthcheck is 594 tests, all reference
vectors pass.

## What is in it

- **Root test-loader dedup (V-4, scoped)**: the duplicated root test loaders
  collapse into one `tests/_root_support.py`. The proposed deletion of
  `lab/stats.py`/`lab/prereg.py` was reverted on review: `lab/analysis.py`
  imports both, so they are live code, not dead code.
- **Buyer-review example navigation**: the published
  `examples/buyer-review.html` gains outbound navigation so readers can move
  on from the demo to the next step.

## Notes

- No behaviour change; this patch only removes dead code and improves the
  example page. The known minors noted in v0.14.0 (partial `book` products on
  failure, no candidate-store cross-process lock) are still queued.
