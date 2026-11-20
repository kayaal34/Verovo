# v0.15.4 — Growovo handoff keeps its task identity

Patch release 0.15.4 (healthcheck package; `@verovo/mcp` SDK versioning
unchanged). buyer-ledger is 396 tests, healthcheck is 609 tests, the selftest,
metric registry, vocabulary check and all conformance vectors pass.

## Why 0.15.4 exists

Growovo's handoff already crossed the boundary as evidence plus a fingerprint,
but the stable identity of a task inside the exporter's run was only implied by
position. A buyer reading the package could see an operation was committed and
still have to guess which exported task it belonged to.

Growovo now exports a `task_bindings` table and derives one stable id per
operation (`growovo:<run>/op/<id>`) and per business event
(`growovo:<run>/ev/<id>`). Verovo needs a way to turn that handoff into Period
Intake tables without inventing an identity of its own.

The other half is an old rule applied to a new command: a normalized package
must never silently overwrite a period bundle that is already in use.

## What is in it

- **`handoff normalize-growovo <handoff.json> --out <dir>`** validates the
  handoff against the Growovo contract first and writes
  `tasks.csv`, `runs.jsonl`, `outcomes.csv` and `manifest.json` into a fresh
  directory. A handoff that fails validation writes nothing and does not create
  the directory.
- **task identity comes from the payload when it is there**:
  `manifest.task_bindings_source` is `payload` when the handoff carries usable
  bindings and `derived_fallback` for older packages, where the same formula is
  applied locally and the manifest says the identity was derived, not exported.
- **no silent overwrite**: `--out` must be absent or empty, otherwise
  `handoff output directory must be absent or empty`. Files are written
  atomically and already-written files are removed if a later write fails.
- **the evidence boundary stays visible**: the manifest records
  `booking_status: evidence_only_not_canonical`,
  `receipt_verified_by_verovo: false`,
  `business_outcome_observed_by_verovo: false` and
  `terminal_state: DELIVERED_WITH_GAP`, and the CLI prints the same limits after
  writing. A committed operation is an action fact; product events are
  unverified by default and a dry run counts as neither execution nor cost.

## Notes

- Verified capability boundary `V-36` is unchanged: a Growovo handoff proves
  what the other system recorded, not supplier acceptance, realized revenue or
  buyer signoff, and normalization never books an amount.
- `task_id` is a stable identifier inside the exporter's run; it is not a buyer
  business task number and carries no buyer meaning.
