# v0.14.1 — review follow-ups: dead code removal and buyer-review navigation

Patch release 0.14.1 (healthcheck package; `@verovo/mcp` SDK versioning
unchanged). buyer-ledger is 237 tests, healthcheck is 594 tests, all reference
vectors pass.

## What is in it

- **V-4 dead-code removal**: the vendored `healthcheck/verovo_healthcheck/lab/`
  modules that nothing imported are deleted, and the duplicated root test
  loaders collapse into one `tests/_root_support.py`.
- **Buyer-review example navigation**: the published
  `examples/buyer-review.html` gains outbound navigation so readers can move
  on from the demo to the next step.

## Notes

- No behaviour change; this patch only removes dead code and improves the
  example page. The known minors noted in v0.14.0 (partial `book` products on
  failure, no candidate-store cross-process lock) are still queued.
