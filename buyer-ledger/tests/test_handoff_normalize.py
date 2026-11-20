"""Growovo handoff → Period Intake tables: evidence stays evidence."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

from buyer_ledger import growovo_handoff, handoff_evidence

from tests.test_growovo_handoff import fingerprint


def fixture(**overrides):
    payload = {
        "schema": "growovo.verovo-handoff/1",
        "run_id": "r1",
        "operations": [
            {
                "operation_id": "op-ok", "run_id": "r1", "channel": "email",
                "action": "send", "result": "committed",
                "started_at": "2026-11-20T01:00:00+00:00",
                "finished_at": "2026-11-20T01:00:05+00:00",
                "receipt": {"message_id": "synthetic-1"},
            },
            {
                "operation_id": "op-dry", "run_id": "r1", "channel": "x",
                "action": "post", "result": "dry-run",
            },
        ],
        "business_events": [{
            "event_id": "ev-1", "event_type": "activation",
            "campaign_id": "r1", "source_ref": "synthetic:ev-1",
            "amount": "100", "currency": "USD",
        }],
        "event_qualifications": [], "askovo_contexts": [],
        "outcome_projection": {"L3": {"state": "PARTIAL"}},
        "evidence_grade": "synthetic",
        "external_action_executed_by_export": False,
        "booking_status": "evidence_only_not_canonical",
        "boundary": "synthetic test fixture",
    }
    payload.update(overrides)
    payload["integrity_sha256"] = fingerprint(payload)
    payload["generated_at"] = "2026-11-20T12:00:00+00:00"
    return payload


def with_bindings(payload):
    payload = dict(payload)
    bindings = []
    for row in payload.get("operations") or []:
        bindings.append({
            "task_id": "growovo:r1/op/%s" % row["operation_id"],
            "kind": "operation", "operation_id": row["operation_id"],
            "run_id": "r1", "result": row.get("result") or None,
        })
    for row in payload.get("business_events") or []:
        bindings.append({
            "task_id": "growovo:r1/ev/%s" % row["event_id"],
            "kind": "product_event", "event_id": row["event_id"],
            "run_id": "r1",
        })
    payload["task_bindings"] = bindings
    payload["integrity_sha256"] = fingerprint(payload)
    return payload


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "handoff.json"
        self.write(with_bindings(fixture()))

    def write(self, payload):
        self.source.write_text(json.dumps(payload, ensure_ascii=False),
                               encoding="utf-8")

    def test_committed_and_event_normalize_but_dry_run_is_excluded(self):
        out = self.root / "intake"
        result = handoff_evidence.normalize_handoff_file(self.source, out)
        self.assertEqual("DELIVERED_WITH_GAP", result["terminal_state"])
        self.assertEqual("evidence_only_not_canonical", result["booking_status"])
        self.assertFalse(result["receipt_verified_by_verovo"])
        self.assertFalse(result["business_outcome_observed_by_verovo"])
        self.assertEqual(["manifest.json", "outcomes.csv", "runs.jsonl",
                          "tasks.csv"], result["files"])
        self.assertFalse((out / "usage.jsonl").exists())

        tasks = read_csv(out / "tasks.csv")
        self.assertEqual(3, len(tasks))
        by_id = {row["task_id"]: row for row in tasks}
        self.assertEqual("growovo_operation:committed",
                         by_id["growovo:r1/op/op-ok"]["type"])
        self.assertEqual("growovo_operation:dry-run",
                         by_id["growovo:r1/op/op-dry"]["type"])
        self.assertEqual("growovo_product_event",
                         by_id["growovo:r1/ev/ev-1"]["type"])

        runs = [json.loads(line) for line in
                (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(3, len(runs))

        outcomes = read_csv(out / "outcomes.csv")
        self.assertEqual(1, len(outcomes))
        outcome = outcomes[0]
        self.assertEqual("growovo:r1/ev/ev-1", outcome["task_id"])
        self.assertEqual("ev-1", outcome["outcome_id"])
        self.assertEqual("source_declared", outcome["status"])
        self.assertEqual("false", outcome["verified"])
        self.assertEqual("100", outcome["value"])
        self.assertEqual("USD", outcome["currency"])
        self.assertEqual("", outcome["accepted_by"])
        self.assertEqual("", outcome["accepted_at"])
        self.assertEqual("synthetic:ev-1", outcome["source_ref"])

        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("payload", manifest["task_bindings_source"])
        facts = {row["operation_id"]: row for row in manifest["action_facts"]}
        self.assertEqual("action_fact", facts["op-ok"]["counted_as"])
        self.assertTrue(facts["op-ok"]["receipt_present"])
        self.assertEqual("excluded", facts["op-dry"]["counted_as"])
        self.assertEqual(1, manifest["counts"]["committed_operations"])
        self.assertEqual(1, len(manifest["excluded"]))
        self.assertEqual("op-dry", manifest["excluded"][0]["id"])
        self.assertEqual("growovo_operations_carry_no_monetary_cost",
                         manifest["usage_not_derived"]["reason"])

    def test_tampered_handoff_is_refused_without_output(self):
        payload = fixture()
        payload["business_events"][0]["event_type"] = "paid"
        self.write(payload)
        out = self.root / "refused"
        with self.assertRaisesRegex(growovo_handoff.HandoffError,
                                    "integrity mismatch"):
            handoff_evidence.normalize_handoff_file(self.source, out)
        self.assertFalse(out.exists())

    def test_package_without_bindings_derives_ids_and_says_so(self):
        self.write(fixture())
        out = self.root / "derived"
        result = handoff_evidence.normalize_handoff_file(self.source, out)
        self.assertEqual("derived_fallback", result["task_bindings_source"])
        tasks = read_csv(out / "tasks.csv")
        ids = {row["task_id"] for row in tasks}
        self.assertEqual({"growovo:r1/op/op-ok", "growovo:r1/op/op-dry",
                          "growovo:r1/ev/ev-1"}, ids)
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("derived_fallback", manifest["task_bindings_source"])

    def test_nonempty_output_is_never_overwritten(self):
        out = self.root / "existing"
        out.mkdir()
        (out / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(handoff_evidence.HandoffNormalizeError):
            handoff_evidence.normalize_handoff_file(self.source, out)
        self.assertEqual("keep", (out / "keep.txt").read_text(encoding="utf-8"))

    def test_event_claimed_verified_is_not_passed_through(self):
        payload = fixture()
        payload["business_events"][0]["verified"] = True
        payload["verification"] = {"state": "buyer_confirmed"}
        payload["integrity_sha256"] = fingerprint(payload)
        self.write(payload)
        out = self.root / "claimed"
        handoff_evidence.normalize_handoff_file(self.source, out)
        outcomes = read_csv(out / "outcomes.csv")
        self.assertEqual("false", outcomes[0]["verified"])
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["business_outcome_observed_by_verovo"])

    def test_file_headers_are_stable(self):
        out = self.root / "headers"
        handoff_evidence.normalize_handoff_file(self.source, out)
        self.assertEqual(",".join(handoff_evidence.TASKS_HEADER),
                         (out / "tasks.csv").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(",".join(handoff_evidence.OUTCOMES_HEADER),
                         (out / "outcomes.csv").read_text(encoding="utf-8").splitlines()[0])
        for line in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines():
            self.assertIsInstance(json.loads(line), dict)

    def test_explicit_bindings_win_and_duplicates_are_reported(self):
        payload = with_bindings(fixture())
        payload["task_bindings"].append(dict(payload["task_bindings"][0]))
        payload["integrity_sha256"] = fingerprint(payload)
        self.write(payload)
        out = self.root / "bound"
        handoff_evidence.normalize_handoff_file(self.source, out)
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("payload", manifest["task_bindings_source"])
        self.assertTrue(any("repeats task_id" in warning
                            for warning in manifest["warnings"]))
        self.assertEqual(3, len(read_csv(out / "tasks.csv")))


if __name__ == "__main__":
    unittest.main()
