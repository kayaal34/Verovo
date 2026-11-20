"""Explicit close-difference disposition; review/signoff never invent resolution."""
import unittest

try:
    from .test_close_report import (
        CloseReportTestBase, USAGE, _source,
    )
except ImportError:  # unittest discover -s tests
    from test_close_report import CloseReportTestBase, USAGE, _source

from buyer_ledger import close_actions, close_report
from buyer_ledger.close import CloseError


class CloseDifferenceActionTests(CloseReportTestBase):
    def ready(self):
        self.make_case(
            sources=[_source("bills", "bill-export.csv"),
                     _source("usage", "usage-export.jsonl", "buyer")],
            bills=self.full_bills(amount="25.00"), usage=USAGE)
        close_report.calculate(self.case)
        return close_report.build_report(self.case)

    def test_required_fields_and_unknown_item_fail_before_append(self):
        report = self.ready()
        item = report["open_items"][0]["id"]
        for kwargs in (
            {"owner": "", "by": "finance", "source_ref": "ticket:1"},
            {"owner": "finance", "by": "", "source_ref": "ticket:1"},
            {"owner": "finance", "by": "finance", "source_ref": ""},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(CloseError) as ctx:
                    close_actions.record_action(
                        self.case, open_item_ref=item, action="inquiry", **kwargs)
                self.assertEqual("INPUT_REQUIRED", ctx.exception.code)
        with self.assertRaises(CloseError):
            close_actions.record_action(
                self.case, open_item_ref="D-999", action="inquiry",
                owner="finance", by="finance", source_ref="ticket:1")
        self.assertFalse((self.case / close_actions.ACTION_LOG).exists())

    def test_inquiry_is_idempotent_and_does_not_resolve_or_claim_external_send(self):
        report = self.ready()
        item_id = report["open_items"][0]["id"]
        kwargs = dict(open_item_ref=item_id, action="inquiry", owner="finance",
                      by="test-role", source_ref="ticket:synthetic-1",
                      note="synthetic inquiry receipt",
                      occurred_at="2026-11-18T10:00:00+08:00")
        first = close_actions.record_action(self.case, **kwargs)
        second = close_actions.record_action(self.case, **kwargs)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["action_id"], second["action_id"])
        self.assertEqual(1, len(close_actions.read_actions(self.case)))
        refreshed = self.load_report()
        item = next(row for row in refreshed["open_items"] if row["id"] == item_id)
        self.assertEqual("inquiry_recorded", item["state"])
        self.assertNotEqual("resolved", item["state"])
        self.assertEqual("ticket:synthetic-1",
                         item["difference_action"]["source_ref"])
        self.assertFalse(refreshed["difference_actions"]["external_action_executed"])
        self.assertIn("declared_reference", refreshed["difference_actions"]["evidence_status"])

    def test_only_explicit_resolved_closes_then_later_dispute_reopens(self):
        report = self.ready()
        item_id = report["open_items"][0]["id"]
        with self.assertRaises(CloseError):
            close_actions.record_action(
                self.case, open_item_ref=item_id, action="resolved",
                owner="finance", by="test-role", source_ref="credit:synthetic-1")
        close_actions.record_action(
            self.case, open_item_ref=item_id, action="resolved",
            owner="finance", by="test-role", source_ref="credit:synthetic-1",
            note="synthetic buyer accepted corrected line")
        item = next(row for row in self.load_report()["open_items"]
                    if row["id"] == item_id)
        self.assertEqual("resolved", item["state"])
        close_actions.record_action(
            self.case, open_item_ref=item_id, action="dispute",
            owner="finance", by="test-role", source_ref="dispute:synthetic-2",
            note="supplier rejected the correction")
        item = next(row for row in self.load_report()["open_items"]
                    if row["id"] == item_id)
        self.assertEqual("dispute_open", item["state"])
        self.assertEqual(2, len(item["difference_action_history"]))

    def test_review_does_not_action_or_resolve_anything(self):
        report = self.ready()
        before = {item["id"]: item["state"] for item in report["open_items"]}
        close_report.record_review(
            self.case, reviewer="test-role", approved_scope="synthetic review")
        refreshed = self.load_report()
        self.assertEqual(0, refreshed["difference_actions"]["total"])
        self.assertEqual(before, {item["id"]: item["state"]
                                  for item in refreshed["open_items"]})
        self.assertFalse(refreshed["review"]["payment_authorization"])


if __name__ == "__main__":
    unittest.main()
