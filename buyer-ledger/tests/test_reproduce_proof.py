from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from buyer_ledger import reproduce as rp
from buyer_ledger import reproduce_proof


class ReproduceProofTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = Path(__file__).resolve().parents[1] / "examples" / "replayable-period"
        self.source = self.root / "source"
        rp.build_bundle([self.fixture / "events.jsonl"], period="2026-10",
                        rule_map_path=self.fixture / "rule-map.json", out_dir=self.source,
                        data_classification="synthetic")

    def test_export_projects_replay_without_claiming_external_acceptance(self):
        out = self.root / "proof"
        result = reproduce_proof.export_proof(self.source, out, by="proof-reviewer")
        self.assertEqual("exported", result["status"])
        proof = json.loads((out / "proof.json").read_text(encoding="utf-8"))
        checks = json.loads((out / "checks.json").read_text(encoding="utf-8"))["checks"]
        by_id = {row["check_id"]: row for row in checks}
        self.assertEqual("PASS", by_id["replay-integrity"]["status"])
        self.assertEqual("UNCHECKED", by_id["external-result"]["status"])
        self.assertEqual("UNAVAILABLE", proof["replay"]["status"])
        self.assertFalse(result["source_mutated"])

    def test_export_refuses_to_write_inside_source_or_overwrite(self):
        with self.assertRaises(reproduce_proof.ProofExportError):
            reproduce_proof.export_proof(self.source, self.source / "proof", by="reviewer")
        out = self.root / "proof"
        out.mkdir()
        with self.assertRaises(reproduce_proof.ProofExportError):
            reproduce_proof.export_proof(self.source, out, by="reviewer")

    def test_projection_excludes_source_paths_and_row_level_subjects(self):
        events = self.root / "events.jsonl"
        rows = []
        for line in (self.fixture / "events.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            row["external_subject_id"] = "PRIVATE-SUBJECT-SENTINEL"
            rows.append(row)
        events.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        private_source = self.root / "valid-private-source"
        rp.build_bundle([events], period="2026-10",
                        rule_map_path=self.fixture / "rule-map.json", out_dir=private_source,
                        data_classification="confidential")
        out = self.root / "proof"
        result = reproduce_proof.export_proof(private_source, out, by="reviewer")
        self.assertEqual("PASS", result["source_verdict"])
        for path in out.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(str(private_source), text)
                self.assertNotIn("PRIVATE-SUBJECT-SENTINEL", text)
        manifest = json.loads((out / "artifacts" / "manifest.json").read_text(encoding="utf-8"))
        replay = json.loads((out / "artifacts" / "usage-replay.json").read_text(encoding="utf-8"))
        self.assertNotIn("verify_command", manifest)
        self.assertNotIn("source", manifest["files"][0])
        self.assertNotIn("lines", replay)
        self.assertEqual("confidential", manifest["sharing"]["data_classification"])

    def test_cli_exports_the_same_projection(self):
        repo = Path(__file__).resolve().parents[1]
        out = self.root / "proof"
        run = subprocess.run([
            sys.executable, "-m", "buyer_ledger", "reproduce", "proof", str(self.source),
            "--out", str(out), "--by", "reviewer", "--json",
        ], cwd=repo, capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual("exported", json.loads(run.stdout)["status"])
        self.assertTrue((out / "bundle.json").is_file())


if __name__ == "__main__":
    unittest.main()
