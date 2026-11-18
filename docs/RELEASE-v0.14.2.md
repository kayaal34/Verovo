# v0.14.2 — review follow-ups: money contract, mapping disclosure, signoff timing

Patch release 0.14.2 (healthcheck package; `@verovo/mcp` SDK versioning
unchanged). buyer-ledger is 240 tests, healthcheck is 600 tests, all
reference vectors pass.

## What is in it

- **V-2 money contract v1**: cents-exact amounts have one shared definition
  (`verovo_healthcheck/money.py`); callers can no longer drift on rounding.
- **V-02 mapping disclosure**: close mapping previews name their open items;
  the confirm output discloses the gaps it accepted.
- **V-07 signoff timing**: signoff race assertions follow the real ordering,
  with a deterministic lock-contention case.
- **e2e quickstart trust**: the economics demo case passes the buyer-evidence
  gate it documents; quickstart hints point at real CLI paths.

## Notes

- No release-process changes; behaviour of existing commands is unchanged
  apart from the stricter/shared definitions above.
