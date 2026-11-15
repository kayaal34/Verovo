# v0.14.0 — buyer ledger from a clean install, and a Chinese landing page

CLI release **0.14.0**.

This release closes a gap that a first-time test exposed: the buyer-ledger
workflow (`close init → ingest → mapping → calculate → report → signoff`) only
worked from a git checkout. `pip install verovo` never carried it, so the part
of the product that produces a reviewable month-end artifact was unreachable
for anyone who did not clone the repository.

## What changes for you

| Area | Before | Now |
|---|---|---|
| Buyer ledger | only from a checkout (`python3 -m buyer_ledger`) | also `pip install verovo-buyer-ledger`, then `verovo-close …` |
| Mapping review | `close mapping` printed `open items: 1` | each gap is named (source / field / reason) |
| Mapping confirm | the suggested command could fail with `REVIEW_REQUIRED` on an unruled control domain | the suggested command carries the exact `--control-domain` flags |
| Confirming gaps | silent | prints `approved unmapped fields as gaps: role.field` |
| Website | English only | `/zh/` landing page with reciprocal `hreflang` |

The buyer ledger is distributed as a **separate** distribution on purpose: it
keeps `pip install verovo` unchanged for people who only want recounts and
reviews. It is not on PyPI yet (a new PyPI project needs a one-time trusted
publisher entry), so install it from the release asset:

```bash
pip install "verovo>=0.14.0"
pip install https://github.com/roy-tong/Verovo/releases/download/v0.14.0/verovo_buyer_ledger-0.1.0-py3-none-any.whl
verovo-close --help
```

A checkout still works exactly as before: `python3 -m buyer_ledger close …`.

## Also in this release

- **V-06 / V-07** (merged since 0.13.0): a journalled recovery path for
  revisions and signoff, and an exclusive per-case lock so two processes can
  never revise or sign the same period at once. The concurrency test that made
  CI red on `main` was itself over-specified — it required the losing process
  to report a lock conflict, when losing the race after the winner finished is
  an equally safe, explicit refusal. The assertion now accepts both and a
  deterministic lock-holder test covers the conflict path.
- **C-01-Lite**: `close status` carries the eight-field delegation projection.
- **Website**: Chinese landing page (`/zh/`), reciprocal `hreflang`, and OG /
  JSON-LD coverage on every user-facing page. Two example pages referenced a
  stylesheet (`../site.css`) that never existed; they now use `../light.css`.

## What this release does not claim

- **No real customer bill was processed.** Every end-to-end run in this release
  used synthetic bundles. The 26-tool catalog is a catalog, not 26 live
  integrations, and `live_bill_validated` is still 0.
- **The ledger is not zero-dependency.** `close calculate` and `close report`
  reuse `verovo_healthcheck.canonical_finance`, so the package declares
  `verovo>=0.13.0`. Everything else in the ledger is standard library.
- **Signoff is not payment approval.** A signed revision records a named
  reviewer's scope and the fingerprints of the report it signed; it never
  authorizes payment and never closes unresolved items.
- **The Chinese page is a translation, not a new claim.** It mirrors the English
  landing page; the boundary statements are carried over, not softened.

## Verification

```
buyer-ledger: 201 tests OK
healthcheck:  593 tests OK
selftest:     PASS
registry:     18 metrics VALID
conformance:  22/22 metrics, 23/23 delegation, 15/15 outcome, 2/2 SSE
website:      seo_check 19 pages, 0 errors (2 warnings, social-preview asset)
```

Wheels built and installed in clean virtualenvs: `verovo 0.14.0` and
`verovo_buyer_ledger 0.1.0`; the seven-step `close` flow ran to `exit=0` at every
step, from a directory outside the repository.
