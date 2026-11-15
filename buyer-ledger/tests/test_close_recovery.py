"""V-06 acceptance tests: interrupted close revisions and signoffs recover.

The V-04 flow renamed the fully built staging directory into
``revisions/rN/`` and only afterwards wrote ``revision.json`` / ``delta.json``
and updated ``revisions/index.json``.  A crash in that window left a promoted
directory the index did not know about, so the next ``close revise`` refused
with ``REVIEW_REQUIRED`` and a human had to repair the case by hand.  The
signoff path had the same window between ``signoff.json``, ``revision.json``
and the index entry.

These tests freeze the recovery contract:

- a write-ahead journal is durable before the multi-file commit starts;
- every close entry point either completes the interrupted transaction and says
  so, or refuses with the completed/pending steps listed;
- ``close recover`` is a dry run by default, with ``--apply`` / ``--rollback``;
- a promoted-but-unindexed directory is restored instead of becoming a dead
  end;
- signoff re-verifies the report fingerprint, canonical snapshot, source file
  hashes and the signoff/revision/index triple before it writes anything.

The crash is injected through the explicit ``close_revision._CRASH_HOOK`` seam
(``_checkpoint(stage, info)``), never through timing.

Frozen red before implementation.
"""
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
HEALTHCHECK_ROOT = REPO_ROOT / "healthcheck"
if str(HEALTHCHECK_ROOT) not in sys.path:
    sys.path.insert(0, str(HEALTHCHECK_ROOT))

from buyer_ledger import close_revision
from buyer_ledger.close import CloseError
from tests.test_close_revision import RevisionTestBase


class InjectedCrash(RuntimeError):
    """Raised by the test crash hook; not a CloseError, so it is not retried."""


def _crash_hook(stage):
    def hook(current, info):
        if current == stage:
            raise InjectedCrash("crash at %s" % current)
    return hook


def _report_fingerprint(report):
    """Same projection ``close_report.build_report`` fingerprints."""
    body = dict(report)
    body.pop("generated_at", None)
    body["replay"] = dict(body.get("replay") or {})
    body["replay"].pop("output_snapshot_fingerprint", None)
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class RecoveryTestBase(RevisionTestBase):
    def crash(self, stage):
        return mock.patch.object(close_revision, "_CRASH_HOOK",
                                 _crash_hook(stage), create=True)

    def index(self):
        return json.loads(
            (self.case / "revisions" / "index.json").read_text(encoding="utf-8"))

    def revision_ids(self):
        return [item["revision_id"] for item in self.index()["revisions"]]

    def journal_path(self, revision_id):
        return self.case / "revisions" / (".journal-%s.json" % revision_id)

    def journal(self, revision_id):
        return self.read_json(self.journal_path(revision_id))

    def read_json(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def revision_record(self, revision_id="r2"):
        return self.read_json(self.case / "revisions" / revision_id
                              / "revision.json")

    def recover(self, *extra):
        return self.run_cli(["close", "recover", str(self.case)] + list(extra))

    def assert_report_fingerprint(self, revision_id="r2"):
        report = self.revision_report(revision_id)
        self.assertEqual(report["replay"]["output_snapshot_fingerprint"],
                         _report_fingerprint(report))

    def _crash_after_promote(self, amount="25.00"):
        self.base_case()
        revised = self.revised_bundle(self.full_bills(amount=amount),
                                      name="v2")
        with self.crash("revise:promoted"):
            with self.assertRaises(InjectedCrash):
                close_revision.revise(self.case, bundle=revised,
                                      reviewer="finance")
        return revised

    def _journal_hashes(self, revision_id="r2"):
        """Every {path, sha256} pair the journal promises to materialize."""
        journal = self.journal(revision_id)
        pairs = list(journal.get("tree") or [])
        pairs.extend({"path": item.get("path"), "sha256": item.get("sha256")}
                     for item in journal.get("writes") or [])
        return pairs


class RevisionCrashRecoveryTests(RecoveryTestBase):
    """The promote-then-index window must be recoverable, not a dead end."""

    def test_journal_is_durable_before_the_commit_with_expected_hashes(self):
        self._crash_after_promote()
        journal = self.journal("r2")
        self.assertEqual("verovo.period-revision-journal/1",
                         journal["schema"])
        self.assertEqual("revise", journal["kind"])
        self.assertEqual("r2", journal["revision_id"])
        self.assertEqual("r1", journal["parent_revision_id"])
        self.assertEqual(64, len(journal["input_snapshot_hash"]))
        self.assertEqual(["promote", "record", "delta", "index"],
                         journal["steps"])

        pairs = self._journal_hashes()
        paths = {item["path"] for item in pairs}
        self.assertIn("revisions/r2/report/report.json", paths)
        self.assertIn("revisions/r2/revision.json", paths)
        self.assertIn("revisions/r2/delta.json", paths)
        self.assertIn("revisions/index.json", paths)
        for item in pairs:
            self.assertEqual(64, len(item["sha256"]), item)
        self.assertEqual("r1", journal["writes"][-1]["before"]["latest"])

    def test_crash_after_promote_is_detected_and_completed_by_status(self):
        self._crash_after_promote()
        journal = self.journal_path("r2")
        self.assertTrue(journal.is_file(), "the revise journal must outlive a "
                        "crash between rename() and index.json")
        self.assertNotIn("r2", self.revision_ids())
        self.assertTrue((self.case / "revisions" / "r2" / "report" /
                         "report.json").is_file())

        code, out = self.run_cli(["close", "status", str(self.case)])
        self.assertEqual(0, code, out)
        status = json.loads(out)
        self.assertIn("recovery", status)
        self.assertEqual("recovered", status["recovery"]["state"])
        self.assertIn("已恢复上一次中断的修订", status["recovery"]["message"])
        self.assertIn("r2", self.revision_ids())
        self.assertEqual("r2", self.index()["latest"])
        self.assertFalse(journal.exists(), "a completed journal must be removed")
        self.assert_report_fingerprint()
        delta = self.read_json(self.case / "revisions" / "r2" / "delta.json")
        self.assertEqual("5.00", delta["amounts"]["invoice_total"]["delta"])

        code, out = self.run_cli(["close", "status", str(self.case),
                                  "--revision", "r2"])
        self.assertEqual(0, code, out)
        self.assertEqual("needs_review",
                         json.loads(out)["c01_status"]["task_state"])

    def test_close_show_entry_also_completes_the_journal(self):
        self._crash_after_promote()
        code, out = self.run_cli(["close", "show", str(self.case)])
        self.assertEqual(0, code, out)
        status = json.loads(out)
        self.assertEqual("recovered", status["recovery"]["state"])
        self.assertIn("r2", self.revision_ids())
        self.assertFalse(self.journal_path("r2").exists())

    def test_close_report_entry_recovers_before_refusing_the_immutable_root(self):
        self._crash_after_promote()
        code, out = self.run_cli(["close", "report", str(self.case)])
        self.assertEqual(2, code, out)
        self.assertIn("r2", self.revision_ids())
        self.assertEqual("r2", self.index()["latest"])
        self.assertFalse(self.journal_path("r2").exists())

    def test_close_recover_is_dry_run_then_apply_completes(self):
        self._crash_after_promote()
        index_path = self.case / "revisions" / "index.json"
        before = index_path.read_bytes()
        code, out = self.recover()
        self.assertEqual(0, code, out)
        self.assertIn("r2", out)
        self.assertIn("--apply", out)
        self.assertEqual(before, index_path.read_bytes(),
                         "the default close recover must not write")
        self.assertTrue(self.journal_path("r2").is_file())

        code, out = self.recover("--apply")
        self.assertEqual(0, code, out)
        self.assertIn("applied", out.lower())
        self.assertIn("r2", self.revision_ids())
        self.assertEqual("r2", self.index()["latest"])
        self.assertFalse(self.journal_path("r2").exists())
        self.assert_report_fingerprint()
        delta = self.read_json(self.case / "revisions" / "r2" / "delta.json")
        self.assertEqual("5.00", delta["amounts"]["invoice_total"]["delta"])
        record = self.revision_record("r2")
        self.assertEqual("r1", record["parent_revision_id"])
        self.assertTrue(record["input_snapshot_hash"])

        again = index_path.read_bytes()
        code, out = self.recover("--apply")
        self.assertEqual(0, code, out)
        self.assertEqual(again, index_path.read_bytes())
        self.assertEqual(["r1", "r2"], self.revision_ids())

    def test_close_recover_rollback_leaves_no_orphan_directory(self):
        revised = self._crash_after_promote()
        code, out = self.recover("--rollback")
        self.assertEqual(0, code, out)
        self.assertIn("rolled back", out.lower())
        self.assertFalse((self.case / "revisions" / "r2").exists())
        self.assertFalse(self.journal_path("r2").exists())
        self.assertEqual(["r1"], self.revision_ids())
        self.assertEqual("r1", self.index()["latest"])

        # The directory is gone, so the same revise can run again normally.
        result = close_revision.revise(self.case, bundle=revised,
                                       reviewer="finance")
        self.assertFalse(result["idempotent"])
        self.assertEqual("r2", result["revision"]["revision_id"])
        self.assertEqual(["r1", "r2"], self.revision_ids())
        self.assert_report_fingerprint()

    def test_revise_entry_completes_the_journal_instead_of_dead_ending(self):
        revised = self._crash_after_promote()
        result = close_revision.revise(self.case, bundle=revised,
                                       reviewer="finance")
        self.assertTrue(result["idempotent"])
        self.assertEqual("r2", result["revision"]["revision_id"])
        self.assertEqual(["r1", "r2"], self.revision_ids())
        self.assertFalse(self.journal_path("r2").exists())
        self.assertFalse((self.case / "revisions" / "r3").exists())

    def test_same_input_after_recovery_stays_idempotent(self):
        revised = self._crash_after_promote()
        code, out = self.recover("--apply")
        self.assertEqual(0, code, out)
        first = close_revision.revise(self.case, bundle=revised,
                                      reviewer="finance")
        second = close_revision.revise(self.case, bundle=revised,
                                       reviewer="finance")
        self.assertTrue(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(["r1", "r2"], self.revision_ids())
        self.assertFalse((self.case / "revisions" / "r3").exists())

    def test_unindexed_revision_directory_is_adopted_without_a_journal(self):
        # Exactly the pre-V-06 crash state: r2 exists on disk, the index only
        # knows r1.  The old flow refused here forever.
        self.base_case()
        revised = self.revised_bundle(self.full_bills(amount="25.00"),
                                      name="v2")
        result = close_revision.revise(self.case, bundle=revised,
                                       reviewer="finance")
        self.assertEqual("r2", result["revision"]["revision_id"])
        index = self.index()
        index["revisions"] = [item for item in index["revisions"]
                              if item["revision_id"] == "r1"]
        index["latest"] = "r1"
        (self.case / "revisions" / "index.json").write_text(
            json.dumps(index, indent=2) + "\n", encoding="utf-8")
        self.assertFalse(self.journal_path("r2").exists())

        recovered = close_revision.revise(self.case, bundle=revised,
                                          reviewer="finance")
        self.assertTrue(recovered["idempotent"])
        self.assertEqual("r2", recovered["revision"]["revision_id"])
        self.assertEqual(["r1", "r2"], self.revision_ids())
        self.assertFalse((self.case / "revisions" / "r3").exists())

    def test_corrupted_promoted_revision_is_reported_not_ignored(self):
        self._crash_after_promote()
        report_path = self.case / "revisions" / "r2" / "report" / "report.json"
        report = self.read_json(report_path)
        report["amounts"]["open_difference"] = "999.00"
        report_path.write_text(json.dumps(report, indent=2) + "\n",
                               encoding="utf-8")

        code, out = self.run_cli(["close", "status", str(self.case)])
        self.assertEqual(0, code, out)
        status = json.loads(out)
        self.assertIn("recovery", status)
        self.assertEqual("conflict", status["recovery"]["state"])
        self.assertIn("需人工核对", status["recovery"]["message"])
        self.assertNotIn("r2", self.revision_ids())
        self.assertTrue(self.journal_path("r2").is_file())

        with self.assertRaises(SystemExit) as ctx:
            self.recover("--apply")
        self.assertEqual(2, ctx.exception.code)
        self.assertNotIn("r2", self.revision_ids())

        code, out = self.recover("--rollback")
        self.assertEqual(0, code, out)
        self.assertFalse((self.case / "revisions" / "r2").exists())
        self.assertFalse(self.journal_path("r2").exists())

    def test_corrupted_journal_is_reported_and_rollback_clears_it(self):
        self.base_case()
        journal = self.journal_path("r2")
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text("{not json", encoding="utf-8")

        code, out = self.run_cli(["close", "status", str(self.case)])
        self.assertEqual(0, code, out)
        recovery = json.loads(out)["recovery"]
        self.assertEqual("conflict", recovery["state"])
        self.assertIn("损坏", recovery["message"])

        with self.assertRaises(SystemExit) as ctx:
            self.recover("--apply")
        self.assertEqual(2, ctx.exception.code)

        code, out = self.recover("--rollback")
        self.assertEqual(0, code, out)
        self.assertFalse(journal.exists())


class SnapshotCrashRecoveryTests(RecoveryTestBase):
    """The root snapshot (revisions/r1 + index) is journalled too."""

    def test_crash_between_root_snapshot_and_index_is_recovered(self):
        self.base_case()
        revised = self.revised_bundle(self.full_bills(amount="25.00"),
                                      name="v2")
        with self.crash("snapshot:built"):
            with self.assertRaises(InjectedCrash):
                close_revision.revise(self.case, bundle=revised,
                                      reviewer="finance")
        self.assertTrue((self.case / "revisions" / "r1" / "revision.json")
                        .is_file())
        self.assertTrue(self.journal_path("r1").is_file())
        index_path = self.case / "revisions" / "index.json"
        self.assertFalse(index_path.is_file())

        code, out = self.recover("--apply")
        self.assertEqual(0, code, out)
        self.assertEqual(["r1"], self.revision_ids())
        self.assertFalse(self.journal_path("r1").exists())
        self.assertTrue((self.case / "revisions" / "r1" / "revision.json")
                        .is_file())

        result = close_revision.revise(self.case, bundle=revised,
                                       reviewer="finance")
        self.assertFalse(result["idempotent"])
        self.assertEqual("r2", result["revision"]["revision_id"])


class SignoffCrashRecoveryTests(RecoveryTestBase):
    """signoff.json + revision.json + index.json must recover to one state."""

    def _signed_crash(self, stage):
        self.base_case()
        revised = self.revised_bundle(self.full_bills(amount="25.00"),
                                      name="v2")
        close_revision.revise(self.case, bundle=revised, reviewer="finance")
        with self.crash(stage):
            with self.assertRaises(InjectedCrash):
                close_revision.signoff(self.case, reviewer="controller",
                                       approved_scope="accept v2",
                                       revision_id="r2")

    def _assert_three_consistent(self):
        signoff = self.read_json(self.case / "revisions" / "r2" /
                                 "signoff.json")
        revision = self.revision_record("r2")
        entry = [item for item in self.index()["revisions"]
                 if item["revision_id"] == "r2"][0]
        self.assertEqual(signoff["state"], revision["status"])
        self.assertEqual(signoff["state"], entry["status"])
        self.assertEqual(signoff["decided_at"], revision["closed_at"])
        self.assertEqual(signoff["decided_at"], entry["closed_at"])
        self.assertEqual("controller", entry["reviewer"])
        self.assertEqual(signoff, revision["signoff"])
        self.assertFalse(signoff["payment_authorization"])

    def _assert_recovery(self, stage, record_signed_before):
        self._signed_crash(stage)
        self.assertTrue((self.case / "revisions" / "r2" / "signoff.json")
                        .is_file())
        self.assertTrue(self.journal_path("r2").is_file())
        record = self.revision_record("r2")
        self.assertEqual(record_signed_before,
                         record["status"].startswith("signed"))
        code, out = self.recover()
        self.assertEqual(0, code, out)
        self.assertIn("apply", out)
        code, out = self.recover("--apply")
        self.assertEqual(0, code, out)
        self.assertFalse(self.journal_path("r2").exists())
        self._assert_three_consistent()
        code, out = self.run_cli(["close", "status", str(self.case),
                                  "--revision", "r2"])
        self.assertEqual(0, code, out)
        self.assertEqual("delivered",
                         json.loads(out)["c01_status"]["task_state"])

    def test_crash_after_signoff_file_recovers_to_one_state(self):
        # signoff.json exists, revision/index still unsigned: the exact
        # "signoff file present, index says not signed" window.
        self._assert_recovery("signoff:signoff-written", False)
        entry = [item for item in self.index()["revisions"]
                 if item["revision_id"] == "r2"][0]
        self.assertEqual("signed", entry["status"])

    def test_crash_after_revision_file_recovers_to_one_state(self):
        # revision.json already signed, index still unsigned.
        self._signed_crash("signoff:revision-written")
        self.assertTrue(self.revision_record("r2")["status"].startswith("signed"))
        entry = [item for item in self.index()["revisions"]
                 if item["revision_id"] == "r2"][0]
        self.assertEqual("report_ready", entry["status"],
                         "the index must still lag behind the revision file")
        code, out = self.recover("--apply")
        self.assertEqual(0, code, out)
        self._assert_three_consistent()
        self.assertFalse(self.journal_path("r2").exists())

    def test_signoff_rollback_restores_the_unsigned_triple(self):
        self._signed_crash("signoff:signoff-written")
        code, out = self.recover("--rollback")
        self.assertEqual(0, code, out)
        self.assertIn("rolled back", out.lower())
        self.assertFalse((self.case / "revisions" / "r2" / "signoff.json")
                         .exists())
        self.assertEqual("report_ready", self.revision_record("r2")["status"])
        entry = [item for item in self.index()["revisions"]
                 if item["revision_id"] == "r2"][0]
        self.assertEqual("report_ready", entry["status"])
        self.assertFalse(self.journal_path("r2").exists())


class SignoffFreshnessTests(RecoveryTestBase):
    """Signoff must re-verify fingerprints and source hashes, not just "a report"."""

    def _signed_root(self):
        self.base_case()
        return close_revision.signoff(self.case, reviewer="controller",
                                      approved_scope="accept r1")

    def _signoff_again(self):
        return close_revision.signoff(self.case, reviewer="controller",
                                      approved_scope="accept r1")

    def _assert_stale(self, action):
        with self.assertRaises(CloseError) as ctx:
            action()
        self.assertEqual("STALE_SOURCE", ctx.exception.code,
                         str(ctx.exception))
        self.assertIn("revise", str(ctx.exception))

    def test_changed_canonical_after_signing_is_stale(self):
        self._signed_root()
        canonical = self.case / "canonical" / "bills.csv"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\n",
                            encoding="utf-8")
        self._assert_stale(self._signoff_again)

    def test_changed_source_file_after_signing_is_stale(self):
        self._signed_root()
        source = self.bundle / "bill-export.csv"
        source.write_text(source.read_text(encoding="utf-8") + "\n",
                         encoding="utf-8")
        self._assert_stale(self._signoff_again)

    def test_tampered_report_is_refused_before_signing(self):
        self.base_case()
        report_path = self.case / "report" / "report.json"
        report = self.read_json(report_path)
        report["amounts"]["open_difference"] = "1.00"
        report_path.write_text(json.dumps(report, indent=2) + "\n",
                               encoding="utf-8")
        with self.assertRaises(CloseError) as ctx:
            close_revision.signoff(self.case, reviewer="controller")
        self.assertEqual("STALE_SOURCE", ctx.exception.code)
        self.assertIn("report", str(ctx.exception))
        self.assertFalse((self.case / "signoff.json").exists())
        self.assertFalse(self.journal_path("r1").exists())

    def test_clean_signoff_records_the_verified_fingerprints(self):
        signoff = self._signed_root()
        calculation = self.read_json(self.case / "calculation.json")
        report = self.read_json(self.case / "report" / "report.json")
        self.assertEqual(calculation["calculation_fingerprint"],
                         signoff["calculation_fingerprint"])
        self.assertEqual(calculation["canonical_snapshot"]["fingerprint"],
                         signoff["canonical_snapshot_fingerprint"])
        self.assertEqual(report["replay"]["output_snapshot_fingerprint"],
                         signoff["report_fingerprint"])
        self.assertEqual(calculation["input_hashes"],
                         signoff["input_hashes"])


if __name__ == "__main__":
    unittest.main()
