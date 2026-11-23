# v0.16.1 — Compatible state, replay and performance hardening

Verovo 0.16.1 refactors the buyer-close state boundary and removes a measured Period Intake hotspot without changing canonical finance, evidence verdicts or signed-revision semantics.

## What changed

- `PeriodCase` owns the read-only close projection; `CaseRepository` owns state paths, strict JSON reads and atomic case writes. Existing `close.build_case` callers keep the same interface.
- Period Intake computes task coverage from prebuilt sets. On the review machine, the synthetic 4,000-row benchmark drops from approximately 0.66 seconds to 0.071 seconds with identical results.
- Money Contract v2 gives task economics, optimization and cost-driver displays one HALF_UP rounding rule. Zero-decimal currency tolerance and allocation arithmetic remain separate contracts.
- Reproduce bundles are built and verified in staging. Failure cannot expose a partial new bundle or damage the old bundle during overwrite.

## Distribution and proof boundaries

The installed `verovo` wheel covers canonical `proof export` and `proof replay`, and release smoke now runs both. Pre-M1 `python3 -m buyer_ledger` close/reproduce workflows remain source-checkout tools.

Replay PASS is not delivery acceptance, payment authorization, supplier recognition or realized savings. Signed revisions remain immutable and unresolved differences stay visible.

## Verification

59 root tests, 424 Buyer Ledger tests and 621 healthcheck tests pass locally. Selftest, the metric registry, 21 generated vocabularies and all 62 protocol vectors pass. SEO reports zero errors and warnings. GitHub CI and release workflows remain the external release gates.

All fixtures are synthetic. This release does not claim a real customer close, supplier acceptance, external recovery or causal business value.
