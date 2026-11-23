# Verovo Compatible Architecture Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Centralize buyer-close state and file access, remove the measured intake hotspot, finish Money Contract v2, and preserve canonical finance, replay and signoff semantics.

**Architecture:** Keep `close.build_case` as a compatibility facade over a new `PeriodCase`. `CaseRepository` owns state paths, strict JSON reads and atomic writes; canonical finance and usage replay remain separate engines with separate proof scopes.

**Tech Stack:** Python 3.9+ standard library, `unittest`, `Decimal`, filesystem journals, existing canonical CSV/JSONL bundles.

## Global Constraints

- PASS/FAIL/UNKNOWN/UNPROVABLE and four completion axes never collapse into each other.
- Signed revisions stay immutable; signoff never authorizes payment or closes unresolved differences.
- `canonical_finance` remains the formal finance engine; `reproduce` remains self-contained usage replay.
- Core runtime remains Python 3.9 compatible and standard-library only.
- Source-only Buyer Ledger commands must be distinguished from commands covered by the `verovo` wheel smoke test.

---

### Task 1: CaseRepository and PeriodCase projection

**Files:**
- Create: `buyer-ledger/buyer_ledger/case_repository.py`
- Create: `buyer-ledger/buyer_ledger/period_case.py`
- Modify: `buyer-ledger/buyer_ledger/close.py` (`build_case` facade)
- Test: `buyer-ledger/tests/test_period_case.py`

**Interfaces:**
- Produces `CaseRepository(case_dir).path(name)`, `exists(name)`, `read_json(name, missing_ok=True)`, `atomic_write_bytes(path, data)`, `state_files()`.
- Produces `PeriodCase(case_dir).project()` returning the existing `verovo.buyer-close-case/1` dictionary.

- [ ] **Step 1: Add failing repository and golden-projection tests**

```python
repo = CaseRepository(case_dir)
self.assertIsNone(repo.read_json("review"))
(case_dir / "review.json").write_text("{bad")
with self.assertRaises(CloseError) as raised:
    repo.read_json("review")
self.assertEqual("ARTIFACT_PARTIAL", raised.exception.code)
self.assertEqual(close_build_case_before, PeriodCase(case_dir).project())
```

- [ ] **Step 2: Verify import and corruption tests fail**

Run: `python3 -m unittest tests.test_period_case -v` from `buyer-ledger/`.
Expected: FAIL because the new classes do not exist.

- [ ] **Step 3: Implement repository and move the projection**

Use same-directory temporary files, `os.replace`, strict object JSON parsing and registered state paths. Move the existing projection body into `PeriodCase` with lazy access to existing close helpers so tests that patch those helpers keep working; leave a thin `close.build_case` wrapper.

- [ ] **Step 4: Run close, signoff and recovery suites**

Run: `python3 -m unittest tests.test_period_case tests.test_close_period_import tests.test_close_report tests.test_close_revision tests.test_close_recovery -v`
Expected: PASS; projection is read-only and malformed existing artifacts fail loudly.

- [ ] **Step 5: Commit**

```bash
git add buyer-ledger/buyer_ledger/case_repository.py buyer-ledger/buyer_ledger/period_case.py buyer-ledger/buyer_ledger/close.py buyer-ledger/tests/test_period_case.py
git commit -m "refactor: introduce PeriodCase and CaseRepository"
```

### Task 2: Linear task coverage accounting

**Files:**
- Modify: `healthcheck/verovo_healthcheck/period_intake.py` (`coverage`)
- Test: `healthcheck/tests/test_period_intake.py`

**Interfaces:**
- Preserves `intake(bundle)["report"]["coverage"]` exactly.

- [ ] **Step 1: Add a failing instrumentation test**

Build disconnected tasks/runs/outcomes, replace the run/outcome containers with counting proxies, and assert coverage does not perform one full scan per task. Include normal, missing-link and duplicate cases.

- [ ] **Step 2: Confirm quadratic behavior fails the operation budget**

Run: `python3 -m unittest tests.test_period_intake.PeriodIntakeCoverageTests -v`
Expected: FAIL with O(tasks × rows) membership scans.

- [ ] **Step 3: Implement precomputed owner sets**

```python
tasks_with_run = {str(run.get("task_id") or "") for run in runs.values()}
tasks_with_outcome = {str(item.get("task_id") or "") for item in outcomes}
coverage = {
    "tasks_without_run": sum(task_id not in tasks_with_run for task_id in tasks),
    "tasks_without_outcome": sum(task_id not in tasks_with_outcome for task_id in tasks),
}
```

- [ ] **Step 4: Run intake/canonical regressions and benchmark**

Run: `python3 -m unittest tests.test_period_intake tests.test_canonical_finance -v`
Expected: PASS and byte-equivalent coverage. Re-run the 500/1000/2000/4000-row benchmark; growth should be approximately linear.

- [ ] **Step 5: Commit**

```bash
git add healthcheck/verovo_healthcheck/period_intake.py healthcheck/tests/test_period_intake.py
git commit -m "perf: index period task coverage"
```

### Task 3: Finish Money Contract v2

**Files:**
- Modify: `healthcheck/verovo_healthcheck/money.py`
- Modify: `healthcheck/verovo_healthcheck/optimization.py`
- Modify: `healthcheck/verovo_healthcheck/cost_drivers.py`
- Test: `healthcheck/tests/test_money_contract.py`

**Interfaces:**
- Existing `_dec` leniency remains local; display rounding delegates to `money.display`.

- [ ] **Step 1: Add failing half-up edge tests**

Assert `optimization._money(Decimal("0.125")) == "0.13"`, `cost_drivers._money(Decimal("0.125")) == "0.13"`, negative half-up behavior, and `optimization._money(None) is None`.

- [ ] **Step 2: Confirm default half-even behavior fails**

Run: `python3 -m unittest tests.test_money_contract -v`
Expected: FAIL with old `0.12` output.

- [ ] **Step 3: Delegate display rounding**

Import `money` and return `money.display(_dec(value))` for non-null optimization values and `money.display(value)` for cost drivers. Do not alter booking, allocation or zero-decimal currency tolerance logic.

- [ ] **Step 4: Run economics and golden reports**

Run: `python3 -m unittest tests.test_money_contract tests.test_cost_optimization tests.test_task_economics tests.test_canonical_finance -v`
Expected: PASS; normal two-decimal production fixtures are unchanged.

- [ ] **Step 5: Commit**

```bash
git add healthcheck/verovo_healthcheck/money.py healthcheck/verovo_healthcheck/optimization.py healthcheck/verovo_healthcheck/cost_drivers.py healthcheck/tests/test_money_contract.py
git commit -m "refactor: complete Money Contract v2 display migration"
```

### Task 4: Atomic reproduce bundle creation

**Files:**
- Modify: `buyer-ledger/buyer_ledger/reproduce.py`
- Test: `buyer-ledger/tests/test_reproduce.py`

**Interfaces:**
- `build_bundle` writes to a sibling temporary directory and atomically renames only after a complete manifest exists; `overwrite=True` never leaves a mixed old/new bundle.

- [ ] **Step 1: Add failing partial-output tests**

Patch replay or manifest writing to raise and assert a previously absent output remains absent and an existing overwritten bundle remains byte-identical.

- [ ] **Step 2: Confirm direct writes fail the test**

Run: `python3 -m unittest tests.test_reproduce -v`
Expected: FAIL because inputs are created before replay completes.

- [ ] **Step 3: Implement staging, backup and recovery**

Build in a sibling temporary directory, verify it, rename an existing output to a backup only at commit time, rename staging into place, restore the backup on failure, and remove temporary paths after success.

- [ ] **Step 4: Run all reproduce/proof regressions**

Run: `python3 -m unittest tests.test_reproduce tests.test_reproduce_proof tests.test_replayable_period_example -v`
Expected: PASS including exact-file-set and symlink refusal.

- [ ] **Step 5: Commit**

```bash
git add buyer-ledger/buyer_ledger/reproduce.py buyer-ledger/tests/test_reproduce.py
git commit -m "fix: make reproduce bundle creation atomic"
```

### Task 5: Distribution boundary and complete verification

**Files:**
- Modify: `.github/workflows/conformance.yml` (release smoke)
- Modify: `buyer-ledger/README.md`, `docs/feature-status.md`, `AGENTS.md`
- Modify: `healthcheck/pyproject.toml`, `CHANGELOG.md`, `healthcheck/verovo_healthcheck/__init__.py`

- [ ] **Step 1: Extend installed-artifact tests**

Add `verovo proof export` and `verovo proof replay` to release smoke. Document that `python3 -m buyer_ledger` close/reproduce remains a source-tree pre-M1 command unless separately packaged; no public install claim is inferred.

- [ ] **Step 2: Run all repository gates**

Run root 59 tests, Buyer Ledger discovery, healthcheck discovery, selftest, registry validation, all metric/outcome/delegation/SSE vectors, `gen_vocab.py --check`, and SEO checks. Expected: all PASS.

- [ ] **Step 3: Build and install the wheel outside the checkout**

Build with the standard `python -m build` workflow on Python 3.11+, install in a fresh venv, and execute every public release-smoke command plus the two proof commands.

- [ ] **Step 4: Commit release metadata**

Bump one patch/minor version based on the final public changes, update release notes and the stale documentation boundary, then commit only verified files.

- [ ] **Step 5: Merge, push, tag and verify deployment**

Push the refactor branch, merge to `main`, push `main`, create the prescribed release tag, and verify GitHub Release/PyPI/Pages workflows and public replay/example URLs.
