"""Import Growovo's signed-by-hash operations/outcome handoff as evidence only.

Integrity verification proves that the JSON body was not changed after Growovo
calculated its fingerprint. It does not prove that an external action, payment,
supplier acceptance or business conversion happened. Nothing imported here is
booked into canonical finance without a separate source mapping and review.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

SOURCE_SCHEMA = "growovo.verovo-handoff/1"
EVIDENCE_SCHEMA = "verovo.growovo-evidence/1"


class HandoffError(ValueError):
    pass


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _fingerprint(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise HandoffError("handoff JSON not found: %s" % path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffError("handoff is not valid UTF-8 JSON: %s" % exc) from exc
    if not isinstance(payload, dict):
        raise HandoffError("handoff must be a JSON object")
    return payload


def _reject_sensitive(value: Any, path: str = "handoff") -> None:
    sensitive = {"password", "passwd", "cookie", "cookies", "access_token",
                 "refresh_token", "api_key", "secret", "authorization"}
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in sensitive:
                raise HandoffError("credential-like field is forbidden: %s.%s" % (path, key))
            _reject_sensitive(child, "%s.%s" % (path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive(child, "%s[%d]" % (path, index))


def validate_handoff(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema") != SOURCE_SCHEMA:
        raise HandoffError("unsupported handoff schema; expected %s" % SOURCE_SCHEMA)
    run_id = _text(payload.get("run_id"))
    if not run_id:
        raise HandoffError("handoff run_id is required")
    supplied = _text(payload.get("integrity_sha256")).lower()
    if len(supplied) != 64 or any(ch not in "0123456789abcdef" for ch in supplied):
        raise HandoffError("integrity_sha256 must be a lowercase SHA-256 hex digest")
    body = dict(payload)
    body.pop("integrity_sha256", None)
    body.pop("generated_at", None)
    expected = _fingerprint(body)
    if not hmac.compare_digest(supplied, expected):
        raise HandoffError("handoff integrity mismatch; import refused and nothing written")
    for field in ("operations", "business_events", "event_qualifications",
                  "askovo_contexts"):
        if not isinstance(payload.get(field), list) or any(
                not isinstance(row, dict) for row in payload[field]):
            raise HandoffError("%s must be an array of objects" % field)
    if not payload["operations"] and not payload["business_events"]:
        raise HandoffError("empty handoff is not importable")
    if not isinstance(payload.get("outcome_projection"), dict):
        raise HandoffError("outcome_projection must be an object")
    if payload.get("booking_status") != "evidence_only_not_canonical":
        raise HandoffError("handoff booking_status must remain evidence_only_not_canonical")
    if payload.get("external_action_executed_by_export") is not False:
        raise HandoffError("export must not claim to execute an external action")
    _reject_sensitive(payload)
    return {
        "run_id": run_id, "integrity_sha256": supplied,
        "operations": len(payload["operations"]),
        "business_events": len(payload["business_events"]),
        "contexts": len(payload["askovo_contexts"]),
        "source_grades": payload.get("evidence_grade"),
    }


def _atomic_write(path: Path, text: str) -> None:
    temp = path.with_name(path.name + ".tmp")
    with temp.open("x", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def import_handoff(source, output_dir) -> Dict[str, Any]:
    source_path = Path(source).resolve()
    payload = _read(source_path)
    checked = validate_handoff(payload)
    out = Path(output_dir)
    if out.exists():
        if not out.is_dir():
            raise HandoffError("output directory must be absent or empty: %s" % out)
        if any(out.iterdir()):
            raise HandoffError("output directory must be absent or empty: %s" % out)
    out.mkdir(parents=True, exist_ok=True)
    source_bytes = source_path.read_bytes()
    source_copy = dict(payload)
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "run_id": checked["run_id"],
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_file_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_integrity_verified": True,
        "integrity_meaning": "unchanged_json_body_not_external_truth",
        "counts": {key: checked[key] for key in
                   ("operations", "business_events", "contexts")},
        "source_evidence_grade": checked["source_grades"],
        "booking_status": "evidence_only_not_canonical",
        "receipt_verified_by_verovo": False,
        "business_outcome_observed_by_verovo": False,
        "payment_authorization": False,
        "external_action_executed_by_import": False,
        "open_items": [
            "Map each needed operation/event to an explicit buyer-controlled source.",
            "Review source independence, time window, account and evidence hash.",
            "Do not book a charge, accept delivery or recognize revenue from this handoff alone.",
        ],
        "terminal_state": "DELIVERED_WITH_GAP",
        "source": source_copy,
    }
    try:
        _atomic_write(out / "source-handoff.json",
                      json.dumps(source_copy, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(out / "evidence.json",
                      json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        # A partial import is never presented as complete; preserve any written
        # source bytes for diagnosis but remove a possibly incomplete evidence view.
        (out / "evidence.json").unlink(missing_ok=True)
        raise
    return {
        "schema": EVIDENCE_SCHEMA, "run_id": checked["run_id"],
        "output": str(out / "evidence.json"),
        "source_integrity_verified": True,
        "booking_status": evidence["booking_status"],
        "terminal_state": evidence["terminal_state"],
        "counts": evidence["counts"],
        "receipt_verified_by_verovo": False,
        "business_outcome_observed_by_verovo": False,
    }
