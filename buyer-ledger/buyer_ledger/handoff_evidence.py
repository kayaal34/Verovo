"""Normalize Growovo's evidence-only handoff into Period Intake tables.

The handoff body is hash-checked by :mod:`growovo_handoff`. That proves the
bytes did not change after export; it does not prove an external action, a
cost, a delivery acceptance or a business conversion happened. The tables
written here are the same evidence in Period Intake form:

- ``tasks.csv`` names every declared task so later sources can bind to it;
- ``runs.jsonl`` keeps the run/task relation;
- ``outcomes.csv`` keeps source-declared product events as **unverified**
  outcomes (``verified`` is always ``false``);
- ``manifest.json`` records the action facts, every excluded row and the
  boundary the reviewer must keep.

No ``usage`` table is derived: a Growovo send carries no monetary cost, and a
usage row without cost would surface in Task Economics as an "unknown cost"
finding, which would be a false statement about the buyer's spend.
"""
from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import growovo_handoff

SCHEMA = "verovo.growovo-normalized/1"
EXECUTED_RESULT = "committed"
DEFAULT_OUTCOME_STATUS = "source_declared"
BOUNDARY = (
    "A committed operation is an action fact, not delivery acceptance, cost or "
    "payment. A product event is source-declared, not buyer-verified. A "
    "dry-run is not execution, not cost and not acceptance. Nothing here is "
    "booked until a buyer adds the files to period-job.json and confirms the "
    "field mapping."
)
TASKS_HEADER = ("task_id", "type")
OUTCOMES_HEADER = ("task_id", "outcome_id", "status", "verified", "value",
                   "currency", "accepted_by", "accepted_at", "source_ref")


class HandoffNormalizeError(growovo_handoff.HandoffError):
    pass


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _operation_task_id(run_id: str, operation_id: str) -> str:
    return "growovo:%s/op/%s" % (run_id, operation_id)


def _event_task_id(run_id: str, event_id: str) -> str:
    return "growovo:%s/ev/%s" % (run_id, event_id)


def _resolve_bindings(payload: Dict[str, Any],
                      run_id: str) -> Tuple[List[Dict[str, Any]], str, List[str]]:
    """Return (bindings, source, warnings).

    A package without ``task_bindings`` (older exporter) still normalizes: the
    ids are derived with the same formula Growovo uses, and the manifest says
    they are derived so a reviewer never mistakes them for exported identity.
    """
    warnings: List[str] = []
    raw = payload.get("task_bindings")
    bindings: List[Dict[str, Any]] = []
    if isinstance(raw, list) and raw:
        for index, row in enumerate(raw):
            if not isinstance(row, dict):
                warnings.append("task_bindings[%d] is not an object; ignored" % index)
                continue
            task_id = _text(row.get("task_id"))
            if not task_id:
                warnings.append("task_bindings[%d] has no task_id; ignored" % index)
                continue
            if any(existing["task_id"] == task_id for existing in bindings):
                warnings.append("task_bindings[%d] repeats task_id %s; first kept"
                                % (index, task_id))
                continue
            bindings.append(dict(row))
        if bindings:
            return bindings, "payload", warnings
        warnings.append("task_bindings was present but unusable; derived ids used")

    for row in payload.get("operations") or []:
        operation_id = _text(row.get("operation_id"))
        if not operation_id:
            continue
        bindings.append({
            "task_id": _operation_task_id(run_id, operation_id),
            "kind": "operation", "operation_id": operation_id,
            "run_id": run_id, "result": _text(row.get("result")) or None,
        })
    for row in payload.get("business_events") or []:
        event_id = _text(row.get("event_id"))
        if not event_id:
            continue
        bindings.append({
            "task_id": _event_task_id(run_id, event_id),
            "kind": "product_event", "event_id": event_id, "run_id": run_id,
        })
    return bindings, "derived_fallback", warnings


def _operation_index(bindings: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {binding["operation_id"]: binding for binding in bindings
            if binding.get("kind") == "operation" and binding.get("operation_id")}


def _event_index(bindings: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {binding["event_id"]: binding for binding in bindings
            if binding.get("kind") == "product_event" and binding.get("event_id")}


def normalize_handoff(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the handoff and return tables plus a provenance manifest."""
    checked = growovo_handoff.validate_handoff(payload)
    run_id = checked["run_id"]
    integrity = checked["integrity_sha256"]
    bindings, bindings_source, warnings = _resolve_bindings(payload, run_id)
    if not bindings:
        raise HandoffNormalizeError(
            "handoff has no bindable operation or product event; nothing to normalize")

    operations = _operation_index(bindings)
    events = _event_index(bindings)

    tasks: List[Dict[str, str]] = []
    runs: List[Dict[str, str]] = []
    seen_tasks = set()
    seen_runs = set()
    for binding in bindings:
        task_id = binding["task_id"]
        kind = _text(binding.get("kind"))
        if kind == "operation":
            row_type = "growovo_operation"
            result = _text(binding.get("result"))
            if result:
                row_type += ":" + result
        elif kind == "product_event":
            row_type = "growovo_product_event"
        else:
            row_type = "growovo_" + (kind or "unknown")
        if task_id not in seen_tasks:
            seen_tasks.add(task_id)
            tasks.append({"task_id": task_id, "type": row_type})
        run = _text(binding.get("run_id")) or run_id
        if (run, task_id) not in seen_runs:
            seen_runs.add((run, task_id))
            runs.append({"run_id": run, "task_id": task_id})

    action_facts: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    committed = 0
    for row in payload.get("operations") or []:
        operation_id = _text(row.get("operation_id"))
        binding = operations.get(operation_id)
        result = _text(row.get("result"))
        executed = result == EXECUTED_RESULT
        if executed:
            committed += 1
        receipt = row.get("receipt")
        fact = {
            "operation_id": operation_id,
            "task_id": binding["task_id"] if binding else None,
            "channel": _text(row.get("channel")) or None,
            "action": _text(row.get("action")) or None,
            "result": result or None,
            "started_at": _text(row.get("started_at")) or None,
            "finished_at": _text(row.get("finished_at")) or None,
            "receipt_present": bool(receipt),
            "counted_as": "action_fact" if executed else "excluded",
        }
        action_facts.append(fact)
        if not executed:
            excluded.append({
                "kind": "operation", "id": operation_id,
                "task_id": fact["task_id"],
                "result": result or None,
                "reason": ("operation result %r is not an executed action: it is "
                           "not delivery, cost or acceptance" % (result or "unknown")),
            })

    outcomes: List[Dict[str, str]] = []
    for row in payload.get("business_events") or []:
        event_id = _text(row.get("event_id"))
        binding = events.get(event_id)
        if not event_id or binding is None:
            excluded.append({
                "kind": "business_event", "id": event_id,
                "task_id": None,
                "reason": "event cannot be bound to a task; no outcome row written",
            })
            continue
        amount = row.get("amount")
        currency = row.get("currency")
        outcomes.append({
            "task_id": binding["task_id"],
            "outcome_id": event_id,
            "status": _text(row.get("status")) or DEFAULT_OUTCOME_STATUS,
            "verified": "false",
            "value": _text(amount),
            "currency": _text(currency),
            "accepted_by": "",
            "accepted_at": "",
            "source_ref": _text(row.get("source_ref"))
                          or "growovo-handoff:%s#%s" % (integrity, event_id),
        })

    manifest = {
        "schema": SCHEMA,
        "source_schema": growovo_handoff.SOURCE_SCHEMA,
        "run_id": run_id,
        "source_integrity_sha256": integrity,
        "normalized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task_bindings_source": bindings_source,
        "counts": {
            "tasks": len(tasks), "runs": len(runs), "outcomes": len(outcomes),
            "operations": len(payload.get("operations") or []),
            "business_events": len(payload.get("business_events") or []),
            "committed_operations": committed,
            "excluded": len(excluded),
        },
        "action_facts": action_facts,
        "excluded": excluded,
        "usage_not_derived": {
            "reason": "growovo_operations_carry_no_monetary_cost",
            "detail": ("a send is not usage cost; writing a cost-less usage row "
                       "would create a false 'unknown cost' finding"),
        },
        "booking_status": "evidence_only_not_canonical",
        "receipt_verified_by_verovo": False,
        "business_outcome_observed_by_verovo": False,
        "boundary": BOUNDARY,
        "warnings": warnings,
        "next_steps": [
            "Add tasks.csv, runs.jsonl and outcomes.csv to period-job.json sources.",
            "Confirm each field mapping by hand; the tables are evidence, not a booking.",
            "Verify a receipt or delivery independently before any acceptance or cost.",
        ],
    }
    return {"tasks": tasks, "runs": runs, "outcomes": outcomes,
            "manifest": manifest}


def _csv_text(header: Tuple[str, ...], rows: List[Dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(header), extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in header})
    return buffer.getvalue()


def _jsonl_text(rows: List[Dict[str, Any]]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                   for row in rows)


def _prepare_output(output_dir) -> Path:
    out = Path(output_dir)
    if out.exists():
        if not out.is_dir():
            raise HandoffNormalizeError(
                "output directory must be absent or empty: %s" % out)
        if any(out.iterdir()):
            raise HandoffNormalizeError(
                "output directory must be absent or empty: %s" % out)
    out.mkdir(parents=True, exist_ok=True)
    return out


def normalize_handoff_file(source, output_dir) -> Dict[str, Any]:
    """Validate ``source`` and write the Period Intake tables to a fresh dir."""
    source_path = Path(source).resolve()
    payload = growovo_handoff._read(source_path)
    tables = normalize_handoff(payload)
    out = _prepare_output(output_dir)
    files = {
        "tasks.csv": _csv_text(TASKS_HEADER, tables["tasks"]),
        "runs.jsonl": _jsonl_text(tables["runs"]),
        "outcomes.csv": _csv_text(OUTCOMES_HEADER, tables["outcomes"]),
        "manifest.json": json.dumps(tables["manifest"], ensure_ascii=False,
                                    indent=2, sort_keys=True) + "\n",
    }
    written: List[Path] = []
    try:
        for name, text in files.items():
            target = out / name
            growovo_handoff._atomic_write(target, text)
            written.append(target)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return {
        "schema": SCHEMA,
        "run_id": tables["manifest"]["run_id"],
        "output": str(out),
        "files": sorted(files),
        "counts": tables["manifest"]["counts"],
        "task_bindings_source": tables["manifest"]["task_bindings_source"],
        "booking_status": "evidence_only_not_canonical",
        "receipt_verified_by_verovo": False,
        "business_outcome_observed_by_verovo": False,
        "terminal_state": "DELIVERED_WITH_GAP",
    }
