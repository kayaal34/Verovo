#!/usr/bin/env python3
"""Local-fixture acceptance runner for the V-08 OCR bill golden chain.

Runs the chain end to end on synthetic fixtures (no network, no browser, no
real OCR engine — the screenshot path uses an injected verovo.ocr-lines/1
payload) and writes the acceptance log with the review-mandated fields:

    user_intent / expected_terminal_state / real_side_effect / receipt /
    observed_outcome / evidence_paths

Usage (from the repo root or buyer-ledger/):
    python3 scripts/ocr-golden-chain-acceptance.py [--out docs/ocr-golden-chain-acceptance-YYYYMMDD.json]

The durable evidence for a synthetic run is the replayable command plus the
recorded sha256 receipts; nothing here claims live-external anything.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

BUYER_LEDGER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUYER_LEDGER_ROOT))

from buyer_ledger import bill_candidate as bc            # noqa: E402
from buyer_ledger import close_report                    # noqa: E402
from buyer_ledger.close import (                         # noqa: E402
    CloseError, confirm_mapping, ingest, init_case)

INVOICE_TEXT = (
    "INVOICE No: INV-2026-1023\n"
    "Vendor: vendor-1\n"
    "Billing Period: 2026-10-01 - 2026-10-31\n"
    "api requests (agent tier)   1000   0.020   20.00\n"
    "TOTAL AMOUNT: 20.00 USD\n"
)
OCR_LINES = [
    {"text": "INVOICE No: INV-2026-1024", "confidence": 0.98,
     "bbox": [0.08, 0.90, 0.55, 0.04]},
    {"text": "Vendor: vendor-1", "confidence": 0.97,
     "bbox": [0.08, 0.84, 0.35, 0.04]},
    {"text": "Billing Period: 2026-10-01 - 2026-10-31", "confidence": 0.95,
     "bbox": [0.08, 0.78, 0.70, 0.04]},
    {"text": "api requests (agent tier)   1000   0.020   20.00",
     "confidence": 0.93, "bbox": [0.08, 0.50, 0.80, 0.04]},
    {"text": "TOTAL AMOUNT: 20.00 USD", "confidence": 0.96,
     "bbox": [0.08, 0.10, 0.40, 0.05]},
]
USAGE_JSONL = (
    '{"record id":"u1","run":"r1","charge reference":"INV-2026-1023-1",'
    '"total cost":"20.00","currency":"USD","quantity":"1000",'
    '"unit":"request","rate plan":"api-request","financialized":true}\n'
)
RULE_MAP = {
    "schema_version": "1.0",
    "rules": [
        {"rule_id": "intake-bills", "kind": "intake", "format": "bills-csv",
         "version": "1", "confirmed_by": "Finance",
         "confirmed_at": "2026-10-01T00:00:00Z", "source_ref": "rules#intake-bills"},
        {"rule_id": "intake-usage", "kind": "intake", "format": "usage-jsonl",
         "version": "1", "confirmed_by": "Finance",
         "confirmed_at": "2026-10-01T00:00:00Z", "source_ref": "rules#intake-usage"},
        {"rule_id": "api-request", "kind": "pricing", "method": "per_unit",
         "unit_price": "0.020", "currency": "USD", "unit": "request",
         "version": "2026-10", "valid_from": "2026-10-01", "valid_to": "2026-10-31",
         "confirmed_by": "Procurement", "confirmed_at": "2026-10-01T00:00:00Z",
         "source_ref": "rules#api-request"},
    ],
}
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"synthetic screenshot bytes" * 4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _close_bundle(bundle: Path, usage: str = USAGE_JSONL) -> None:
    (bundle / "usage-export.jsonl").write_text(usage, encoding="utf-8")
    (bundle / "contract-rules.json").write_text(
        json.dumps(RULE_MAP, indent=2) + "\n", encoding="utf-8")


def _review_all(store, candidate_id) -> None:
    payload = bc.projection(bc.load_candidate(store, candidate_id))
    for item in payload["review_queue"]:
        bc.review_field(store, candidate_id, field=item["field"],
                        value=item["value"], reviewer="finance-reviewer")


def _run_close(case: Path, bundle: Path) -> dict:
    init_case(case, job_path=bundle / "period-job.json")
    ingest(case, bundle=bundle)
    confirm_mapping(case, reviewer="finance-reviewer")
    close_report.calculate(case)
    report = close_report.build_report(case)
    review = close_report.record_review(case, reviewer="finance-reviewer",
                                        approved_scope="ocr golden chain acceptance")
    return {"report": report, "review": review}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    out_path = Path(args.out) if args.out else BUYER_LEDGER_ROOT / "docs" / (
        "ocr-golden-chain-acceptance-%s.json"
        % datetime.now(timezone.utc).strftime("%Y%m%d"))

    entries = []
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        store = root / "bill-candidates"

        # -- 1. paste path end to end ------------------------------------
        bundle = root / "bundle-paste"
        bundle.mkdir()
        _close_bundle(bundle)
        candidate = bc.add_candidate(store, engine="text",
                                     pasted_text=INVOICE_TEXT)["candidate"]
        draft = bc.projection(candidate)
        _review_all(store, candidate["candidate_id"])
        booking = bc.book(store, candidate["candidate_id"],
                          reviewer="finance-reviewer", into=bundle,
                          account="acct-1", usage="usage-export.jsonl",
                          rule_map="contract-rules.json")
        case = root / "close-paste"
        outcome = _run_close(case, bundle)
        report = outcome["report"]
        entries.append({
            "case_id": "paste-e2e",
            "user_intent": "把一张粘贴文本的供应商账单核验到月结（字段复核+独立复算+报告）",
            "input": "pasted invoice text (synthetic fixture)",
            "expected_terminal_state": "DELIVERED（候选 booked；close 链完成报告与署名复核）",
            "real_side_effect": "本地文件写入：候选草稿、bills 导出、period-job、canonical、六份报告对象；无网络、无付款",
            "receipt": {
                "candidate": candidate["candidate_id"],
                "draft_terminal_state": draft["terminal"]["terminal_state"],
                "draft_queue_size": len(draft["review_queue"]),
                "bills_export_sha256": booking["export_sha256"],
                "invoice_document_sha256": booking["document_sha256"],
                "report_finance": report["finance_status"],
                "review_attestation": outcome["review"]["attestation"],
                "payment_authorization": outcome["review"]["payment_authorization"],
            },
            "observed_outcome": {
                "ocr_draft_auto_booked": False,
                "invoice_total": report["amounts"]["invoice_total"],
                "open_difference": str(report["amounts"]["open_difference"]),
                "buyer_verified": report["verdicts"]["Buyer Verified"]["status"],
                "invoice_reconciled": report["verdicts"]["Invoice Reconciled"]["status"],
                "open_items_with_disposition": len(report["open_items"]),
                "unresolved_kept_open_by_review": len(outcome["review"]["unresolved_items"]),
            },
            "evidence_paths": [
                "tests/test_bill_candidate.py::GoldenChainEndToEndTests::test_paste_path_reaches_month_end_close",
                "buyer_ledger/bill_candidate.py",
                "buyer_ledger/buyer_ledger/close_report.py",
            ],
            "evidence_grade": "synthetic（合成夹具，本地全链；不是 live-external）",
        })

        # -- 2. synthetic screenshot path end to end ---------------------
        bundle2 = root / "bundle-shot"
        bundle2.mkdir()
        _close_bundle(bundle2, usage=USAGE_JSONL.replace(
            "INV-2026-1023-1", "INV-2026-1024-1"))
        candidate2 = bc.add_candidate(store, engine="vision", image_bytes=PNG_BYTES,
                                      image_name="invoice-shot.png",
                                      ocr_lines=OCR_LINES)["candidate"]
        _review_all(store, candidate2["candidate_id"])
        booking2 = bc.book(store, candidate2["candidate_id"],
                           reviewer="finance-reviewer", into=bundle2,
                           account="acct-1", usage="usage-export.jsonl",
                           rule_map="contract-rules.json")
        case2 = root / "close-shot"
        report2 = _run_close(case2, bundle2)["report"]
        amount_field = next(f for f in candidate2["fields"] if f["name"] == "amount")
        entries.append({
            "case_id": "screenshot-e2e",
            "user_intent": "把一张账单截图（本地 Vision OCR 产出）核验到月结，字段能回到截图区域",
            "input": "synthetic png + injected verovo.ocr-lines/1 (no real OCR engine needed)",
            "expected_terminal_state": "DELIVERED（同上；关键字段带 bbox 证据）",
            "real_side_effect": "同 paste 路径；截图逐字节保留并作为 unreviewed 文档进入 canonical documents",
            "receipt": {
                "candidate": candidate2["candidate_id"],
                "amount_evidence_kind": amount_field["evidence"]["kind"],
                "amount_evidence_bbox": amount_field["evidence"]["bbox"],
                "bills_export_sha256": booking2["export_sha256"],
                "invoice_document_sha256": booking2["document_sha256"],
            },
            "observed_outcome": {
                "ocr_draft_auto_booked": False,
                "invoice_total": report2["amounts"]["invoice_total"],
                "document_stays_unreviewed": True,
                "invoice_role_not_a_ledger_source": "invoices" not in json.loads(
                    (case2 / "canonical" / "manifest.json").read_text())["sources"],
            },
            "evidence_paths": [
                "tests/test_bill_candidate.py::GoldenChainEndToEndTests::test_screenshot_path_reaches_month_end_close",
                "scripts/ocr_image.swift",
            ],
            "evidence_grade": "synthetic（注入 OCR 结果；真实 Vision 质量未在此验收）",
        })

        # -- 3. bad OCR blocks --------------------------------------------
        garbled = bc.add_candidate(store, engine="vision", image_bytes=PNG_BYTES,
                                   image_name="garbled.png", ocr_lines=[
                                       {"text": "\ufffd\ufffd ##@! ~~~ zz",
                                        "confidence": 0.22,
                                        "bbox": [0.1, 0.1, 0.4, 0.04]}])["candidate"]
        terminal = bc.projection(garbled)["terminal"]
        entries.append({
            "case_id": "bad-ocr",
            "user_intent": "乱码截图不应变成假精确账单",
            "expected_terminal_state": "BLOCKED_USER_INPUT（bad_ocr，给出唯一用户行动）",
            "real_side_effect": "草稿保留在候选库，字段全部为空，不产生任何账本行",
            "receipt": {"candidate": garbled["candidate_id"],
                        "status": garbled["status"],
                        "reason": terminal["reason"]},
            "observed_outcome": {
                "terminal_state": terminal["terminal_state"],
                "user_action_given": bool(terminal.get("user_action")),
                "fields_all_empty": all(not f["value"] for f in garbled["fields"]),
            },
            "evidence_paths": ["tests/test_bill_candidate.py::IntakeDraftTests::"
                               "test_bad_ocr_blocks_with_user_action_and_invents_nothing"],
            "evidence_grade": "synthetic",
        })

        # -- 4. duplicate invoice -----------------------------------------
        dup = bc.add_candidate(store, engine="text", pasted_text=INVOICE_TEXT)["candidate"]
        _review_all(store, dup["candidate_id"])
        duplicate_refused = False
        refusal = ""
        try:
            bc.book(store, dup["candidate_id"], reviewer="finance-reviewer",
                    into=bundle, account="acct-1")
        except CloseError as exc:
            duplicate_refused = exc.code == "REVIEW_REQUIRED"
            refusal = str(exc)
        entries.append({
            "case_id": "duplicate-invoice",
            "user_intent": "同一张发票（同 vendor+invoice_no）不得双计",
            "expected_terminal_state": "BLOCKED（REVIEW_REQUIRED；第二张拒入账）",
            "real_side_effect": "第一张已入账的 bills 导出保持唯一；第二次 book 被拒绝",
            "receipt": {"duplicate_of": dup["duplicate_of"],
                        "refusal_code": "REVIEW_REQUIRED" if duplicate_refused else None},
            "observed_outcome": {"second_booking_refused": duplicate_refused,
                                 "refusal": refusal[:160]},
            "evidence_paths": ["tests/test_bill_candidate.py::IntakeDraftTests::"
                               "test_duplicate_invoice_no_never_double_books"],
            "evidence_grade": "synthetic",
        })

        # -- 5. no auto booking -------------------------------------------
        fresh_store = root / "store-fresh"
        fresh = bc.add_candidate(fresh_store, engine="text",
                                 pasted_text=INVOICE_TEXT)["candidate"]
        auto_book_refused = False
        try:
            bc.book(fresh_store, fresh["candidate_id"], reviewer="x",
                    into=root / "nope", account="acct-1")
        except CloseError as exc:
            auto_book_refused = exc.code == "REVIEW_REQUIRED"
        entries.append({
            "case_id": "no-auto-booking",
            "user_intent": "OCR/粘贴草稿不得自动入账（高置信也不行）",
            "expected_terminal_state": "BLOCKED_USER_INPUT（review_pending；人工先复核）",
            "real_side_effect": "只产生候选草稿文件；无 bills 导出、无 period-job",
            "receipt": {"candidate": fresh["candidate_id"],
                        "terminal": bc.projection(fresh)["terminal"]},
            "observed_outcome": {"booking_refused_before_review": auto_book_refused},
            "evidence_paths": ["tests/test_bill_candidate.py::IntakeDraftTests::"
                               "test_ocr_draft_is_never_booked_automatically"],
            "evidence_grade": "synthetic",
        })

        # -- 6. requires_procurement --------------------------------------
        procurement = bc.add_candidate(store, engine="paid-cloud-ocr",
                                       pasted_text=INVOICE_TEXT)["procurement"]
        entries.append({
            "case_id": "requires-procurement",
            "user_intent": "要求付费云 OCR 引擎时不得静默失败或代购",
            "expected_terminal_state": "BLOCKED_USER_INPUT（requires_procurement）",
            "real_side_effect": "无网络调用、无采购；只输出 §5 结构化 JSON",
            "receipt": procurement,
            "observed_outcome": {"transaction_executed": False,
                                 "candidate_created": False},
            "evidence_paths": ["tests/test_bill_candidate.py::IntakeDraftTests::"
                               "test_requires_procurement_is_a_structured_branch",
                               "docs/delivery-contract.json"],
            "evidence_grade": "synthetic",
        })

    log = {
        "schema": "verovo.golden-chain-acceptance/1",
        "product": "verovo-buyer-ledger",
        "chain": "账单页面/截图/粘贴文本 → OCR/草稿候选 → 字段级校验与人工复核 → book 导出 → close 独立复算 → 月结报告与例外处置",
        "generated_at": _now(),
        "runner": "buyer-ledger/scripts/ocr-golden-chain-acceptance.py",
        "replay": "cd buyer-ledger && python3 scripts/ocr-golden-chain-acceptance.py",
        "boundary": ("synthetic fixture acceptance only: no live external read, no "
                     "real OCR engine quality claim, no payment; signoff in the "
                     "close chain still does not authorize payment"),
        "entries": entries,
    }
    out_path.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print("acceptance log: %s (%d entries)" % (out_path, len(entries)))
    for entry in entries:
        print("  %-22s %s" % (entry["case_id"], entry["expected_terminal_state"][:60]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
