"""Growovo→Verovo handoff: hash integrity without evidence-grade inflation."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from buyer_ledger import growovo_handoff


def fingerprint(payload):
    body = dict(payload)
    body.pop("integrity_sha256", None)
    body.pop("generated_at", None)
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def fixture():
    payload = {
        "schema": "growovo.verovo-handoff/1",
        "run_id": "synthetic-run",
        "operations": [{
            "operation_id": "op-1", "run_id": "synthetic-run",
            "channel": "email", "action": "message", "result": "committed",
            "account": {"handle": "synthetic-sender"},
        }],
        "business_events": [{
            "event_id": "evt-1", "event_type": "activation",
            "campaign_id": "synthetic-run", "source_ref": "synthetic:evt-1",
        }],
        "event_qualifications": [], "askovo_contexts": [],
        "outcome_projection": {"L3": {"state": "PARTIAL"}},
        "evidence_grade": "synthetic",
        "external_action_executed_by_export": False,
        "booking_status": "evidence_only_not_canonical",
        "boundary": "synthetic test fixture",
    }
    payload["integrity_sha256"] = fingerprint(payload)
    payload["generated_at"] = "2026-11-18T12:00:00+00:00"
    return payload


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "handoff.json"
        self.source.write_text(json.dumps(fixture(), ensure_ascii=False), encoding="utf-8")

    def test_valid_handoff_imports_as_unbooked_evidence(self):
        result = growovo_handoff.import_handoff(self.source, self.root / "imported")
        self.assertEqual("DELIVERED_WITH_GAP", result["terminal_state"])
        evidence = json.loads(Path(result["output"]).read_text(encoding="utf-8"))
        self.assertTrue(evidence["source_integrity_verified"])
        self.assertFalse(evidence["receipt_verified_by_verovo"])
        self.assertFalse(evidence["business_outcome_observed_by_verovo"])
        self.assertEqual("evidence_only_not_canonical", evidence["booking_status"])
        self.assertEqual(1, evidence["counts"]["operations"])

    def test_tampering_is_refused_without_output(self):
        payload = fixture()
        payload["business_events"][0]["event_type"] = "paid"
        self.source.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(growovo_handoff.HandoffError, "integrity mismatch"):
            growovo_handoff.import_handoff(self.source, self.root / "refused")
        self.assertFalse((self.root / "refused").exists())

    def test_nonempty_output_is_never_overwritten(self):
        out = self.root / "existing"
        out.mkdir()
        (out / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(growovo_handoff.HandoffError):
            growovo_handoff.import_handoff(self.source, out)
        self.assertEqual("keep", (out / "keep.txt").read_text(encoding="utf-8"))

    def test_credentials_and_empty_handoff_are_rejected(self):
        empty = fixture()
        empty["operations"] = []
        empty["business_events"] = []
        empty["integrity_sha256"] = fingerprint(empty)
        with self.assertRaisesRegex(growovo_handoff.HandoffError, "empty"):
            growovo_handoff.validate_handoff(empty)
        credential = fixture()
        credential["operations"][0]["access_token"] = "must-not-cross"
        credential["integrity_sha256"] = fingerprint(credential)
        with self.assertRaisesRegex(growovo_handoff.HandoffError, "forbidden"):
            growovo_handoff.validate_handoff(credential)


if __name__ == "__main__":
    unittest.main()
