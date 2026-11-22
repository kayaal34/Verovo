from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from buyer_ledger import reproduce

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "replayable-period"


class ReplayablePeriodExampleTest(unittest.TestCase):
    def test_public_fixture_runs_four_step_proof_without_auto_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private"
            public = root / "public"
            built = reproduce.build_bundle(
                [FIXTURE / "events.jsonl"], period="2026-10",
                rule_map_path=FIXTURE / "rule-map.json", out_dir=private,
                data_classification="synthetic")
            self.assertEqual("synthetic", built["manifest"]["sharing"]["data_classification"])
            self.assertEqual(2, built["manifest"]["replay_counts"]["received"])
            published = reproduce.publish_bundle(private, public, approved_by="fixture-owner")
            verification = reproduce.verify_bundle(public)
            self.assertEqual("PASS", verification["verdict"])
            self.assertEqual("verovo.reproduce-template/1", published["template"]["schema"])
            self.assertEqual("confidential", published["template"]["build_command"].split()[-1])
            page = (public / "index.html").read_text(encoding="utf-8")
            for stage in ("Intake", "Recompute", "Review", "Decide"):
                self.assertIn(stage, page)
            self.assertIn("Delivery", page)
            self.assertIn("UNKNOWN", page)
            self.assertIn("UNPROVEN", page)


if __name__ == "__main__":
    unittest.main()
