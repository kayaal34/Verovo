"""C-01-Lite 合同：close status 必须给出最小委托投影。

契约：ai输出/ovo-C01-Lite-最小委托契约-v1-20261115.md
"""
import json
import unittest
from pathlib import Path

from buyer_ledger import cli
from buyer_ledger import close as close_mod

from .test_close_period_import import BILLS_CSV, RULE_MAP, USAGE_JSONL, _job
import tempfile

STATES = {"NEEDS_INPUT", "READY", "NEEDS_REVIEW", "BLOCKED", "PARTIAL", "DELIVERED"}
CODES = {"INPUT_REQUIRED", "AUTH_REQUIRED", "REVIEW_REQUIRED", "UNSUPPORTED_SCOPE",
         "POLICY_DENIED", "RATE_LIMITED", "STALE_SOURCE",
         "INDETERMINATE_EXTERNAL_ACTION", "ARTIFACT_PARTIAL", "INTERNAL_ERROR"}


class DelegationContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.case = base / "case"
        self.bundle = base / "bundle"
        self.bundle.mkdir(parents=True)
        (self.bundle / "bill-export.csv").write_text(BILLS_CSV, encoding="utf-8")
        (self.bundle / "usage-export.jsonl").write_text(USAGE_JSONL, encoding="utf-8")
        (self.bundle / "rules.json").write_text(json.dumps(RULE_MAP), encoding="utf-8")
        (self.bundle / "period-job.json").write_text(json.dumps(_job()),
                                                     encoding="utf-8")
        close_mod.init_case(self.case, job_path=self.bundle / "period-job.json")

    def _status(self):
        return close_mod.build_case(self.case)

    def test_delegation_block_is_complete(self):
        block = self._status()["delegation"]
        for key in ("task_id", "task_state", "next_actor", "next_required_action",
                    "blocked_reason_code", "result_ref", "authorization_scope",
                    "recovery_entry", "delegation_boundary"):
            self.assertIn(key, block, f"{key} missing")
        self.assertIn(block["task_state"], STATES)
        self.assertIn(block["blocked_reason_code"], CODES | {None})
        self.assertTrue(block["next_required_action"] or block["result_ref"])
        self.assertTrue(block["delegation_boundary"])

    def test_empty_case_needs_input_and_a_human(self):
        block = self._status()["delegation"]
        self.assertEqual("NEEDS_INPUT", block["task_state"])
        self.assertIn("buyer", block["next_actor"])
        self.assertEqual("INPUT_REQUIRED", block["blocked_reason_code"])
        self.assertIsNone(block["result_ref"])

    def test_scope_reports_currency_and_period_not_invented(self):
        block = self._status()["delegation"]
        self.assertEqual("USD", block["authorization_scope"]["currency"])
        self.assertEqual("2026-10-01", block["authorization_scope"]["period"]["from"])
        self.assertEqual("acct-1", block["authorization_scope"]["account"]["account"])

    def test_status_markdown_is_human_readable_and_keeps_boundary(self):
        text = cli._close_status_markdown(self._status())
        self.assertIn("# 月结状态", text)
        self.assertIn("完成轴", text)
        self.assertIn("缺失输入", text)
        self.assertIn("边界", text)


if __name__ == "__main__":
    unittest.main()
