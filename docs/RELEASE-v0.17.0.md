# v0.17.0 — From audit findings to buyer actions

Verovo 0.17.0 turns an existing Audit Case into a line-level spend-action worklist and introduces a local Provider Manifest contract for replaceable agent-data adapters. It also aligns the public product narrative around independent measurement and audit for AI agents: audit charges, measure outcomes and keep actual buyer decisions separate.

## What changed

- `verovo spend-actions --case AUDIT_CASE` produces `ready_for_payment`, `evidence_required`, `review_required`, `unreviewed` and `settled` actions with owner roles, reasons, finding links and per-currency amount coverage. Only explicitly confirmed clean lines become ready for payment; an unflagged line is not implicitly cleared.
- `verovo provider check --manifest MANIFEST` validates engine identity, integration mode, source trust, explicit readiness checks, resource limits, coverage, cleanup and raw-snapshot digests. Local admission is `admit`, `degrade` or `refuse`; every result states `live_engine_contacted=false`.
- Added Spend Action List and Provider Manifest JSON Schemas, a digest-checked synthetic Provider example, updated buyer task entry points, and REA-inspired lifecycle and evidence boundaries for future adapters.
- Updated the website, bilingual README, product definition, commercial scope and validation roadmap. The next evidence gates are an actual buyer disposition, stop-receipt reconciliation with later charges, and a reusable private procurement comparison.

## Distribution and boundaries

Both new commands run locally with Python 3.9+ and the standard library. The installed wheel includes both workflows. They do not execute or authorize payment, contact an upstream engine, enforce a runtime budget or claim complete production coverage.

Financial correctness, authorization, delivery and economic value remain separate conclusions. Public benchmark rankings, universal connectors and real-time spending control are not shipped capabilities.

## Verification

647 healthcheck tests, 66 root tests, 430 Buyer Ledger tests and 91 Lab tests pass locally. Both new CLI workflows pass from source and an isolated wheel installation. The Audit golden case remains valid; selftest, generated vocabularies, metric registry, 62 protocol vectors, spec-drift and SEO checks pass. Desktop and mobile website rendering, plus the 24-page OVO BP PPTX/PDF, were also checked.

All new fixtures are synthetic. This release does not claim a real customer disposition, live provider integration, external recovery, effective runtime blocking or procurement validation. GitHub CI and release workflows remain the external release gates.
