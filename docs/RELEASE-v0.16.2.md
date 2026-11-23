# v0.16.2 — Consistent revisions and verifiable signoff

Verovo 0.16.2 closes the remaining buyer-close consistency gaps: direct read-only projections cannot expose mixed revisions, legacy Case writes share one repository boundary, and a signoff must bind the exact report and calculation it approves.

## What changed

- `PeriodCase` compares byte-level state snapshots and rejects unfinished journals or state changes during projection. `close status` still performs recovery first and exposes recovery diagnostics, so it is not a side-effect-free read.
- Legacy close init, ingest and mapping writes use `CaseRepository` for jobs, scans, mappings, canonical tables, rule maps and manifests.
- Signoff projection requires matching report revision and report/calculation/canonical fingerprints. Revision-built reports now carry their actual revision id instead of defaulting to r1, so an old signoff cannot endorse a new report.
- Shared Proof output separates `contract_validation.status` from `business_verdict.status`; naive timestamps are recorded explicitly and cannot become a determined business PASS.

## Distribution and boundaries

The installed `verovo` wheel continues to cover canonical `proof export` and `proof replay`. Pre-M1 `python3 -m buyer_ledger` close/reproduce workflows remain source-checkout tools.

A valid Proof contract is not business success. Replay PASS is not delivery acceptance, payment authorization, supplier recognition or realized savings. Signed revisions remain immutable and unresolved differences stay visible.

## Verification

61 root tests, 430 Buyer Ledger tests and 621 healthcheck tests pass locally. Selftest, the metric registry, 21 generated vocabularies and all 62 protocol vectors pass. Wheel build and isolated CLI checks pass. GitHub CI and release workflows remain the external release gates.

All fixtures are synthetic. This release does not claim a real customer close, supplier acceptance, external recovery or causal business value.
