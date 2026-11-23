# Spend Action List and Provider Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn an existing Audit Case into a line-level buyer worklist and validate a REA-inspired Provider Manifest without executing payments or claiming live upstream verification.

**Architecture:** Add two focused Python modules to the existing healthcheck package. `spend_action.py` reads the existing Audit Case and its source invoice lines; `provider_contract.py` validates adapter metadata and computes admission from explicit checks. Extend the existing CLI and schemas rather than adding a second finance engine or input ledger.

**Tech Stack:** Python 3.9+, standard library only, `decimal.Decimal`, `argparse`, `unittest`, JSON Schema documents.

## Global Constraints

- Preserve Python `>=3.9`; add no runtime dependency.
- Unknown amounts, coverage, readiness and cleanup never default to zero, empty or success.
- `ready_for_payment` is a recommendation only for explicitly clean lines; the buyer decides and records the actual action separately.
- Provider checks validate manifests only. They do not contact an upstream engine or prove production coverage.
- Keep one vendor/account/period scope and preserve currencies separately.
- Follow existing CLI exit codes: 0 for completed analysis, 2 for input/output errors.

---

### Task 1: Line-level Spend Action List

**Files:**
- Create: `healthcheck/verovo_healthcheck/spend_action.py`
- Create: `healthcheck/tests/test_spend_action.py`
- Create: `schemas/spend-action-list.schema.json`
- Modify: `healthcheck/verovo_healthcheck/cli.py`
- Modify: `examples/audit-golden-case/README.md`

**Interfaces:**
- Consumes: existing `audit-case.json`, sibling `manifest.json`, invoice JSON documents with `lines`.
- Produces: `build_action_list(case, case_root, manifest=None) -> dict`, `action_markdown(result) -> str`, and `verovo spend-actions --case PATH [--json PATH] [--markdown PATH]`.

- [ ] **Step 1: Write failing behavior tests**

```python
def test_golden_case_separates_clean_open_unreviewed_and_settled_lines(self):
    result = spend_action.build_action_list(self.case, self.root, self.manifest)
    rows = {row["source"]["line_id"]: row for row in result["actions"]}
    self.assertEqual(rows["line-004"]["action"], "ready_for_payment")
    self.assertEqual(rows["line-007"]["action"], "evidence_required")
    self.assertEqual(rows["line-005"]["action"], "review_required")
    self.assertEqual(rows["line-003"]["action"], "settled")
    self.assertEqual(rows["line-002"]["action"], "unreviewed")
    self.assertEqual(rows["line-004"]["amount"]["value"], "100.00")
    self.assertFalse(result["executes_payment"])
```

Add separate tests for case-level `unverified_outcome`, mixed-currency summaries, missing amounts, paths escaping the case root, and the rule that an unflagged line is not implicitly payable.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_spend_action -v` from `healthcheck/`.
Expected: import failure because `verovo_healthcheck.spend_action` does not exist.

- [ ] **Step 3: Implement the minimal action projector**

Implement these exact public functions:

```python
def build_action_list(case, case_root, manifest=None):
    """Return verovo.spend-action-list/0.1 without modifying the case."""
```

```python
def action_markdown(result):
    """Render actions, owner roles, reasons and per-currency totals."""
```

Use `Decimal(str(value))`, quantize with the existing HALF_UP convention, and reject missing/non-finite amounts. Discover invoice sources from the optional manifest and finding evidence. Only `manifest.clean_charge_lines` can produce `ready_for_payment`. Open unprovable or evidence-class findings produce `evidence_required`; other open findings produce `review_required`; realized-only lines produce `settled`; all other lines remain `unreviewed`.

- [ ] **Step 4: Run unit tests and verify GREEN**

Run: `python3 -m unittest tests.test_spend_action -v` from `healthcheck/`.
Expected: all spend-action tests pass with no cross-currency sum and no missing amount converted to zero.

- [ ] **Step 5: Add the CLI test first, then wire the command**

Test `main(["spend-actions", "--case", path, "--json", out, "--markdown", md])` end to end. Verify it fails before wiring, then add `cmd_spend_actions`, imports and parser arguments. Print Markdown by default and support `--json-only` for machine pipelines.

- [ ] **Step 6: Add schema and golden-case documentation**

Define the output schema with actions `ready_for_payment`, `evidence_required`, `review_required`, `unreviewed`, `settled`. Document the exact local command and the fact that buyer payment/disposition remains an external action.

- [ ] **Step 7: Run the complete healthcheck suite**

Run: `python3 -m unittest discover -s tests` from `healthcheck/`.
Expected: the original 621 tests plus new tests pass.

### Task 2: Provider Manifest and Admission

**Files:**
- Create: `healthcheck/verovo_healthcheck/provider_contract.py`
- Create: `healthcheck/tests/test_provider_contract.py`
- Create: `schemas/provider-manifest.schema.json`
- Create: `examples/provider-manifest/otel-file.json`
- Modify: `healthcheck/verovo_healthcheck/cli.py`
- Create: `docs/provider-contract.md`

**Interfaces:**
- Consumes: `verovo.provider-manifest/0.1` JSON with adapter, engine, source, explicit readiness checks, limits, coverage and cleanup.
- Produces: `evaluate_manifest(document) -> dict`, `load_manifest(path) -> dict`, and `verovo provider check --manifest PATH [--json PATH]`.

- [ ] **Step 1: Write failing contract and admission tests**

```python
def test_valid_file_provider_is_admitted_with_partial_coverage(self):
    result = provider_contract.evaluate_manifest(self.valid)
    self.assertEqual(result["admission"], "admit")
    self.assertEqual(result["coverage"]["status"], "partial")
```

Add independent tests for required check failure, required check unknown, optional check failure, retained or unknown cleanup, process engine without a 64-character SHA-256, boolean or non-positive limits, negative coverage counts, and the rule that declared `coverage=complete` cannot override failed checks.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_provider_contract -v` from `healthcheck/`.
Expected: import failure because `provider_contract` does not exist.

- [ ] **Step 3: Implement strict validation and admission**

```python
def evaluate_manifest(document):
    """Validate the manifest and return admission plus explicit reasons."""
```

Admission is `refuse` when a required check fails or is unknown, or cleanup is retained/unknown. It is `degrade` when an optional check fails or is unknown. Otherwise it is `admit`. Every check explicitly declares `required`; there is no hidden default. Process engines require `executable_sha256`. Limits are positive integers; coverage counts are non-negative integers; booleans are refused as integers.

- [ ] **Step 4: Run contract tests and verify GREEN**

Run: `python3 -m unittest tests.test_provider_contract -v` from `healthcheck/`.
Expected: all provider tests pass and no manifest field is mutated.

- [ ] **Step 5: Add the CLI test first, then wire `provider check`**

Verify JSON output and exit 2 for invalid input. Add the nested parser without changing existing Provider SDK behavior.

- [ ] **Step 6: Add schema, example and documentation**

Pin the example to a file-based OTel adapter with partial coverage, exact versions, source trust fields, resource limits and released cleanup. State that `admit` means the manifest passes local admission rules, not that the engine was contacted or its records are complete.

- [ ] **Step 7: Run all healthcheck tests**

Run: `python3 -m unittest discover -s tests` from `healthcheck/`.
Expected: all tests pass.

### Task 3: Release Alignment and End-to-End Verification

**Files:**
- Modify: `healthcheck/pyproject.toml`
- Modify: `healthcheck/verovo_healthcheck/__init__.py`
- Modify: `CHANGELOG.md`
- Modify: `docs/feature-status.md`
- Modify: `ROADMAP.md`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `website/index.html`

- [ ] **Step 1: Run both real CLI workflows**

Run `python3 healthcheck/verovo spend-actions --case examples/audit-golden-case/audit-case.json --json /tmp/verovo-actions.json --markdown /tmp/verovo-actions.md` and `python3 healthcheck/verovo provider check --manifest examples/provider-manifest/otel-file.json --json /tmp/verovo-provider.json`.
Expected: both exit 0; outputs match the unit-tested contracts.

- [ ] **Step 2: Validate examples and negative paths**

Run `python3 scripts/validate_audit_case.py examples/audit-golden-case/audit-case.json` and the new negative contract tests.
Expected: `VALID`; invalid paths, missing evidence, unsupported runtime and retained cleanup fail closed.

- [ ] **Step 3: Align release metadata and public capability boundaries**

Set the next minor version only after the features pass. Document the new local worklist and manifest check as available, while keeping live provider connectivity, real customer disposition, runtime spending enforcement and public benchmarks unverified or planned.

- [ ] **Step 4: Run repository verification**

Run the AGENTS.md commands, root tests, buyer-ledger tests, Lab tests, generated vocabulary check and local website link checks.
Expected: all relevant existing and new checks pass; pre-existing ResourceWarnings are recorded but not represented as new failures.

- [ ] **Step 5: Review the final diff**

Confirm no generated reports, credentials, browser profiles or temporary outputs enter the commit. Check that public text distinguishes current local capability from roadmap and real-customer validation.
