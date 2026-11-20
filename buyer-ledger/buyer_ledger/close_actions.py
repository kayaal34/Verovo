"""Append-only, explicitly evidenced disposition of buyer-close open items.

A report/review/signoff never implies that a difference was sent to a vendor,
corrected, disputed, reserved or resolved. This module records those decisions
as separate declarations bound to one report calculation and one open item.
It does not contact a supplier, post an accounting correction or move money.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .close import CloseError

ACTION_SCHEMA = "verovo.close-difference-action/1"
ACTION_LOG = "difference-actions.jsonl"
ACTIONS = ("inquiry", "correction", "dispute", "reserve", "resolved")
OPEN_STATES = {
    "inquiry": "inquiry_recorded",
    "correction": "correction_recorded",
    "dispute": "dispute_open",
    "reserve": "reserve_recorded",
    "resolved": "resolved",
}


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path, label: str) -> Dict[str, Any]:
    if not path.is_file():
        raise CloseError("INPUT_REQUIRED", "%s not found: %s" % (label, path))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CloseError("INPUT_REQUIRED", "%s is invalid JSON: %s" % (label, exc)) from exc
    if not isinstance(payload, dict):
        raise CloseError("INPUT_REQUIRED", "%s must be an object" % label)
    return payload


def _parse_time(value: str, label: str) -> str:
    text = _text(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CloseError("INPUT_REQUIRED", "%s must be ISO 8601" % label) from exc
    if parsed.tzinfo is None:
        raise CloseError("INPUT_REQUIRED", "%s must include a timezone" % label)
    return text


def read_actions(case_dir: Path) -> List[Dict[str, Any]]:
    path = Path(case_dir) / ACTION_LOG
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CloseError("STALE_SOURCE", "%s line %d is corrupt: %s"
                                 % (ACTION_LOG, line_no, exc)) from exc
            if not isinstance(row, dict) or row.get("schema") != ACTION_SCHEMA:
                raise CloseError("STALE_SOURCE", "%s line %d has an invalid schema"
                                 % (ACTION_LOG, line_no))
            rows.append(row)
    return rows


def _current_calculation(case_dir: Path) -> str:
    calculation = _read_json(Path(case_dir) / "calculation.json", "calculation.json")
    fingerprint = _text(calculation.get("calculation_fingerprint"))
    if not fingerprint:
        raise CloseError("STALE_SOURCE", "calculation fingerprint is missing")
    return fingerprint


def applicable_actions(case_dir: Path) -> List[Dict[str, Any]]:
    """Return only actions bound to the current calculation, never stale ones.

    A revision may carry the parent's append-only log as lineage. Parent actions
    stay readable in that file but do not close items in the new calculation.
    """
    current = _current_calculation(case_dir)
    return [row for row in read_actions(case_dir)
            if _text(row.get("calculation_fingerprint")) == current]


def project_open_items(case_dir: Path,
                       open_items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Overlay explicit action state without changing amounts or source findings."""
    if not (Path(case_dir) / ACTION_LOG).exists():
        rows = []
    else:
        rows = applicable_actions(case_dir)
    by_item: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_item.setdefault(_text(row.get("open_item_ref")), []).append(row)
    projected = []
    for original in open_items:
        item = dict(original)
        history = by_item.get(_text(item.get("id")), [])
        if history:
            latest = history[-1]
            item["state"] = ("resolved" if latest.get("action") == "resolved"
                             else OPEN_STATES.get(str(latest.get("action")), "unresolved"))
            item["difference_action"] = latest
            item["difference_action_history"] = [row["action_id"] for row in history]
        else:
            item["difference_action"] = None
            item["difference_action_history"] = []
        projected.append(item)
    known = {_text(item.get("id")) for item in open_items}
    orphaned = [row.get("open_item_ref") for row in rows
                if _text(row.get("open_item_ref")) not in known]
    if orphaned:
        raise CloseError("STALE_SOURCE",
                         "difference action references an item absent from this report: %s"
                         % sorted(set(orphaned)))
    by_action = {name: sum(row.get("action") == name for row in rows)
                 for name in ACTIONS}
    summary = {
        "schema": "verovo.close-difference-action-projection/1",
        "total": len(rows), "by_action": by_action,
        "actioned_item_refs": sorted(by_item),
        "resolved_item_refs": sorted(item["id"] for item in projected
                                     if item.get("state") == "resolved"
                                     and item.get("difference_action")),
        "unresolved_item_refs": [item["id"] for item in projected
                                 if item.get("state") not in
                                 ("resolved", "closed", "not_applicable")],
        "external_action_executed": False,
        "payment_authorization": False,
        "evidence_status": "declared_reference_not_independently_verified",
        "boundary": ("Actions are append-only local declarations with source refs. "
                     "Recording an inquiry/correction/dispute/reserve is not proof "
                     "that the supplier received or accepted it. Only an explicit "
                     "resolved action changes an item to resolved; review/signoff never do."),
    }
    return projected, summary


def record_action(case_dir: Path, *, open_item_ref: str, action: str,
                  owner: str, by: str, source_ref: str, note: str = "",
                  occurred_at: str = "") -> Dict[str, Any]:
    case_dir = Path(case_dir)
    if (case_dir / "signoff.json").is_file():
        raise CloseError("REVIEW_REQUIRED",
                         "signed revisions are immutable; record the action in a new revision")
    report = _read_json(case_dir / "report" / "report.json", "report/report.json")
    calculation_fingerprint = _current_calculation(case_dir)
    item_id = _text(open_item_ref)
    matches = [item for item in report.get("open_items", [])
               if _text(item.get("id")) == item_id]
    if not matches:
        raise CloseError("INPUT_REQUIRED",
                         "open_item_ref %r does not exist in this report" % item_id)
    action = _text(action).lower()
    if action not in ACTIONS:
        raise CloseError("UNSUPPORTED_SCOPE",
                         "action must be one of %s" % (list(ACTIONS),))
    owner, by, source_ref = _text(owner), _text(by), _text(source_ref)
    for value, label in ((owner, "owner"), (by, "by"), (source_ref, "source_ref")):
        if not value:
            raise CloseError("INPUT_REQUIRED", "difference action needs %s" % label)
    note = _text(note)
    if action == "resolved" and not note:
        raise CloseError("INPUT_REQUIRED",
                         "resolved needs a note explaining the resolution basis")
    occurred = _parse_time(occurred_at or _now(), "occurred_at")
    identity = {
        "job_id": report.get("job_id"), "revision_id": report.get("revision_id"),
        "calculation_fingerprint": calculation_fingerprint,
        "open_item_ref": item_id, "action": action, "owner": owner, "by": by,
        "source_ref": source_ref, "note": note,
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":")).encode("utf-8")).hexdigest()
    action_id = "DA-" + digest[:20]
    existing = [row for row in read_actions(case_dir) if row.get("action_id") == action_id]
    if existing:
        result = dict(existing[0])
        result["idempotent"] = True
        return result
    row = dict(identity)
    row.update({
        "schema": ACTION_SCHEMA, "action_id": action_id,
        "occurred_at": occurred, "recorded_at": _now(),
        "attestation": "attested_by_declaration",
        "external_action_executed": False,
        "receipt_verification": "declared_reference_not_independently_verified",
        "idempotent": False,
    })
    path = case_dir / ACTION_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = case_dir / ".difference-actions.lock"
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows fallback is process-local
        fcntl = None
    with lock_path.open("a+b") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            # Re-check after taking the lock so concurrent identical requests
            # cannot append the same deterministic action twice.
            if any(item.get("action_id") == action_id for item in read_actions(case_dir)):
                result = next(item for item in read_actions(case_dir)
                              if item.get("action_id") == action_id)
                result = dict(result, idempotent=True)
                return result
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    # The action is valid only if its user-facing projection can be rebuilt.
    # Do not delete the append-only row on a projection failure; fail loud and
    # preserve the evidence for recovery instead of rewriting history.
    from . import close_report
    close_report.build_report(case_dir)
    return row


def list_actions(case_dir: Path) -> Dict[str, Any]:
    report = _read_json(Path(case_dir) / "report" / "report.json", "report/report.json")
    projected, summary = project_open_items(case_dir, report.get("open_items", []))
    return {"actions": applicable_actions(case_dir), "projection": summary,
            "open_items": projected}
