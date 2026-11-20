# v0.15.2 — bill source integrity and fail-closed candidate locking

Patch release 0.15.2 (healthcheck package; `@verovo/mcp` SDK versioning
unchanged). buyer-ledger is 363 tests, healthcheck is 609 tests, the selftest,
metric registry and conformance vectors pass.

## Why 0.15.2 exists

V-09 made booking atomic and recoverable, but it proved only that the write
completed — not that the file being written was still the file a human
reviewed. The candidate store saved the source SHA-256 at creation and then
never re-checked it, so a bill could be edited after review and still be
booked with the old reviewed fields, leaving a period bundle whose voucher no
longer matched the review. The candidate lock also silently degraded to
no-lock where `fcntl` is unavailable, which contradicted the fail-closed
discipline used by the period revision lock.

## What is in it

- **V-09b the booked bill is the reviewed bill**: `review_field` and `book`
  re-verify the source file's SHA-256 and the OCR artefacts (`ocr.txt`
  `raw_text_sha256`, payload schema, lines and reconstructed text) before
  reusing an earlier review. Any mismatch raises `STALE_SOURCE`; `book` checks
  once more immediately before copying the source into the period bundle, so a
  change between chunks is caught as well. `test_bill_candidate_integrity.py`
  covers a swapped source, edited OCR text, a tampered OCR payload and the
  clean path.
- **candidate locking fails closed**: when `fcntl` cannot be imported, the
  store raises `UNSUPPORTED_CAPABILITY` instead of proceeding without a lock.
  The code is registered in the close error-code contract.

## Notes

- Verified capability boundary `V-36` is unchanged: a Growovo handoff proves
  what the other system recorded, not supplier acceptance, realized revenue or
  buyer signoff, and it never authorizes payment.
- No other behaviour changes: booking output, period bundles and report
  formats are untouched on the clean path.
