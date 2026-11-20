# v0.15.3 — handoff premises, shared error families, trustworthy bill lines

Patch release 0.15.3 (healthcheck package; `@verovo/mcp` SDK versioning
unchanged). buyer-ledger is 384 tests, healthcheck is 609 tests, the selftest,
metric registry, vocabulary check and all conformance vectors pass.

## Why 0.15.3 exists

Two gaps from the round-3 usability review, plus two places where a report or
a booking looked more certain than its inputs.

First, Growovo's handoff crossed the product boundary as evidence plus a
fingerprint, but the judgement it rested on — scope, assumptions, limits,
manual review state — stayed behind in the other repository. The buyer could
see what was recorded and not what it assumed.

Second, the two products spoke different words for the same refusal. A missing
input was `INPUT_REQUIRED` in Verovo and a bare message in Growovo, so a user
moving between them had to relearn the vocabulary instead of the task.

Third, a bill draft's line arithmetic used a tolerance that, in zero-decimal
currencies such as JPY and KRW, accepted any row; and a row whose numbers
could not be parsed could still be booked after a note said "accepted".
Fourth, the close report printed missing amounts as dashes and left the reader
to work out which number was unknown, why, and who must supply it.

## What is in it

- **premises travel with the handoff and never upgrade a grade**:
  `handoff import-growovo` accepts and preserves a `premises` object, records
  `premises_do_not_upgrade_grade: true`, and adds an open item to re-check the
  premises against buyer-controlled sources. A handoff without premises still
  imports. Premises sit inside the integrity fingerprint, so editing them
  refuses the whole import.
- **one shared error vocabulary, no shared database**:
  `buyer_ledger/error_semantics.py` classifies each frozen C-01 code into the
  seven families used by Growovo and documents what is missing, the impact and
  the legal next action. It fails at import time if `close.py` changes without
  the mapping. `close.py` itself is unchanged.
- **line arithmetic is checked against the printed amount**: quantity × unit
  price must match the line amount within
  `max(one smallest printed unit, 1% of |line amount|)`. A line whose quantity,
  unit price or amount is not a number cannot be booked even when it was
  "accepted with a note" — `book` raises `REVIEW_REQUIRED` and names the row.
  An inclusive period end date is converted to a half-open interval, and the
  draft records `period_end_converted_to_exclusive` with a note and a ×0.9
  confidence so the reviewer sees the conversion.
- **the close report names what is unknown**: the report body gains
  `amount_layers` (confirmed amounts, unattributed cost, missing records,
  what cannot be computed and why, what input is needed) and
  `open_item_groups` (one row per root cause, affected count, sample ids, one
  action). The Markdown keeps the per-item table and puts the grouped table
  above it. Review and signoff wording distinguishes "reviewed", "signed" and
  "all differences resolved"; a signed report states the unresolved count.

## Notes

- Verified capability boundary `V-36` is unchanged: a Growovo handoff proves
  what the other system recorded, not supplier acceptance, realized revenue or
  buyer signoff, and it never authorizes payment. Premises describe the other
  system's assumptions; they are not buyer evidence.
- No change to booking amounts, period-bundle contents or any verification
  verdict. The report changes are presentation of the same facts.
