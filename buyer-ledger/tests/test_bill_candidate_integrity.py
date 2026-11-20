"""V-09b acceptance tests: the booked document is the reviewed document.

``add_candidate`` records the SHA-256 of the bill file and of the OCR text it
was extracted from. A field review and a booking are human attestations about
*that* source. These tests freeze the red lines:

- swapping or editing the source file after review makes review and book fail
  with ``STALE_SOURCE``; the old review never books a different file;
- tampering with the OCR text, the structured OCR payload, or their agreement
  is rejected the same way;
- deleting the source file is rejected, not exported as an empty document;
- a checked-but-later-swapped file is caught again at copy time (TOCTOU);
- an untouched candidate still books, and the bundle document is byte-identical
  to the reviewed source;
- without ``fcntl`` the candidate store refuses to run instead of pretending to
  have cross-process exclusion.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from buyer_ledger import bill_candidate as bc
from buyer_ledger.close import CloseError

INVOICE_TEXT = (
    "INVOICE No: INV-2026-1023\n"
    "Vendor: vendor-1\n"
    "Billing Period: 2026-10-01 - 2026-10-31\n"
    "api requests (agent tier)   1000   0.020   20.00\n"
    "TOTAL AMOUNT: 20.00 USD\n"
)

OCR_LINES = {
    "schema": "verovo.ocr-lines/1",
    "lines": [
        {"text": "INVOICE No: INV-2026-1023", "confidence": 0.98,
         "bbox": [0.08, 0.06, 0.55, 0.04]},
        {"text": "Vendor: vendor-1", "confidence": 0.97,
         "bbox": [0.08, 0.12, 0.35, 0.04]},
        {"text": "Billing Period: 2026-10-01 - 2026-10-31", "confidence": 0.95,
         "bbox": [0.08, 0.18, 0.70, 0.04]},
        {"text": "api requests (agent tier)   1000   0.020   20.00",
         "confidence": 0.93, "bbox": [0.08, 0.40, 0.80, 0.04]},
        {"text": "TOTAL AMOUNT: 20.00 USD", "confidence": 0.96,
         "bbox": [0.08, 0.90, 0.40, 0.05]},
    ],
}

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"synthetic screenshot bytes" * 4


class IntegrityTestBase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "bill-candidates"
        self.bundle = self.root / "bundle"

    def add_pasted(self):
        return bc.add_candidate(self.store, engine="text",
                                pasted_text=INVOICE_TEXT)["candidate"]

    def add_screenshot(self):
        lines = json.loads(json.dumps(OCR_LINES))["lines"]
        return bc.add_candidate(self.store, engine="vision", image_bytes=PNG_BYTES,
                                image_name="invoice-shot.png",
                                ocr_lines=lines)["candidate"]

    def review_all(self, candidate):
        payload = bc.projection(candidate)
        for item in payload["review_queue"]:
            bc.review_field(self.store, candidate["candidate_id"],
                            field=item["field"], value=item["value"],
                            reviewer="finance-reviewer")
        return bc.load_candidate(self.store, candidate["candidate_id"])

    def candidate_path(self, candidate, name=None):
        return self.store / candidate["candidate_id"] / (
            name or candidate["source"]["file"])

    def book(self, candidate):
        return bc.book(self.store, candidate["candidate_id"], reviewer="finance",
                       into=self.bundle, account="acct-1")

    def assert_stale(self, ctx):
        self.assertEqual("STALE_SOURCE", ctx.exception.code)


class TextSourceIntegrityTests(IntegrityTestBase):
    def test_tampered_source_after_review_blocks_booking(self):
        candidate = self.review_all(self.add_pasted())
        self.candidate_path(candidate).write_text(
            "INVOICE No: INV-2026-1023\nTOTAL AMOUNT: 999.00 USD\n",
            encoding="utf-8")
        with self.assertRaises(CloseError) as ctx:
            self.book(candidate)
        self.assert_stale(ctx)
        self.assertFalse(self.bundle.exists(), "no bundle may be written")

    def test_tampered_source_blocks_further_field_review(self):
        candidate = self.add_pasted()
        self.candidate_path(candidate).write_text(
            INVOICE_TEXT.replace("20.00", "99.00"), encoding="utf-8")
        queue = bc.projection(candidate)["review_queue"]
        with self.assertRaises(CloseError) as ctx:
            bc.review_field(self.store, candidate["candidate_id"],
                            field=queue[0]["field"], value=queue[0]["value"],
                            reviewer="finance-reviewer")
        self.assert_stale(ctx)

    def test_missing_source_blocks_booking(self):
        candidate = self.review_all(self.add_pasted())
        self.candidate_path(candidate).unlink()
        with self.assertRaises(CloseError) as ctx:
            self.book(candidate)
        self.assert_stale(ctx)

    def test_source_swapped_between_review_and_copy_is_caught(self):
        candidate = self.review_all(self.add_pasted())
        original_validate = bc._validate_job

        def tamper_then_validate(job):
            original_validate(job)
            self.candidate_path(candidate).write_text(
                "INVOICE No: INV-2026-1023\nTOTAL AMOUNT: 1.00 USD\n",
                encoding="utf-8")

        with patch.object(bc, "_validate_job", side_effect=tamper_then_validate):
            with self.assertRaises(CloseError) as ctx:
                self.book(candidate)
        self.assert_stale(ctx)
        self.assertFalse(self.bundle.exists(), "staging must be cleaned up")

    def test_untouched_candidate_books_the_reviewed_file(self):
        candidate = self.review_all(self.add_pasted())
        source_bytes = self.candidate_path(candidate).read_bytes()
        result = self.book(candidate)
        document = Path(result["document_path"]).read_bytes()
        self.assertEqual(source_bytes, document)


class ImageOcrIntegrityTests(IntegrityTestBase):
    def test_tampered_ocr_text_blocks_booking(self):
        candidate = self.review_all(self.add_screenshot())
        (self.store / candidate["candidate_id"] / "ocr.txt").write_text(
            "TOTAL AMOUNT: 99.00 USD\n", encoding="utf-8")
        with self.assertRaises(CloseError) as ctx:
            self.book(candidate)
        self.assert_stale(ctx)

    def test_tampered_ocr_payload_blocks_booking(self):
        candidate = self.review_all(self.add_screenshot())
        path = self.store / candidate["candidate_id"] / "ocr.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["lines"][-1]["text"] = "TOTAL AMOUNT: 99.00 USD"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CloseError) as ctx:
            self.book(candidate)
        self.assert_stale(ctx)

    def test_unreadable_ocr_payload_blocks_booking(self):
        candidate = self.review_all(self.add_screenshot())
        (self.store / candidate["candidate_id"] / "ocr.json").write_text(
            "{not json", encoding="utf-8")
        with self.assertRaises(CloseError) as ctx:
            self.book(candidate)
        self.assert_stale(ctx)


class LockPlatformTests(IntegrityTestBase):
    def test_missing_fcntl_refuses_to_operate(self):
        with patch.dict(sys.modules, {"fcntl": None}):
            with self.assertRaises(CloseError) as ctx:
                self.add_pasted()
        self.assertEqual("UNSUPPORTED_CAPABILITY", ctx.exception.code)
        self.assertIn("fcntl", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
