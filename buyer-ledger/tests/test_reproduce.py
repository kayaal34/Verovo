"""第三方可复核的重放验收包：逐字节哈希、独立重跑与身份映射缺项。

Fixture 全部合成，不联网、不调用供应商后台、不生成 invoice。
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from buyer_ledger import reproduce as rp

PERIOD = "2026-10"


def _rule_map():
    return {
        "schema": "verovo.usage-rule-map/1",
        "rule_map_version": "rm-1",
        "approved_by": "buyer-ops",
        "approved_at": "2026-11-01T00:00:00Z",
        "rules": [
            {"rule_id": "r-count", "metric_code": "api_call",
             "aggregate": "count", "unit": "call", "currency": "USD",
             "unit_price": "0.01", "price_version": "p-1",
             "valid_from": "2026-10-01"},
        ],
    }


def _event(tid, **props):
    event = {
        "transaction_id": tid,
        "external_subject_id": "acct-1",
        "metric_code": "api_call",
        "occurred_at": "2026-10-05T10:00:00+00:00",
        "ingested_at": "2026-11-02T00:00:00+00:00",
        "source_ref": "usage.jsonl",
        "source_hash": "sha256:aaa",
    }
    if props:
        event["properties"] = props
    return event


class ReproduceBundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.events_path = self.root / "events.jsonl"
        self.rule_path = self.root / "rule-map.json"
        self.out = self.root / "bundle"
        self.rule_path.write_text(json.dumps(_rule_map()), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_events(self, events):
        self.events_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8")

    def _build(self, **kwargs):
        return rp.build_bundle([self.events_path], period=PERIOD,
                               rule_map_path=self.rule_path, out_dir=self.out,
                               **kwargs)

    def test_rp_01_build_then_verify_passes(self):
        self._write_events([_event("t1", task_id="task-1", attempt_id="a-1",
                                   vendor="acme", vendor_record_id="v-1"),
                            _event("t2", task_id="task-1", attempt_id="a-1",
                                   vendor="acme", vendor_record_id="v-2")])
        result = self._build()
        self.assertTrue(result["ok"])
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], rp.SCHEMA)
        for name in ("usage-replay.json", "usage-replay-lines.csv",
                     "replay-diff.md", rp.IDENTITY_NAME, rp.DISCREPANCIES_NAME,
                     rp.README_NAME):
            self.assertTrue((self.out / name).is_file(), name)
        verdict = rp.verify_bundle(self.out)
        self.assertEqual(verdict["verdict"], "PASS",
                         [c for c in verdict["checks"] if not c["ok"]])

    def test_rp_02_one_byte_change_fails_hash_check(self):
        self._write_events([_event("t1", task_id="task-1", attempt_id="a-1",
                                   vendor="acme")])
        self._build()
        target = self.out / "inputs" / "01-events.jsonl"
        target.write_text(target.read_text(encoding="utf-8") + " ",
                          encoding="utf-8")
        verdict = rp.verify_bundle(self.out)
        self.assertEqual(verdict["verdict"], "FAIL")
        failed = {check["name"] for check in verdict["checks"] if not check["ok"]}
        self.assertIn("file:inputs/01-events.jsonl", failed)

    def test_rp_03_missing_manifest_field_is_unproven(self):
        self._write_events([_event("t1", task_id="task-1", attempt_id="a-1",
                                   vendor="acme")])
        self._build()
        manifest_path = self.out / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("input_fingerprint")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        verdict = rp.verify_bundle(self.out)
        self.assertEqual(verdict["verdict"], "UNPROVEN")
        names = {check["name"] for check in verdict["checks"] if not check["ok"]}
        self.assertIn("manifest.fields", names)

    def test_rp_04_no_properties_stays_unmatched(self):
        self._write_events([_event("t1"), _event("t2")])
        self._build()
        identity = json.loads((self.out / rp.IDENTITY_NAME).read_text(encoding="utf-8"))
        self.assertEqual(identity["coverage"]["complete_for_attempt"], 0)
        self.assertEqual(identity["unmatched_count"], 2)
        missing = identity["unmatched"][0]["missing"]
        self.assertEqual(missing, ["task_id", "attempt_id", "vendor"])
        self.assertEqual(rp.verify_bundle(self.out)["verdict"], "PASS")

    def test_rp_05_retry_of_outside_bundle_is_dangling(self):
        self._write_events([_event("t2", task_id="task-1", attempt_id="a-2",
                                   vendor="acme", retry_of="t1")])
        self._build()
        identity = json.loads((self.out / rp.IDENTITY_NAME).read_text(encoding="utf-8"))
        self.assertEqual(identity["dangling_count"], 1)
        self.assertEqual(identity["dangling_refs"][0]["points_to"], "t1")
        self.assertEqual(rp.verify_bundle(self.out)["verdict"], "PASS")

    def test_rp_06_identity_map_fills_gaps(self):
        self._write_events([_event("t1")])
        identity_map = self.root / "identity.json"
        identity_map.write_text(json.dumps({
            "t1": {"task_id": "task-9", "attempt_id": "a-9", "vendor": "acme"}}),
            encoding="utf-8")
        self._build(identity_map_path=identity_map)
        identity = json.loads((self.out / rp.IDENTITY_NAME).read_text(encoding="utf-8"))
        record = identity["records"][0]
        self.assertEqual(record["task_id"], "task-9")
        self.assertEqual(record["sources"]["task_id"], "identity_map")
        self.assertTrue(record["complete_for_attempt"])
        self.assertEqual(rp.verify_bundle(self.out)["verdict"], "PASS")

    def test_rp_07_non_empty_out_dir_requires_overwrite(self):
        self._write_events([_event("t1")])
        self._build()
        with self.assertRaises(rp.ReproduceError):
            self._build()

    def test_rp_08_cli_build_and_verify(self):
        self._write_events([_event("t1", task_id="task-1", attempt_id="a-1",
                                   vendor="acme")])
        repo = Path(__file__).resolve().parents[1]
        build = subprocess.run(
            [sys.executable, "-m", "buyer_ledger", "reproduce", "build",
             "--input", str(self.events_path), "--period", PERIOD,
             "--rule-map", str(self.rule_path), "--out", str(self.out),
             "--json"],
            cwd=repo, capture_output=True, text=True)
        self.assertEqual(build.returncode, 0, build.stderr)
        self.assertEqual(json.loads(build.stdout)["ok"], True)
        verify = subprocess.run(
            [sys.executable, "-m", "buyer_ledger", "reproduce", "verify",
             str(self.out), "--json"],
            cwd=repo, capture_output=True, text=True)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertEqual(json.loads(verify.stdout)["verdict"], "PASS")
    def test_rp_09_confidential_bundle_cannot_be_published(self):
        self._write_events([_event("t1", task_id="task-1", attempt_id="a-1",
                                   vendor="acme")])
        self._build()
        with self.assertRaises(rp.ReproduceError):
            rp.publish_bundle(self.out, self.root / "public", approved_by="roy")

    def test_rp_10_synthetic_public_copy_verifies_without_source_paths(self):
        self._write_events([_event("t1", task_id="task-1", attempt_id="a-1",
                                   vendor="acme")])
        self._build(data_classification="synthetic")
        result = rp.publish_bundle(self.out, self.root / "public", approved_by="roy")
        self.assertEqual("PASS", rp.verify_bundle(self.root / "public")["verdict"])
        self.assertNotIn(str(self.root), json.dumps(result["manifest"]))
        self.assertEqual("verovo.reproduce-template/1", result["template"]["schema"])
        self.assertTrue((self.root / "public" / "index.html").is_file())

    def test_rp_11_public_redacted_requires_named_approval(self):
        self._write_events([_event("t1", task_id="task-1", attempt_id="a-1",
                                   vendor="acme")])
        with self.assertRaises(rp.ReproduceError):
            self._build(data_classification="public-redacted")
        approved = self._build(data_classification="public-redacted",
                               sharing_approved_by="privacy-owner")
        self.assertEqual("privacy-owner",
                         approved["manifest"]["sharing"]["approved_by"])
        with self.assertRaises(rp.ReproduceError):
            rp.publish_bundle(self.out, self.root / "public", approved_by="someone-else")
    def test_unregistered_or_residual_private_file_cannot_publish(self):
        self._write_events([_event("t1", task_id="task-1", attempt_id="a-1", vendor="acme")])
        self._build(data_classification="synthetic")
        (self.out / "private-note.txt").write_text("PRIVATE-SENTINEL", encoding="utf-8")
        verdict = rp.verify_bundle(self.out)
        self.assertNotEqual("PASS", verdict["verdict"])
        with self.assertRaises(rp.ReproduceError):
            rp.publish_bundle(self.out, self.root / "public", approved_by="roy")

    def test_symlinked_input_is_rejected(self):
        self._write_events([_event("t1", task_id="task-1", attempt_id="a-1", vendor="acme")])
        self._build(data_classification="synthetic")
        target = self.out / "inputs" / "01-events.jsonl"
        external = self.root / "outside.jsonl"
        shutil.copyfile(target, external)
        target.unlink()
        target.symlink_to(external)
        self.assertNotEqual("PASS", rp.verify_bundle(self.out)["verdict"])

    def test_generator_version_is_mandatory(self):
        self._write_events([_event("t1", task_id="task-1", attempt_id="a-1", vendor="acme")])
        self._build()
        path = self.out / rp.MANIFEST_NAME
        manifest = json.loads(path.read_text(encoding="utf-8"))
        del manifest["generator_version"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual("UNPROVEN", rp.verify_bundle(self.out)["verdict"])

    def test_unapproved_rule_map_does_not_create_charge_pass(self):
        self._write_events([_event("t1", task_id="task-1", attempt_id="a-1", vendor="acme")])
        rule = _rule_map()
        rule["approved_by"] = ""
        self.rule_path.write_text(json.dumps(rule), encoding="utf-8")
        result = self._build()
        self.assertNotEqual("PASS", result["manifest"]["charge_verdict"])
        self.assertEqual("PASS", result["manifest"]["replay_integrity"])


if __name__ == "__main__":
    unittest.main()
