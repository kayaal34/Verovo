"""文档登记、受审规则重放与文档级原子入账。"""

import hashlib
import os
from typing import Optional

from . import parsing
from .llm import LLMClient
from .model import LedgerError, SourceDocument
from .store import LedgerStore

MAX_FILE_BYTES = 20 * 1024 * 1024
# 业务优先：上限可调，不因默认值直接挡住一整个账期的导入。
IMPORT_LIMIT_ENV = "VEROVO_MAX_IMPORT_MB"


def _max_file_bytes():
    raw = os.environ.get(IMPORT_LIMIT_ENV, "").strip()
    if raw:
        try:
            configured = int(float(raw) * 1024 * 1024)
        except ValueError:
            configured = 0
        if configured > 0:
            return configured
    return MAX_FILE_BYTES


def read_and_hash(path):
    limit = _max_file_bytes()
    size = os.path.getsize(path)
    if size > limit:
        raise LedgerError(
            "file exceeds %d-byte import limit; split it by period or set %s to raise the cap"
            % (limit, IMPORT_LIMIT_ENV))
    with open(path, "rb") as f:
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise LedgerError(
            "file exceeds %d-byte import limit; split it by period or set %s to raise the cap"
            % (limit, IMPORT_LIMIT_ENV))
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise LedgerError("only UTF-8 CSV/TSV is supported without loss; convert encoding first") from exc
    return text, hashlib.sha256(raw).hexdigest()


def import_file(store: LedgerStore, path, provider, tier, period="",
                doc_type="bill", account_ref="", use_agent_if_new=False,
                llm_client: Optional[LLMClient] = None, save_rule=True,
                fetched_via="manual_import", fetched_at="", extra=None):
    """Agent 规则只存草案；已批准规则才解析，并在一个事务内写入全部行。"""
    if tier in parsing.PARSEABLE_TIERS:
        # 未解析材料也必须有账期，否则会绕到账期覆盖检查之外。
        parsing._period(period)
    text, content_hash = read_and_hash(path)
    file_name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    doc = SourceDocument(
        content_hash=content_hash, provider=provider, tier=tier,
        account_ref=account_ref, doc_type=doc_type, period=period,
        file_path=path, file_name=file_name, fetched_via=fetched_via,
        fetched_at=fetched_at, extra=extra or {})
    doc_id, created = store.put_document(doc)
    if not created and store.count_rows_for_document(doc_id) > 0:
        return {"document_id": doc_id, "duplicate": True, "charge_lines": 0,
                "usage_events": 0, "notes": ["same content hash already imported (INV-1)"]}

    if tier not in parsing.PARSEABLE_TIERS:
        return {"document_id": doc_id, "duplicate": not created, "charge_lines": 0,
                "usage_events": 0, "notes": ["tier %s stored as reference only" % tier]}

    fingerprint = parsing.format_fingerprint(file_name, text)
    rule = store.get_parse_rule(fingerprint, provider=provider,
                                doc_type=doc_type, tier=tier)
    if rule is not None and not rule["approved"]:
        return {"document_id": doc_id, "duplicate": False, "fingerprint": fingerprint,
                "charge_lines": 0, "usage_events": 0, "review_required": True,
                "spec": rule["spec"],
                "notes": ["parse rule is a draft; run rule approve before parsing"]}
    if rule is None:
        if not use_agent_if_new:
            return {"document_id": doc_id, "duplicate": False,
                    "charge_lines": 0, "usage_events": 0, "fingerprint": fingerprint,
                    "notes": ["no approved parse rule; run parse --agent, or "
                              "preset apply for known export formats"]}
        if not (llm_client and llm_client.configured):
            return {"document_id": doc_id, "duplicate": False,
                    "charge_lines": 0, "usage_events": 0, "fingerprint": fingerprint,
                    "notes": ["LLM not configured: set VEROVO_LLM_BASE_URL / "
                              "VEROVO_LLM_API_KEY / VEROVO_LLM_MODEL for --agent "
                              "drafting, or preset apply for known export formats"]}
        spec, golden = parsing.agent_generate_spec(
            file_name, text, provider, doc_type, llm_client)
        if save_rule:
            store.put_parse_rule(fingerprint, spec, provider=provider,
                                 doc_type=doc_type, tier=tier,
                                 created_via="agent", golden=golden, approved=False)
        return {"document_id": doc_id, "duplicate": False, "fingerprint": fingerprint,
                "charge_lines": 0, "usage_events": 0, "review_required": True,
                "spec": spec, "spec_source": "agent_draft",
                "notes": ["agent draft validated but not applied; review and run rule approve"]}

    spec = rule["spec"]
    result = parsing.apply_spec(file_name, text, spec, doc, provider, period=period,
                                review_status="verified")
    n_lines = n_events = 0
    try:
        for cl in result["charge_lines"]:
            _, inserted = store.put_charge_line(cl, commit=False)
            n_lines += 1 if inserted else 0
        for ue in result["usage_events"]:
            _, inserted = store.put_usage_event(ue, commit=False)
            n_events += 1 if inserted else 0
        store.conn.commit()
    except Exception:
        store.conn.rollback()
        raise
    return {"document_id": doc_id, "duplicate": False,
            "fingerprint": fingerprint, "spec_source": "replay",
            "charge_lines": n_lines, "usage_events": n_events,
            "review_required": False,
            "notes": result["notes"], "constraint": result["constraint"]}
