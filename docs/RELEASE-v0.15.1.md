# v0.15.1 — controlled buyer identity and post-signoff reports (release-notes fix)

Patch release 0.15.1 (healthcheck package; `@verovo/mcp` SDK versioning
unchanged). buyer-ledger is 354 tests, healthcheck is 609 tests, the selftest
and metric registry pass.

## Why 0.15.1 exists

`v0.15.0` was tagged and passed `release-guard`, but its GitHub Release step
failed: `docs/RELEASE-v0.15.0.md` was missing from the tagged commit, and the
workflow refuses to fall back to older notes. No GitHub Release was created for
`v0.15.0`; the tag itself is untouched. `v0.15.1` carries the same product
changes plus the release notes this file provides.

## What is in it

- **V-21 acceptance authority is a controlled identity, not a self-declared
  string**: acceptance comes from the controlled organization record and its
  credentials. A supplier accepting its own delivery, an unknown organization,
  or a forged buyer label resolves to `UNPROVABLE` and can never reach Buyer
  Verified `PASS`. The report header names the party that actually accepted.
  `AcceptanceAuthorityTests` covers supplier, unknown-org, forged-string and
  legitimate-buyer cases.
- **V-02 the delivered report is regenerated after signoff**: signoff re-renders
  `report-delivered.md`/`.json` bound to the signed revision, the input
  fingerprint and the signoff reference. The pre-signoff snapshot is retained
  and labelled as pre-signoff. Evidence added after signoff can only produce a
  new revision; it never mutates the signed report.

## Notes

- Verified capability boundary `V-36`: importing a Growovo handoff proves what
  the other system recorded. It does not prove supplier acceptance, realized
  revenue or buyer signoff, and it never authorizes payment.
- No other behaviour changes: the only difference from `v0.15.0` is this file
  and the version string.
