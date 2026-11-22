# v0.16.0 — Reviewable results, not just a success message

Verovo 0.16.0 adds portable result proofs and self-contained usage-period replay. It keeps the existing canonical finance and buyer-review engines unchanged.

## What you can do

- Export a local buyer period with `verovo proof export --bundle PERIOD --out NEW_DIR --by REVIEWER`.
- Recheck the exported hashes and canonical output with `verovo proof replay PROOF_DIR --bundle PERIOD`. Changed inputs return EXPIRED; output drift returns FAIL.
- Build and verify a usage-only period with Buyer Ledger's existing `reproduce build` and `reproduce verify` commands.
- Publish an explicitly classified synthetic or approved public-redacted local copy, then inspect its four-stage reading page.
- Project either replay bundle into the common Prove Anything format with `reproduce proof BUNDLE --out NEW_DIR --by REVIEWER`.

## Proof boundaries

The reproduce projection allowlists manifest and aggregate replay fields. It excludes source paths, row-level events and subject identifiers. Missing private inputs keep independent replay UNAVAILABLE; external acceptance stays UNCHECKED. A source replay PASS establishes the recorded usage checks, not invoice reconciliation, delivery acceptance or buyer action.

Proof export, replay and projection are read-only with respect to their source. They do not send messages, authorize payment, submit a dispute or sign acceptance.

## Verification

619 healthcheck tests, 418 buyer-ledger tests, the selftest, metric registry, conformance vectors and SEO checks pass locally. GitHub CI is the release gate. Buyer Ledger remains a source-run component in this repository; this release does not invent a separate Buyer Ledger package.

The `@verovo/mcp` SDK keeps its own version. Public fixtures are synthetic and do not establish customer adoption, recovery or causal business value.
