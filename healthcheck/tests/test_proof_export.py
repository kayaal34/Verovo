from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "healthcheck"))
CLI = ROOT / "healthcheck/verovo"
FIXTURE = ROOT / "healthcheck/tests/fixtures/canonical-finance"


def tree_hashes(root: Path) -> dict:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


class ProofExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bundle = Path(self.tmp.name) / "period"
        shutil.copytree(FIXTURE, self.bundle)
        self.out = Path(self.tmp.name) / "proof"

    def _run(self, *args):
        return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True)

    def _export(self):
        return self._run("proof", "export", "--bundle", str(self.bundle), "--out", str(self.out),
                         "--by", "proof-reviewer", "--classification", "synthetic")

    def _checks(self):
        return json.loads((self.out / "checks.json").read_text(encoding="utf-8"))["checks"]

    def test_export_preserves_finance_verdicts_at_separate_levels(self):
        before = tree_hashes(self.bundle)
        completed = self._export()
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(before, tree_hashes(self.bundle))
        checks = self._checks()
        by_id = {item["check_id"]: item for item in checks}
        self.assertEqual("PASS", by_id["canonical-artifact"]["status"])
        self.assertEqual("P1", by_id["canonical-artifact"]["level"])
        self.assertEqual("PASS", by_id["rule-repriced:seat-fee"]["status"])
        self.assertEqual("P1", by_id["rule-repriced:seat-fee"]["level"])
        self.assertEqual("INDETERMINATE", by_id["invoice-reconciled:seat-fee"]["status"])
        self.assertEqual("P2", by_id["invoice-reconciled:seat-fee"]["level"])
        self.assertEqual("PASS", by_id["delivery-accepted:buyer-accept-t1"]["status"])
        self.assertEqual("FAIL", by_id["delivery-accepted:buyer-reject-t2"]["status"])
        self.assertEqual("P4", by_id["delivery-accepted:buyer-accept-t1"]["level"])
        self.assertEqual("FAIL", json.loads(completed.stdout)["proof_status"])
        self.assertNotIn("proof_level", json.loads((self.out / "proof.json").read_text(encoding="utf-8")))

    def test_replay_recomputes_same_canonical_output(self):
        self.assertEqual(self._export().returncode, 0)
        completed = self._run("proof", "replay", str(self.out), "--bundle", str(self.bundle))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual("PASS", payload["status"])
        self.assertTrue(payload["output_match"])

    def test_replay_marks_changed_inputs_expired_without_rewriting_proof(self):
        self.assertEqual(self._export().returncode, 0)
        before = tree_hashes(self.out)
        usage = self.bundle / "usage.jsonl"
        usage.write_text(usage.read_text(encoding="utf-8") + "\n")
        completed = self._run("proof", "replay", str(self.out), "--bundle", str(self.bundle))
        self.assertEqual(1, completed.returncode, completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual("EXPIRED", payload["status"])
        self.assertFalse(payload["output_match"])
        self.assertEqual(before, tree_hashes(self.out))

    def test_replay_fingerprint_excludes_only_volatile_clock(self):
        import copy
        from verovo_healthcheck import canonical_finance
        from verovo_healthcheck.proof_export import semantic_fingerprint
        first = canonical_finance.run(self.bundle)
        second = copy.deepcopy(first)
        second["intake_report"]["generated_at"] = "2030-01-01T00:00:00+00:00"
        self.assertEqual(semantic_fingerprint(first), semantic_fingerprint(second))
        second["case"]["usage"][0]["quantity"] = "999999"
        self.assertNotEqual(semantic_fingerprint(first), semantic_fingerprint(second))

    def test_driver_change_marks_old_proof_expired(self):
        self.assertEqual(self._export().returncode, 0)
        drivers = self.bundle / "drivers.csv"
        drivers.write_text(drivers.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        completed = self._run("proof", "replay", str(self.out), "--bundle", str(self.bundle))
        self.assertEqual(1, completed.returncode)
        self.assertEqual("EXPIRED", json.loads(completed.stdout)["status"])

    def test_public_redacted_canonical_export_requires_a_real_projection(self):
        completed = self._run("proof", "export", "--bundle", str(self.bundle), "--out", str(self.out),
                              "--by", "proof-reviewer", "--classification", "public-redacted")
        self.assertEqual(2, completed.returncode)
        self.assertFalse(self.out.exists())

    def test_contract_approved_by_is_actual_reviewer_not_buyer_name(self):
        self.assertEqual(self._export().returncode, 0)
        contract = json.loads((self.out / "contract.json").read_text(encoding="utf-8"))
        self.assertEqual("proof-reviewer", contract["approved_by"])

    def test_tampered_canonical_artifact_cannot_replay_pass(self):
        self.assertEqual(self._export().returncode, 0)
        artifact = self.out / "artifacts/canonical.json"
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        payload["case_id"] = "tampered"
        artifact.write_text(json.dumps(payload), encoding="utf-8")
        completed = self._run("proof", "replay", str(self.out), "--bundle", str(self.bundle))
        self.assertEqual(1, completed.returncode)
        self.assertEqual("INDETERMINATE", json.loads(completed.stdout)["status"])

    def test_p3_waits_for_independent_review(self):
        self.assertEqual(self._export().returncode, 0)
        checks = self._checks()
        by_id = {item["check_id"]: item for item in checks}
        self.assertEqual("P1", by_id["rule-repriced:seat-fee"]["level"])
        self.assertEqual("PASS", by_id["rule-repriced:seat-fee"]["status"])
        self.assertEqual("UNCHECKED", by_id["independent-review:rule-repriced"]["status"])

    def test_existing_output_is_not_overwritten(self):
        self.out.mkdir()
        (self.out / "keep").write_text("keep", encoding="utf-8")
        completed = self._export()
        self.assertEqual(2, completed.returncode)
        self.assertEqual("keep", (self.out / "keep").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
