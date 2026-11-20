"""V-NEXT-2 acceptance tests: allocation policy versions and fingerprints.

Every fixture is synthetic.  Allocation never rewrites canonical charges,
never touches delivery or sign-off state and never emits an invoice.
"""
import csv
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from buyer_ledger import allocation_policy as ap

FIXED = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)

CHARGES = {
    "schema": ap.INPUT_SCHEMA,
    "currency": "USD",
    "charges": [
        {"charge_id": "c1", "amount": "600", "category": "usage",
         "cost_center": "alpha"},
        {"charge_id": "c2", "amount": "400", "category": "usage",
         "cost_center": "beta"},
        {"charge_id": "c3", "amount": "100", "category": "tax",
         "cost_center": "alpha"},
    ],
}

DRIVER = {
    "schema": "verovo.driver-input/1",
    "metric_code": "api_call",
    "unit": "call",
    "values": [{"cost_center": "alpha", "value": "3"},
               {"cost_center": "beta", "value": "1"}],
}


def _policy(**overrides):
    payload = {
        "schema": ap.SCHEMA,
        "policy_id": "alloc-2026-10",
        "policy_version": "1",
        "scope": "平台共享成本，2026-10",
        "window": {"from": "2026-10-01", "to": "2026-11-01"},
        "timezone": "UTC",
        "resolution": "cost_center_month",
        "driver_metric": "api_call",
        "driver_source": "usage-replay:ur-1",
        "driver_unit": "call",
        "idle_handling": "hold_unallocated",
        "shared_cost_handling": "proportional",
        "overhead_handling": "exclude",
        "rounding": {"quantum": "0.01", "mode": "down"},
        "negative_amount_handling": "allocate",
        "exclusion_rule": {"categories": ["tax"]},
        "residual_rule": "rounding_to_unallocated",
        "source_hashes": ["sha256:1111"],
        "rule_map_hash": "sha256:2222",
    }
    payload.update(overrides)
    return payload


def _approved(policy=None, **overrides):
    approver = overrides.pop("approved_by", "buyer-cfo")
    base = policy if policy is not None else _policy(**overrides)
    return ap.approve_policy(base, approved_by=approver,
                             approved_at="2026-11-05T09:00:00Z",
                             approval_ref="APR-2026-10")


def _run(policy, charges=None, driver=None, **kwargs):
    return ap.allocate(policy, charges=charges or CHARGES,
                       driver=driver or DRIVER,
                       generated_at=kwargs.pop("generated_at", FIXED),
                       **kwargs)


def _unallocated_reasons(result):
    return [item["reason"] for item in result["unallocated_items"]]


def _all_reasons(result):
    return _unallocated_reasons(result) + [
        item["reason"] for item in result["non_allocatable_items"]]


class AllocationPolicyTest(unittest.TestCase):

    # -- VN2-01 ----------------------------------------------------------
    def test_vn2_01_rule_change_requires_new_version(self):
        policy = _policy()
        with self.assertRaises(ap.AllocationError) as caught:
            ap.revise_policy(policy, {"rounding": {"quantum": "0.01",
                                                   "mode": "up"}},
                             policy_version="1")
        self.assertEqual(caught.exception.code, "POLICY_VERSION_CONFLICT")
        revised = ap.revise_policy(policy, {"rounding": {"quantum": "0.01",
                                                         "mode": "up"}},
                                   policy_version="2", reason="向上取整")
        self.assertEqual(revised["policy_version"], "2")
        self.assertEqual(revised["parent_policy_version"], "1")
        self.assertEqual(revised["state"], "draft")
        self.assertNotEqual(ap.policy_hash(revised), ap.policy_hash(policy))
        self.assertNotIn("approved_by", revised)

    # -- VN2-02 ----------------------------------------------------------
    def test_vn2_02_two_approved_policies_diff_explainable(self):
        first = _approved()
        second_draft = ap.revise_policy(
            first, {"negative_amount_handling": "hold_unallocated"},
            policy_version="2", reason="负 credit 不进分摊")
        second = _approved(second_draft, approved_by="buyer-ops")
        diff = ap.diff_policies(first, second)
        fields = [item["field"] for item in diff["result_affecting_changes"]]
        self.assertIn("negative_amount_handling", fields)
        self.assertTrue(diff["hash_changed"])
        self.assertIn("复核", diff["explanation"])
        self.assertIn("不说明业务好坏", diff["note"])

        credit = {"currency": "USD",
                  "charges": [{"charge_id": "c1", "amount": "1000",
                               "cost_center": "alpha"},
                              {"charge_id": "c2", "amount": "-100",
                               "cost_center": "alpha"}]}
        one = _run(first, charges=credit)
        two = _run(second, charges=credit)
        self.assertEqual(one["totals"]["allocated"], "900.0000")
        self.assertEqual(two["totals"]["allocated"], "1000.0000")
        self.assertEqual(two["totals"]["non_allocatable"], "-100.0000")
        self.assertNotEqual(one["policy"]["policy_version"],
                            two["policy"]["policy_version"])

    # -- VN2-03 ----------------------------------------------------------
    def test_vn2_03_missing_driver_stays_unallocated(self):
        charges = {"currency": "USD",
                   "charges": [{"charge_id": "c9", "amount": "400",
                                "cost_center": "gamma"}]}
        result = _run(_approved(), charges=charges)
        self.assertEqual(result["allocations"], [])
        self.assertEqual(result["unallocated_items"][0]["reason"],
                         "DRIVER_MISSING")
        self.assertEqual(result["totals"]["unallocated"], "400.0000")
        self.assertTrue(result["identity"]["holds"])
        self.assertEqual(result["status"], "OK")

    # -- VN2-04 ----------------------------------------------------------
    def test_vn2_04_zero_driver_total_no_division(self):
        driver = {"schema": "verovo.driver-input/1", "metric_code": "api_call",
                  "unit": "call",
                  "values": [{"cost_center": "alpha", "value": "0"},
                             {"cost_center": "beta", "value": "0"}]}
        result = _run(_approved(), driver=driver)
        self.assertEqual(result["allocations"], [])
        self.assertEqual(result["driver"]["total"], "0.0000")
        self.assertEqual(set(_unallocated_reasons(result)),
                         {"DRIVER_ZERO_TOTAL"})
        self.assertEqual(result["status"], "unverifiable")
        self.assertIn("DRIVER_ZERO_TOTAL", result["status_reasons"])
        self.assertTrue(result["identity"]["holds"])

    # -- VN2-05 ----------------------------------------------------------
    def test_vn2_05_unit_mismatch_rejected(self):
        driver = dict(DRIVER, unit="hours")
        with self.assertRaises(ap.AllocationError) as caught:
            _run(_approved(), driver=driver)
        self.assertEqual(caught.exception.code, "DRIVER_UNIT_MISMATCH")
        driver = dict(DRIVER, metric_code="tokens")
        with self.assertRaises(ap.AllocationError) as caught:
            _run(_approved(), driver=driver)
        self.assertEqual(caught.exception.code, "DRIVER_METRIC_MISMATCH")

    # -- VN2-06 ----------------------------------------------------------
    def test_vn2_06_identity_holds(self):
        result = _run(_approved())
        self.assertTrue(result["identity"]["holds"])
        self.assertEqual(result["identity"]["difference"], "0.0000")
        self.assertEqual(result["totals"]["charges"], "1100.0000")
        self.assertEqual(result["totals"]["eligible"], "1000.0000")
        self.assertEqual(result["totals"]["allocated"], "1000.0000")
        self.assertEqual(result["totals"]["unallocated"], "0.0000")
        self.assertEqual(result["totals"]["non_allocatable"], "100.0000")
        self.assertEqual(result["identity"]["allocatable"], "1100.0000")
        units = {item["charge_id"]: item["unit_cost"]
                 for item in result["allocations"]}
        self.assertEqual(units, {"c1": "200.0000", "c2": "400.0000"})

    # -- VN2-07 ----------------------------------------------------------
    def test_vn2_07_rounding_residual_single_line(self):
        charges = {"currency": "USD",
                   "charges": [{"charge_id": "shared", "amount": "100",
                                "category": "platform"}]}
        driver = {"schema": "verovo.driver-input/1", "metric_code": "api_call",
                  "unit": "call",
                  "values": [{"cost_center": "a", "value": "1"},
                             {"cost_center": "b", "value": "1"},
                             {"cost_center": "c", "value": "1"}]}
        result = _run(_approved(), charges=charges, driver=driver)
        amounts = [item["amount"] for item in result["allocations"]]
        self.assertEqual(amounts, ["33.3300", "33.3300", "33.3300"])
        residuals = [item for item in result["unallocated_items"]
                     if item["reason"] == "ROUNDING_RESIDUAL"]
        self.assertEqual(len(residuals), 1)
        self.assertEqual(residuals[0]["amount"], "0.0100")
        self.assertEqual(result["identity"]["residual"], "0.0100")
        total = sum(Decimal(item["amount"]) for item in result["allocations"])
        total += Decimal(result["totals"]["unallocated"])
        self.assertEqual(total, Decimal("100"))
        self.assertTrue(result["identity"]["holds"])

    # -- VN2-08 ----------------------------------------------------------
    def test_vn2_08_negative_credit_by_rule(self):
        credit = {"currency": "USD",
                  "charges": [{"charge_id": "c1", "amount": "1000",
                               "cost_center": "alpha"},
                              {"charge_id": "credit", "amount": "-100",
                               "cost_center": "alpha"}]}
        allocated = _run(_approved(), charges=credit)
        self.assertEqual(allocated["totals"]["allocated"], "900.0000")
        self.assertEqual(allocated["totals"]["non_allocatable"], "0.0000")

        held = _run(_approved(negative_amount_handling="hold_unallocated"),
                    charges=credit)
        self.assertEqual(held["totals"]["allocated"], "1000.0000")
        self.assertEqual(held["totals"]["non_allocatable"], "-100.0000")
        self.assertIn("NEGATIVE_HELD", _all_reasons(held))
        self.assertEqual(held["identity"]["allocatable"], "900.0000")
        self.assertTrue(held["identity"]["holds"])

        with self.assertRaises(ap.AllocationError) as caught:
            _run(_approved(negative_amount_handling="reject"), charges=credit)
        self.assertEqual(caught.exception.code, "NEGATIVE_AMOUNT_REJECTED")

    # -- VN2-09 ----------------------------------------------------------
    def test_vn2_09_unapproved_draft_only(self):
        result = _run(_policy())
        self.assertEqual(result["status"], "draft_only")
        self.assertEqual(result["policy"]["state"], "draft")
        self.assertEqual(result["allocations"], [])
        self.assertEqual(set(_unallocated_reasons(result)),
                         {"POLICY_NOT_APPROVED"})

    # -- VN2-10 ----------------------------------------------------------
    def test_vn2_10_missing_source_marks_unit_cost_unverifiable(self):
        policy = _approved(_policy(source_hashes=[], rule_map_hash=""))
        result = _run(policy)
        self.assertEqual(result["status"], "unverifiable")
        self.assertIn("SOURCE_HASH_MISSING", result["status_reasons"])
        self.assertIn("RULE_MAP_HASH_MISSING", result["status_reasons"])
        self.assertTrue(result["allocations"])
        for item in result["allocations"]:
            self.assertFalse(item["unit_cost_verifiable"])

    # -- VN2-11 ----------------------------------------------------------
    def test_vn2_11_delivery_stays_unknown(self):
        result = _run(_approved())
        self.assertEqual(result["delivery_state"], "UNKNOWN")
        self.assertEqual(result["effects"]["charges_delta"], "none")
        self.assertEqual(result["effects"]["delivery"], "UNKNOWN")
        self.assertEqual(result["effects"]["signoff"], "none")

    # -- VN2-12 ----------------------------------------------------------
    def test_vn2_12_roi_null_without_economics(self):
        result = _run(_approved())
        self.assertIsNone(result["roi"]["value"])
        self.assertEqual(result["roi"]["reason"], "ECONOMICS_MISSING")
        zero = _run(_approved(), economics={"benefit_amount": "10",
                                            "cost_amount": "0"})
        self.assertEqual(zero["roi"]["reason"], "ZERO_COST")
        ratio = _run(_approved(), economics={"benefit_amount": "200",
                                             "cost_amount": "100"})
        self.assertEqual(ratio["roi"]["value"], "2.0000")
        self.assertIn("不代表因果", ratio["roi"]["note"])

    # -- VN2-13 ----------------------------------------------------------
    def test_vn2_13_replay_stable(self):
        policy = _approved()
        first = _run(policy)
        second = _run(policy)
        self.assertEqual(first, second)
        self.assertEqual(first["inputs"]["input_fingerprint"],
                         second["inputs"]["input_fingerprint"])

    # -- VN2-14 ----------------------------------------------------------
    def test_vn2_14_new_policy_does_not_transfer_signoff(self):
        first = _approved()
        snapshot = json.dumps(first, sort_keys=True)
        second_draft = ap.revise_policy(
            first, {"rounding": {"quantum": "0.01", "mode": "nearest"}},
            policy_version="2", reason="改舍入")
        for field in ("approved_by", "approved_at", "approval_ref"):
            self.assertNotIn(field, second_draft)
        self.assertEqual(json.dumps(first, sort_keys=True), snapshot)
        second = _approved(second_draft, approved_by="buyer-ops-2")
        one = _run(first)
        two = _run(second)
        self.assertEqual(one["policy"]["approved_by"], "buyer-cfo")
        self.assertEqual(two["policy"]["approved_by"], "buyer-ops-2")
        self.assertNotEqual(one["policy"]["policy_hash"],
                            two["policy"]["policy_hash"])

    # -- XN2-02 ----------------------------------------------------------
    def test_xn2_02_canonical_hash_unchanged_by_projection(self):
        policy = _approved()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(json.dumps(policy, ensure_ascii=False),
                            encoding="utf-8")
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            result = _run(ap.load_policy(path))
            ap.write_result(result, Path(tmp) / "out")
            after = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(before, after)

    # -- XN2-03 ----------------------------------------------------------
    def test_xn2_03_repeat_projection_idempotent(self):
        policy = _approved()
        with tempfile.TemporaryDirectory() as tmp:
            first = _run(policy)
            second = _run(policy)
            ap.write_result(first, Path(tmp) / "a")
            ap.write_result(second, Path(tmp) / "b")
            name = "allocation-result.json"
            self.assertEqual((Path(tmp) / "a" / name).read_bytes(),
                             (Path(tmp) / "b" / name).read_bytes())

    # -- XN2-04 ----------------------------------------------------------
    def test_xn2_04_result_traces_inputs(self):
        result = _run(_approved())
        inputs = result["inputs"]
        self.assertTrue(inputs["charges_hash"].startswith("sha256:"))
        self.assertTrue(inputs["driver_hash"].startswith("sha256:"))
        self.assertTrue(inputs["input_fingerprint"].startswith("sha256:"))
        self.assertEqual(result["policy"]["policy_hash"],
                         ap.policy_hash(_approved()))
        other = _run(_approved(), charges={"currency": "USD", "charges": [
            {"charge_id": "c1", "amount": "600", "cost_center": "alpha"}]})
        self.assertNotEqual(inputs["charges_hash"], other["inputs"]["charges_hash"])

    # -- XN2-05 ----------------------------------------------------------
    def test_xn2_05_missing_facts_not_zero(self):
        result = _run(_approved(), charges={"currency": "USD", "charges": [
            {"charge_id": "c1", "amount": "600", "cost_center": "gamma"}]})
        self.assertEqual(result["totals"]["allocated"], "0.0000")
        self.assertEqual(result["unallocated_items"][0]["reason"],
                         "DRIVER_MISSING")
        self.assertIsNone(result["roi"]["value"])
        self.assertEqual(result["delivery_state"], "UNKNOWN")

    # -- XN2-09 ----------------------------------------------------------
    def test_xn2_09_csv_formula_injection_escaped(self):
        charges = {"currency": "USD", "charges": [
            {"charge_id": "=cmd", "amount": "600", "cost_center": "alpha"}]}
        with tempfile.TemporaryDirectory() as tmp:
            paths = ap.write_result(_run(_approved(), charges=charges),
                                    Path(tmp))
            with open(paths["csv"], newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        self.assertEqual(rows[0]["charge_id"], "'=cmd")

    # -- XN2-10 ----------------------------------------------------------
    def test_xn2_10_deterministic_replay(self):
        policy = _approved()
        with tempfile.TemporaryDirectory() as tmp:
            one = ap.write_result(_run(policy), Path(tmp) / "one")
            two = ap.write_result(_run(policy), Path(tmp) / "two")
            for key in ("json", "csv", "summary"):
                self.assertEqual(Path(one[key]).read_bytes(),
                                 Path(two[key]).read_bytes())

    # -- XN2-11 ----------------------------------------------------------
    def test_xn2_11_float_refused(self):
        charges = {"currency": "USD", "charges": [
            {"charge_id": "c1", "amount": 1.5, "cost_center": "alpha"}]}
        with self.assertRaises(ap.AllocationError) as caught:
            _run(_approved(), charges=charges)
        self.assertEqual(caught.exception.code, "FLOAT_REFUSED")

    # -- XN2-12 ----------------------------------------------------------
    def test_xn2_12_rollback_local_only(self):
        policy = _approved()
        before = json.dumps(policy, sort_keys=True)
        result = _run(policy)
        self.assertEqual(json.dumps(policy, sort_keys=True), before)
        self.assertEqual(result["effects"]["signoff"], "none")
        self.assertEqual(result["effects"]["charges_delta"], "none")

    # -- write_result smoke ---------------------------------------------
    def test_write_result_files_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ap.write_result(_run(_approved()), Path(tmp))
            for key in ("json", "csv", "summary"):
                self.assertTrue(Path(paths[key]).is_file(), key)
            summary = Path(paths["summary"]).read_text(encoding="utf-8")
            self.assertIn("恒等式", summary)
            self.assertIn("Delivery：UNKNOWN", summary)
            self.assertIn("残差", summary)


if __name__ == "__main__":
    unittest.main()
