"""V-09: OCR 候选入账的事务完整性。

冻结 2026-11-18 复审 §2 的四条失败路径：
- 缺失 usage/rule-map 时先校验、后写盘，失败不留半份账期包；
- CSV/复制/落盘中任意一步失败都不留对外可用的半份产物；
- 进程中断后能明确回滚或修复，重跑不重复导出；
- 两个进程同时导入/入账时不会分配到同一个候选 id，也不会双写同一账期。
"""
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from buyer_ledger import bill_candidate as bc
from buyer_ledger.close import CloseError

from tests.test_bill_candidate import (
    INVOICE_TEXT,
    BillCandidateTestBase,
    RULE_MAP,
    USAGE_JSONL,
)

REVIEWER = "finance-reviewer"


def _import_worker(store: str, text: str, queue) -> None:
    try:
        candidate = bc.add_candidate(Path(store), engine="text",
                                     pasted_text=text)["candidate"]
        queue.put(("ok", candidate["candidate_id"]))
    except Exception as exc:  # noqa: BLE001 cross-process result channel
        queue.put(("error", type(exc).__name__, str(exc)))


def _book_worker(store: str, candidate_id: str, into: str, queue) -> None:
    try:
        booking = bc.book(Path(store), candidate_id, reviewer=REVIEWER,
                          into=Path(into), account="acct-1")
        queue.put(("ok", booking.get("booked_by")))
    except Exception as exc:  # noqa: BLE001 cross-process result channel
        queue.put(("error", type(exc).__name__, str(exc)))


class BookTransactionTests(BillCandidateTestBase):
    def ready(self):
        return self.review_all_key_fields(self.add_pasted())

    def test_missing_usage_is_checked_before_any_write(self):
        candidate = self.ready()
        self.bundle.mkdir(parents=True)
        (self.bundle / "keep.txt").write_text("operator input", encoding="utf-8")
        with self.assertRaises(CloseError) as ctx:
            bc.book(self.store, candidate["candidate_id"], reviewer=REVIEWER,
                    into=self.bundle, account="acct-1", usage="missing.jsonl")
        self.assertEqual("INPUT_REQUIRED", ctx.exception.code)
        self.assertEqual(["keep.txt"],
                         sorted(item.name for item in self.bundle.iterdir()))

    def test_missing_rule_map_is_checked_before_any_write(self):
        candidate = self.ready()
        with self.assertRaises(CloseError) as ctx:
            bc.book(self.store, candidate["candidate_id"], reviewer=REVIEWER,
                    into=self.bundle, account="acct-1",
                    rule_map="missing-rules.json")
        self.assertEqual("INPUT_REQUIRED", ctx.exception.code)
        self.assertFalse(self.bundle.exists())

    def test_csv_failure_leaves_no_partial_bundle(self):
        candidate = self.ready()
        self.bundle.mkdir(parents=True)
        (self.bundle / "keep.txt").write_text("operator input", encoding="utf-8")
        with patch.object(bc.csv.DictWriter, "writerows",
                          side_effect=OSError("disk full")):
            with self.assertRaises(CloseError) as ctx:
                bc.book(self.store, candidate["candidate_id"], reviewer=REVIEWER,
                        into=self.bundle, account="acct-1")
        self.assertEqual("ARTIFACT_PARTIAL", ctx.exception.code)
        self.assertEqual(["keep.txt"],
                         sorted(item.name for item in self.bundle.iterdir()))
        self.assertFalse(list(self.root.glob(".staging-book-*")))

    def test_copy_failure_leaves_no_partial_bundle(self):
        candidate = self.ready()
        source = self.store / candidate["candidate_id"] / candidate["source"]["file"]
        source.unlink()
        with self.assertRaises(CloseError) as ctx:
            bc.book(self.store, candidate["candidate_id"], reviewer=REVIEWER,
                    into=self.bundle, account="acct-1")
        self.assertEqual("ARTIFACT_PARTIAL", ctx.exception.code)
        self.assertFalse(self.bundle.exists())

    def test_interrupted_finalize_is_rolled_back_then_replayed(self):
        candidate = self.ready()
        self.bundle.mkdir(parents=True)
        (self.bundle / "keep.txt").write_text("operator input", encoding="utf-8")
        # Simulate a crash after the pending marker and one generated file landed.
        leftover = self.bundle / "INV-2026-1023-invoice.csv"
        leftover.write_text("half\r\n", encoding="utf-8")
        pending = self.bundle / bc.PENDING_NAME
        pending.write_text(json.dumps({
            "schema": "verovo.booking-pending/1",
            "candidate_id": candidate["candidate_id"],
            "files": [{"name": leftover.name,
                       "sha256": bc._sha256_file(leftover)}],
        }), encoding="utf-8")

        booking = bc.book(self.store, candidate["candidate_id"], reviewer=REVIEWER,
                          into=self.bundle, account="acct-1")

        self.assertTrue(booking["export_sha256"])
        self.assertFalse(pending.exists())
        self.assertIn("INV-2026-1023-invoice.csv",
                      [item.name for item in self.bundle.iterdir()])
        self.assertIn("booking.json", [item.name for item in self.bundle.iterdir()])
        self.assertTrue((self.bundle / "keep.txt").is_file())

    def test_rerun_repairs_candidate_status_without_exporting_twice(self):
        candidate = self.ready()
        first = bc.book(self.store, candidate["candidate_id"], reviewer=REVIEWER,
                        into=self.bundle, account="acct-1")
        # Simulate a crash after the bundle committed but before the status write.
        stored = bc.load_candidate(self.store, candidate["candidate_id"])
        stored["status"] = "unreviewed"
        stored["booking"] = None
        bc._write_candidate(self.store, stored)

        repaired = bc.book(self.store, candidate["candidate_id"], reviewer=REVIEWER,
                           into=self.bundle, account="acct-1")
        self.assertEqual(first["booked_at"], repaired["booked_at"])
        self.assertEqual(first["export_sha256"], repaired["export_sha256"])
        self.assertEqual("booked", bc.load_candidate(
            self.store, candidate["candidate_id"])["status"])
        exports = [item.name for item in self.bundle.iterdir()]
        self.assertEqual(1, len([name for name in exports if name.endswith("-invoice.csv")]))

    def test_another_candidate_cannot_overwrite_a_booked_bundle(self):
        first = self.ready()
        bc.book(self.store, first["candidate_id"], reviewer=REVIEWER,
                into=self.bundle, account="acct-1")
        before = sorted(item.name for item in self.bundle.iterdir())

        other_text = INVOICE_TEXT.replace("INV-2026-1023", "INV-2026-9999")
        second = self.review_all_key_fields(self.add_pasted(other_text))
        with self.assertRaises(CloseError) as ctx:
            bc.book(self.store, second["candidate_id"], reviewer=REVIEWER,
                    into=self.bundle, account="acct-1")
        self.assertEqual("REVIEW_REQUIRED", ctx.exception.code)
        self.assertEqual(before, sorted(item.name for item in self.bundle.iterdir()))
        self.assertEqual("unreviewed", bc.load_candidate(
            self.store, second["candidate_id"])["status"])

    def test_committed_invoice_stays_booked_after_a_status_crash(self):
        first = self.ready()
        bc.book(self.store, first["candidate_id"], reviewer=REVIEWER,
                into=self.bundle, account="acct-1")
        # Simulate a crash after the bundle commit but before the status write:
        # only the store-level booking index and the bundle manifest survive.
        stored = bc.load_candidate(self.store, first["candidate_id"])
        stored["status"] = "unreviewed"
        stored["booking"] = None
        bc._write_candidate(self.store, stored)

        second = self.review_all_key_fields(self.add_pasted())
        with self.assertRaises(CloseError) as ctx:
            bc.book(self.store, second["candidate_id"], reviewer=REVIEWER,
                    into=self.root / "other-bundle", account="acct-1")
        self.assertEqual("REVIEW_REQUIRED", ctx.exception.code)
        self.assertFalse((self.root / "other-bundle").exists())

    def test_usage_and_rule_map_survive_booking(self):
        candidate = self.ready()
        self.bundle.mkdir(parents=True)
        (self.bundle / "usage-export.jsonl").write_text(USAGE_JSONL, encoding="utf-8")
        (self.bundle / "contract-rules.json").write_text(
            json.dumps(RULE_MAP, indent=2) + "\n", encoding="utf-8")
        bc.book(self.store, candidate["candidate_id"], reviewer=REVIEWER,
                into=self.bundle, account="acct-1", usage="usage-export.jsonl",
                rule_map="contract-rules.json")
        job = json.loads((self.bundle / "period-job.json").read_text(encoding="utf-8"))
        self.assertEqual(["bills", "invoice", "usage"],
                         [entry["role"] for entry in job["sources"]])


class ConcurrencyTests(BillCandidateTestBase):
    def test_two_imports_never_share_a_candidate_id(self):
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        workers = [
            context.Process(target=_import_worker,
                            args=(str(self.store), INVOICE_TEXT, queue))
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
        outcomes = [queue.get(timeout=5) for _ in range(2)]
        ids = [item[1] for item in outcomes if item[0] == "ok"]
        self.assertTrue(ids, outcomes)
        self.assertEqual(len(ids), len(set(ids)), "两个进程分配到了同一个候选 id")
        self.assertEqual(len(ids), len(bc.list_candidates(self.store)))
        self.assertFalse(list(self.store.glob(bc.STAGING_PREFIX + "*")))

    def test_two_bookings_of_one_candidate_succeed_once(self):
        candidate = self.review_all_key_fields(self.add_pasted())
        self.bundle.mkdir(parents=True)
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        workers = [
            context.Process(target=_book_worker,
                            args=(str(self.store), candidate["candidate_id"],
                                  str(self.bundle), queue))
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
        outcomes = [queue.get(timeout=5) for _ in range(2)]
        self.assertEqual(1, len([item for item in outcomes if item[0] == "ok"]),
                         outcomes)
        exports = [name for name in
                   (item.name for item in self.bundle.iterdir())
                   if name.endswith("-invoice.csv")]
        self.assertEqual(1, len(exports))


if __name__ == "__main__":
    unittest.main()
