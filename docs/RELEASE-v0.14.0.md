# v0.14.0 — OCR bill golden chain and machine-readable delivery contract

CLI release 0.14.0 (healthcheck package; `@verovo/mcp` SDK versioning unchanged).

This release opens the review-mandated intake channel for bills and makes the
delivery surface machine-checkable. Buyer-ledger is 237 tests, healthcheck is
594 tests, all reference vectors pass.

## What is in it

- **V-08 OCR bill golden chain**: `bill-candidate add/review/book` — a bill
  screenshot (local Vision OCR), pasted text or injected OCR lines becomes an
  unreviewed candidate draft with field-level evidence (bbox region or text
  span + confidence per vendor/invoice_no/period/currency/amount/line item).
  Key fields and low-confidence or missing-evidence fields queue for named
  human review; the draft never books itself, and booking is an explicit
  `--by` act exporting a bills source (row-level provenance back to the
  candidate), the verbatim document and a `verovo.period-job/1` for the
  existing close chain.
- **Hardening from the adversarial review**: every unextracted key field stays
  JSON null (never invented), the image path preserves the exact OCR text and
  hash, real Vision OCR explicitly refuses non-darwin (`UNSUPPORTED_SCOPE`),
  line-total mismatches block booking, and mixed-currency close imports are
  rejected. A duplicate invoice_no refuses to double-book; bad OCR ends
  `BLOCKED_USER_INPUT` with one user action; `--engine paid-cloud-ocr` returns
  the §5 `requires_procurement` JSON without any transaction. `UNPROVABLE`
  stays a verification conclusion, never a delivery terminal state.
- **P0.1 delivery contract**: `python3 -m buyer_ledger contract show` prints
  `buyer-ledger/docs/delivery-contract.json` — supported intents, §3.1 delivery
  terminal states vs verification conclusions, exclusions, the §5 procurement
  template and evidence requirements. Consistency tests keep the file, the CLI
  first interaction and the implementation aligned.
- **Bilingual close reports**: `close status/show --format auto|json|md`
  renders Chinese markdown in a terminal while piped JSON stays a stable
  contract; the close report's four axes and five verdicts carry Chinese
  labels.
- **C-01-Lite delegation block** on `close status` (consistent with the ovo
  family contract).
- **healthcheck**: `evaluate()` reports every missing scope field in one
  `INPUT` error instead of only the first.

## Notes

- Website: ovo family siblings (Askovo research, Growovo execution) linked in
  the footer Project column.
- Known minor (non-blocking): a failed `book` can leave partial products in
  the target bundle (retry recovers); candidate store has no cross-process
  lock. Both are queued for a patch release.
