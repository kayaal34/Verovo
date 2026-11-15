"""V-04 period revisions: append-only, independently recalculated versions.

The V-02/V-03 root layout stays the first version.  On the first revision the
root is snapshotted into ``revisions/r1`` and every later version gets its own
``revisions/rN`` directory; nothing in an older revision is rewritten.  A
revision stores the canonical bundle, calculation, report and review/signoff
sidecars it produced, so a later source change cannot move an earlier signoff.

The delta separates three things that are easy to conflate:

- amount / cause / completion-axis changes;
- newly available evidence (a source or verdict that did not exist before);
- credit **applied** (reduces the amount due) versus credit merely **issued**
  (open exposure, never realized savings).  The recovery/settlement vocabulary
  is aligned with ``credited``/``paid`` meaning realized cash only when the
  credit was actually applied; issuance alone stays pending.
"""
from __future__ import annotations

import json
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import close_report
from .close import (CloseError, confirm_mapping, init_case, ingest,
                    load_period_job)

REVISION_SCHEMA = "verovo.period-revision/1"
DELTA_SCHEMA = "verovo.period-revision-delta/1"
SIGNOFF_SCHEMA = "verovo.close-signoff/1"
INDEX_SCHEMA = "verovo.period-revision-index/1"
REVISION_DIR = "revisions"

AMOUNT_KEYS = (
    "invoice_total", "rule_repriced", "matched_postings",
    "distinct_source_reconciled", "known_adjustments", "open_difference",
    "credits_applied", "credits_pending", "realized_savings",
)
AXES = ("finance_status", "verification_status", "delivery_status",
        "economics_status")
CREDIT_TYPES = ("credit", "refund", "discount")


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path, label: str) -> Dict[str, Any]:
    if not path.is_file():
        raise CloseError("INPUT_REQUIRED", "%s not found: %s" % (label, path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CloseError("INPUT_REQUIRED", "%s is invalid JSON: %s"
                         % (label, exc)) from exc
    if not isinstance(data, dict):
        raise CloseError("INPUT_REQUIRED", "%s must be an object" % label)
    return data


def _write_json(path: Path, payload: Any) -> None:
    close_report._write_json(path, payload)


def _sha_file(path: Path) -> str:
    return close_report._sha_file(path)


def _fingerprint(payload: Any) -> str:
    return close_report._fingerprint(payload)


def _money(value: Optional[Decimal]) -> Optional[str]:
    return close_report._money(value)


def _dec(value: Any, label: str) -> Optional[Decimal]:
    return close_report._dec(value, label)


def _revisions_dir(case_dir: Path) -> Path:
    return Path(case_dir) / REVISION_DIR


def _index_path(case_dir: Path) -> Path:
    return _revisions_dir(case_dir) / "index.json"


def _load_index(case_dir: Path) -> Dict[str, Any]:
    path = _index_path(case_dir)
    if not path.is_file():
        return {"schema": INDEX_SCHEMA, "revisions": [], "latest": None}
    data = _read_json(path, "revisions/index.json")
    if not isinstance(data.get("revisions"), list):
        raise CloseError("INPUT_REQUIRED", "revisions/index.json needs revisions")
    return data


def _write_index(case_dir: Path, index: Dict[str, Any]) -> None:
    _write_json(_index_path(case_dir), index)


def _revision_dir(case_dir: Path, revision_id: str) -> Path:
    return _revisions_dir(case_dir) / revision_id


def _case_id(case_dir: Path) -> str:
    return _text(load_period_job(Path(case_dir) / "period-job.json").get("job_id"))


def _bundle_root(case_dir: Path) -> Optional[Path]:
    scan_path = Path(case_dir) / "intake" / "scan.json"
    if not scan_path.is_file():
        return None
    scan = json.loads(scan_path.read_text(encoding="utf-8"))
    root = Path(_text(scan.get("bundle_root")))
    if root.is_dir():
        return root
    local = Path(case_dir) / "bundle"
    return local if local.is_dir() else None


def _rewrite_bundle_root(case_dir: Path) -> None:
    scan_path = Path(case_dir) / "intake" / "scan.json"
    if not scan_path.is_file():
        return
    scan = json.loads(scan_path.read_text(encoding="utf-8"))
    bundle = Path(case_dir) / "bundle"
    scan["bundle_root"] = str(bundle)
    _write_json(scan_path, scan)


def _source_snapshot(case_dir: Path) -> List[Dict[str, Any]]:
    scan = _read_json(Path(case_dir) / "intake" / "scan.json", "intake/scan.json")
    snapshot = []
    for role, records in (scan.get("sources") or {}).items():
        for record in records:
            snapshot.append({
                "role": _text(role),
                "path": _text(record.get("path")),
                "sha256": _text(record.get("sha256")),
            })
    snapshot.sort(key=lambda item: (item["role"], item["path"]))
    return snapshot


def _input_snapshot_hash(case_dir: Path) -> str:
    """Semantic input identity; formatting-only job changes stay idempotent."""
    job = load_period_job(Path(case_dir) / "period-job.json")
    identity = {
        "job_id": job["job_id"],
        "buyer": _text(job.get("buyer")),
        "vendor": job["vendor"],
        "account": job["account"],
        "currency": job["currency"],
        "period": dict(job["period"]),
        "rule_map": job.get("rule_map"),
        "task_economics_requested": job.get("task_economics_requested") is True,
        "sources": _source_snapshot(case_dir),
    }
    return _fingerprint(identity)


def _rule_map_hash(case_dir: Path) -> str:
    path = Path(case_dir) / "canonical" / "rule-map.json"
    return _sha_file(path) if path.is_file() else ""


def _report(case_dir: Path) -> Optional[Dict[str, Any]]:
    path = Path(case_dir) / "report" / "report.json"
    return _read_json(path, "report/report.json") if path.is_file() else None


def _review(case_dir: Path) -> Optional[Dict[str, Any]]:
    path = Path(case_dir) / "review.json"
    return _read_json(path, "review.json") if path.is_file() else None


def _signoff(case_dir: Path) -> Optional[Dict[str, Any]]:
    path = Path(case_dir) / "signoff.json"
    return _read_json(path, "signoff.json") if path.is_file() else None


def _status(case_dir: Path) -> str:
    signoff = _signoff(case_dir)
    if signoff:
        return _text(signoff.get("state")) or "signed"
    if _review(case_dir) is not None:
        return "reviewed"
    if _report(case_dir) is not None:
        return "report_ready"
    if (Path(case_dir) / "calculation.json").is_file():
        return "calculated"
    return "draft"


def _build_record(case_dir: Path, revision_id: str,
                  parent_revision_id: Optional[str], source_added: List[str],
                  source_removed: List[str], source_changed: List[str],
                  reason: str = "", created_at: str = "") -> Dict[str, Any]:
    case_dir = Path(case_dir)
    job = load_period_job(case_dir / "period-job.json")
    calculation = (_read_json(case_dir / "calculation.json", "calculation.json")
                   if (case_dir / "calculation.json").is_file() else None)
    report = _report(case_dir)
    review = _review(case_dir)
    signoff = _signoff(case_dir)
    confirmation = (_read_json(case_dir / "intake" / "mapping-confirmed.json",
                               "intake/mapping-confirmed.json")
                    if (case_dir / "intake" / "mapping-confirmed.json").is_file()
                    else None)
    reviewer = ""
    if signoff:
        reviewer = _text(signoff.get("reviewer"))
    elif review:
        reviewer = _text(review.get("reviewer"))
    elif confirmation:
        reviewer = _text(confirmation.get("confirmed_by"))
    return {
        "schema": REVISION_SCHEMA,
        "period_job_id": job["job_id"],
        "revision_id": revision_id,
        "parent_revision_id": parent_revision_id,
        "input_snapshot_hash": _input_snapshot_hash(case_dir),
        "rule_map_hash": _rule_map_hash(case_dir),
        "source_snapshot": _source_snapshot(case_dir),
        "source_added": sorted(source_added),
        "source_removed": sorted(source_removed),
        "source_changed": sorted(source_changed),
        "reviewer": reviewer or None,
        "closed_at": (signoff or {}).get("decided_at") or None,
        "status": _status(case_dir),
        "reason": _text(reason),
        "created_at": created_at or _now(),
        "report_fingerprint": ((report or {}).get("replay") or {}).get(
            "output_snapshot_fingerprint"),
        "calculation_fingerprint": (calculation or {}).get(
            "calculation_fingerprint"),
        "canonical_snapshot_hash": ((calculation or {}).get(
            "canonical_snapshot") or {}).get("fingerprint"),
        "signoff": signoff,
    }


def _summary(record: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("revision_id", "parent_revision_id", "status",
            "input_snapshot_hash", "rule_map_hash", "created_at", "closed_at",
            "reviewer", "source_added", "source_removed", "source_changed")
    return {key: record.get(key) for key in keys}


def _ensure_root_snapshot(case_dir: Path) -> Dict[str, Any]:
    case_dir = Path(case_dir)
    index = _load_index(case_dir)
    if index.get("revisions"):
        return index
    if not (case_dir / "report" / "report.json").is_file():
        raise CloseError("INPUT_REQUIRED",
                         "the current period has no report; run close calculate "
                         "and close report before creating a revision")
    root = _revisions_dir(case_dir) / "r1"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for name in ("period-job.json", "intake", "canonical", "calculation.json",
                 "report", "review.json", "signoff.json"):
        source = case_dir / name
        if source.is_dir():
            shutil.copytree(source, root / name, symlinks=True)
        elif source.is_file():
            shutil.copy2(source, root / name)
    bundle = _bundle_root(case_dir)
    if bundle is not None:
        shutil.copytree(bundle, root / "bundle", symlinks=True)
        _rewrite_bundle_root(root)
    record = _build_record(root, "r1", None,
                           [item["path"] for item in _source_snapshot(root)],
                           [], [], reason="initial snapshot")
    _write_json(root / "revision.json", record)
    index = {
        "schema": INDEX_SCHEMA,
        "case_id": _case_id(case_dir),
        "revisions": [_summary(record)],
        "latest": "r1",
    }
    _write_index(case_dir, index)
    return index


def _validate_same_identity(parent: Dict[str, Any], child: Dict[str, Any]) -> None:
    for field in ("job_id", "buyer", "vendor", "account", "currency"):
        if _text(parent.get(field)) != _text(child.get(field)):
            raise CloseError("UNSUPPORTED_SCOPE",
                             "a revision cannot change %s (%r -> %r)"
                             % (field, parent.get(field), child.get(field)))
    for field in ("from", "to", "to_exclusive", "timezone"):
        if parent["period"].get(field) != child["period"].get(field):
            raise CloseError("UNSUPPORTED_SCOPE",
                             "a revision cannot change period.%s" % field)


def _diff_sources(before: List[Dict[str, Any]],
                  after: List[Dict[str, Any]]) -> Tuple[List[str], List[str], List[str]]:
    def keyed(items: List[Dict[str, Any]]) -> Dict[Tuple[str, str], str]:
        return {(item["role"], item["path"]): item["sha256"] for item in items}

    old, new = keyed(before), keyed(after)
    added = sorted(path for role, path in new if (role, path) not in old)
    removed = sorted(path for role, path in old if (role, path) not in new)
    changed = sorted(path for (role, path), sha in new.items()
                     if (role, path) in old and old[(role, path)] != sha)
    return added, removed, changed


def _parent_control_domains(parent_dir: Path,
                            parent_job: Dict[str, Any],
                            new_job: Dict[str, Any]) -> Dict[str, str]:
    """Inherit a control domain only for an unchanged role/path source.

    A revised export from the same source keeps its control domain; a new or
    renamed source must be resolved explicitly (``--control-domain``).
    """
    path = parent_dir / "intake" / "mapping-confirmed.json"
    if not path.is_file():
        return {}
    confirmation = json.loads(path.read_text(encoding="utf-8"))
    parent_paths = {entry["role"]: entry["path"] for entry in parent_job["sources"]}
    new_paths = {entry["role"]: entry["path"] for entry in new_job["sources"]}
    domains = {}
    for role, entry in (confirmation.get("sources") or {}).items():
        if not isinstance(entry, dict) or not _text(entry.get("control_domain")):
            continue
        if parent_paths.get(role) == new_paths.get(role):
            domains[role] = _text(entry["control_domain"])
    return domains


def revise(case_dir: Path, *, bundle: Path = None, source: Path = None,
           role: str = "", reason: str = "", reviewer: str,
           control_domains: Dict[str, str] = None) -> Dict[str, Any]:
    """Create a new independent revision; same input is a no-op."""
    case_dir = Path(case_dir)
    reviewer = _text(reviewer)
    if not reviewer:
        raise CloseError("INPUT_REQUIRED",
                         "a revision needs --review-by; no mapping is auto-confirmed")
    if bool(bundle) == bool(source):
        raise CloseError("INPUT_REQUIRED",
                         "provide exactly one of --bundle or --source")
    index = _ensure_root_snapshot(case_dir)
    parent_id = index["latest"]
    parent_dir = _revision_dir(case_dir, parent_id)
    parent_record = _read_json(parent_dir / "revision.json", "revision.json")
    parent_job = load_period_job(parent_dir / "period-job.json")
    parent_bundle = _bundle_root(parent_dir)
    if parent_bundle is None:
        raise CloseError("STALE_SOURCE",
                         "the parent revision has no available bundle snapshot")
    next_id = "r%d" % (len(index["revisions"]) + 1)
    staging = _revisions_dir(case_dir) / (".staging-" + next_id)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    job = deepcopy(parent_job)

    if bundle is not None:
        new_bundle = Path(bundle)
        if not new_bundle.is_dir():
            raise CloseError("INPUT_REQUIRED", "revision bundle not found: %s"
                             % new_bundle)
        shutil.copytree(new_bundle, staging / "bundle", symlinks=True)
        new_job_path = staging / "bundle" / "period-job.json"
        if new_job_path.is_file():
            job = load_period_job(new_job_path)
            _validate_same_identity(parent_job, job)
    else:
        source_path = Path(source)
        if not source_path.is_file():
            raise CloseError("INPUT_REQUIRED", "revision source not found: %s"
                             % source_path)
        shutil.copytree(parent_bundle, staging / "bundle", symlinks=True)
        entries = job["sources"]
        if role:
            matches = [entry for entry in entries if entry["role"] == role]
        else:
            matches = [entry for entry in entries
                       if Path(entry["path"]).name == source_path.name]
        if len(matches) != 1:
            raise CloseError("INPUT_REQUIRED",
                             "cannot identify the source role; pass --role "
                             "(matches: %d)" % len(matches))
        entry = matches[0]
        old_rel = entry["path"]
        new_rel = (old_rel if Path(old_rel).name == source_path.name else
                   (Path(old_rel).parent / source_path.name).as_posix())
        target = staging / "bundle" / new_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target)
        if new_rel != old_rel:
            stale = staging / "bundle" / old_rel
            if stale.is_file():
                stale.unlink()
        entry["path"] = new_rel
        entry["sha256"] = _sha_file(target)

    (staging / "new-job.json").write_text(
        json.dumps(job, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    init_case(staging, job_path=staging / "new-job.json")
    (staging / "new-job.json").unlink()
    ingest(staging, bundle=staging / "bundle")
    domains = _parent_control_domains(parent_dir, parent_job, job)
    domains.update(control_domains or {})
    confirm_mapping(staging, reviewer=reviewer, control_domains=domains)
    close_report.calculate(staging)
    close_report.build_report(staging)

    added, removed, changed = _diff_sources(
        parent_record.get("source_snapshot") or _source_snapshot(parent_dir),
        _source_snapshot(staging))
    record = _build_record(staging, next_id, parent_id, added, removed, changed,
                           reason=reason)
    if (record["input_snapshot_hash"] == parent_record["input_snapshot_hash"]
            and record["rule_map_hash"] == parent_record["rule_map_hash"]):
        shutil.rmtree(staging)
        return {"revision": parent_record, "delta": None, "idempotent": True}
    final = _revision_dir(case_dir, next_id)
    if final.exists():
        shutil.rmtree(staging)
        raise CloseError("REVIEW_REQUIRED",
                         "revision %s already exists; no version is overwritten"
                         % next_id)
    staging.rename(final)
    _rewrite_bundle_root(final)
    record = _build_record(final, next_id, parent_id, added, removed, changed,
                           reason=reason)
    _write_json(final / "revision.json", record)
    delta = _build_delta_dirs(parent_dir, final)
    _write_json(final / "delta.json", delta)
    index["revisions"].append(_summary(record))
    index["latest"] = next_id
    _write_index(case_dir, index)
    return {"revision": record, "delta": delta, "idempotent": False,
            "path": str(final)}


def _amount_change(before: Any, after: Any, key: str) -> Dict[str, Any]:
    a = _dec(before, "%s before" % key)
    b = _dec(after, "%s after" % key)
    if a is None or b is None:
        return {"before": before, "after": after, "delta": None,
                "status": "unknown"}
    return {"before": _money(a), "after": _money(b),
            "delta": _money(b - a), "status": "comparable"}


def _findings(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {"%s|%s" % (item.get("kind"), item.get("line_ref") or ""): item
            for item in report.get("findings") or []}


def _verdict_changes(before: Dict[str, Any],
                     after: Dict[str, Any]) -> List[Dict[str, Any]]:
    changes = []
    before_verdicts = before.get("verdicts") or {}
    after_verdicts = after.get("verdicts") or {}
    for name in after_verdicts:
        old = _text((before_verdicts.get(name) or {}).get("status"))
        new = _text((after_verdicts.get(name) or {}).get("status"))
        if old != new:
            changes.append({"verdict": name, "before": old, "after": new})
    return changes


def _credit_lines(report: Dict[str, Any]) -> Tuple[List[Dict[str, Any]],
                                                   List[Dict[str, Any]]]:
    applied, pending = [], []
    for line in report.get("line_reconciliation") or []:
        if line.get("adjustment_type") not in CREDIT_TYPES:
            continue
        item = {
            "line_ref": line.get("line_ref"),
            "type": line.get("adjustment_type"),
            "state": line.get("adjustment_state"),
            "amount": line.get("amount"),
            "currency": line.get("currency"),
        }
        if line.get("classification") == "explained_adjustment":
            applied.append(item)
        elif line.get("classification") == "excluded_pending_adjustment":
            pending.append(item)
    return applied, pending


def _build_delta_dirs(before_dir: Path, after_dir: Path) -> Dict[str, Any]:
    before = _read_json(before_dir / "report" / "report.json", "report/report.json")
    after = _read_json(after_dir / "report" / "report.json", "report/report.json")
    before_amounts = before.get("amounts") or {}
    after_amounts = after.get("amounts") or {}
    amounts = {key: _amount_change(before_amounts.get(key),
                                   after_amounts.get(key), key)
               for key in AMOUNT_KEYS}
    axes = {axis: {"before": before.get(axis), "after": after.get(axis)}
            for axis in AXES}
    changed_axes = [axis for axis in AXES
                    if axes[axis]["before"] != axes[axis]["after"]]

    before_findings = _findings(before)
    after_findings = _findings(after)
    new_findings = sorted(set(after_findings) - set(before_findings))
    cleared_findings = sorted(set(before_findings) - set(after_findings))
    changed_findings = []
    for key in sorted(set(before_findings) & set(after_findings)):
        old, new = before_findings[key], after_findings[key]
        if (old.get("state") != new.get("state")
                or old.get("confirmed_cause") != new.get("confirmed_cause")
                or old.get("candidate_causes") != new.get("candidate_causes")):
            changed_findings.append({
                "key": key,
                "before": {"state": old.get("state"),
                           "confirmed_cause": old.get("confirmed_cause")},
                "after": {"state": new.get("state"),
                          "confirmed_cause": new.get("confirmed_cause")},
            })

    before_sources = {(item["role"], item["path"]): item["sha256"]
                      for item in _source_snapshot(before_dir)}
    after_sources = {(item["role"], item["path"]): item["sha256"]
                     for item in _source_snapshot(after_dir)}
    new_sources = sorted(path for role, path in after_sources
                         if (role, path) not in before_sources)
    removed_sources = sorted(path for role, path in before_sources
                             if (role, path) not in after_sources)
    changed_sources = sorted(path for (role, path), sha in after_sources.items()
                             if (role, path) in before_sources
                             and before_sources[(role, path)] != sha)
    verdict_changes = _verdict_changes(before, after)
    newly_available = list(new_sources)
    for change in verdict_changes:
        if (change["before"] in ("UNPROVABLE", "UNKNOWN", "not_applicable", "")
                and change["after"] in ("PASS", "FAIL")):
            newly_available.append("%s:%s" % (change["verdict"], change["after"]))

    before_applied, before_pending = _credit_lines(before)
    after_applied, after_pending = _credit_lines(after)
    applied_keys = {(item["line_ref"], item["amount"]) for item in before_applied}
    applied_this_revision = [item for item in after_applied
                             if (item["line_ref"], item["amount"])
                             not in applied_keys]
    pending_keys = {(item["line_ref"], item["type"], item["amount"],
                     item["state"]) for item in before_pending}
    issued_this_revision = [
        item for item in after_pending
        if (item["line_ref"], item["type"], item["amount"], item["state"])
        not in pending_keys]
    credits = {
        "applied": _amount_change(before_amounts.get("credits_applied"),
                                  after_amounts.get("credits_applied"),
                                  "credits_applied"),
        "pending": _amount_change(before_amounts.get("credits_pending"),
                                  after_amounts.get("credits_pending"),
                                  "credits_pending"),
        "realized": _amount_change(before_amounts.get("realized_savings"),
                                   after_amounts.get("realized_savings"),
                                   "realized_savings"),
        "issued_not_applied": after_pending,
        "issued_this_revision": issued_this_revision,
        "applied_this_revision": applied_this_revision,
        "classification": {
            "amount_changes": [key for key, value in amounts.items()
                               if value["delta"] not in (None, "0", "0.00")],
            "cause_changes": sorted(set(new_findings + cleared_findings)
                                    | {item["key"] for item in changed_findings}),
            "evidence_added": newly_available,
            "credit_applied": applied_this_revision,
            "credit_issued_not_applied": issued_this_revision,
        },
    }
    return {
        "schema": DELTA_SCHEMA,
        "from_revision": before_dir.name,
        "to_revision": after_dir.name,
        "amounts": amounts,
        "axes": axes,
        "changed_axes": changed_axes,
        "causes": {
            "new_findings": new_findings,
            "cleared_findings": cleared_findings,
            "changed_findings": changed_findings,
        },
        "evidence": {
            "new_sources": new_sources,
            "removed_sources": removed_sources,
            "changed_sources": changed_sources,
            "verdict_changes": verdict_changes,
            "newly_available": newly_available,
        },
        "credits": credits,
        "rules": {
            "before": _rule_map_hash(before_dir),
            "after": _rule_map_hash(after_dir),
            "changed": _rule_map_hash(before_dir) != _rule_map_hash(after_dir),
        },
        "revision": {
            "before": _read_json(before_dir / "revision.json",
                                 "revision.json").get("status"),
            "after": _read_json(after_dir / "revision.json",
                                "revision.json").get("status"),
        },
        "classification": {
            "amount_changes": credits["classification"]["amount_changes"],
            "cause_changes": credits["classification"]["cause_changes"],
            "evidence_added": credits["classification"]["evidence_added"],
            "credit_applied": applied_this_revision,
            "credit_issued_not_applied": issued_this_revision,
        },
    }


def build_delta(case_dir: Path, from_revision: str,
                to_revision: str) -> Dict[str, Any]:
    case_dir = Path(case_dir)
    return _build_delta_dirs(_revision_dir(case_dir, from_revision),
                             _revision_dir(case_dir, to_revision))


def load_revision(case_dir: Path, revision_id: str) -> Dict[str, Any]:
    return _read_json(_revision_dir(Path(case_dir), revision_id) / "revision.json",
                      "revision.json")


def list_revisions(case_dir: Path) -> List[Dict[str, Any]]:
    case_dir = Path(case_dir)
    records = []
    for summary in _load_index(case_dir).get("revisions") or []:
        revision_id = _text(summary.get("revision_id"))
        path = _revision_dir(case_dir, revision_id) / "revision.json"
        records.append(_read_json(path, "revision.json")
                       if path.is_file() else summary)
    return records


def latest_revision(case_dir: Path) -> Optional[Dict[str, Any]]:
    index = _load_index(Path(case_dir))
    latest = index.get("latest")
    return load_revision(case_dir, latest) if latest else None


def resolve_case_dir(case_dir: Path, revision_id: str = "") -> Path:
    case_dir = Path(case_dir)
    index = _load_index(case_dir)
    if revision_id:
        if revision_id == "r1" and not index.get("revisions"):
            # Before the first revise, the root layout *is* r1.
            return case_dir
        target = _revision_dir(case_dir, revision_id)
        if not target.is_dir():
            raise CloseError("INPUT_REQUIRED", "revision not found: %s"
                             % revision_id)
        return target
    latest = index.get("latest")
    if latest:
        return _revision_dir(case_dir, latest)
    return case_dir


def signoff(case_dir: Path, *, reviewer: str, approved_scope: str = "",
            reservations: List[str] = None, note: str = "",
            revision_id: str = "") -> Dict[str, Any]:
    """Record a named period signoff; never mutates the report or closes items."""
    case_dir = Path(case_dir)
    reviewer = _text(reviewer)
    if not reviewer:
        raise CloseError("INPUT_REQUIRED",
                         "signoff needs --by; an Agent cannot sign for a human")
    target = resolve_case_dir(case_dir, revision_id)
    if (target / "signoff.json").is_file():
        raise CloseError("REVIEW_REQUIRED",
                         "this revision is already signed; a later source must "
                         "create a new revision, never overwrite the signoff")
    report = _read_json(target / "report" / "report.json", "report/report.json")
    unresolved = [item for item in report.get("open_items") or []
                  if item.get("state") not in ("resolved", "closed",
                                               "not_applicable")]
    revision = _read_json(target / "revision.json", "revision.json") \
        if (target / "revision.json").is_file() else None
    state = "signed_with_open_items" if unresolved else "signed"
    decided_at = _now()
    source_hashes = list(report.get("source_hashes") or [])
    source_snapshot = _source_snapshot(target)
    rule_map_hash = _text(report.get("rule_map_hash"))
    scope_hash = _fingerprint({
        "case_id": report.get("job_id"),
        "revision_id": (revision or {}).get("revision_id", "r1"),
        "approved_scope": _text(approved_scope),
        "source_hashes": source_hashes,
        "source_snapshot": source_snapshot,
        "rule_map_hash": rule_map_hash,
    })
    record = {
        "schema": SIGNOFF_SCHEMA,
        "job_id": report.get("job_id"),
        "revision_id": (revision or {}).get("revision_id", "r1"),
        "state": state,
        "reviewer": reviewer,
        "decided_at": decided_at,
        "attestation": "attested_by_declaration",
        "approved_scope": _text(approved_scope),
        "reservations": [item for item in (reservations or []) if _text(item)],
        "note": _text(note),
        "scope_hash": scope_hash,
        "source_hashes": source_hashes,
        "source_snapshot": source_snapshot,
        "rule_map_hash": rule_map_hash,
        "report_fingerprint": (report.get("replay") or {}).get(
            "output_snapshot_fingerprint"),
        "unresolved_items": [{
            "id": item.get("id"), "kind": item.get("kind"),
            "line_ref": item.get("line_ref"), "state": item.get("state"),
            "amount": item.get("amount"), "currency": item.get("currency"),
            "owner": item.get("owner"), "next_evidence": item.get("next_evidence"),
        } for item in unresolved],
        "payment_authorization": False,
        "boundary": ("A named period signoff is a declaration over this revision's "
                     "source and rule fingerprints. It is not payment "
                     "authorization, does not close unresolved items, and does not "
                     "transfer to a later revision."),
    }
    _write_json(target / "signoff.json", record)
    if revision is not None:
        revision["status"] = state
        revision["closed_at"] = decided_at
        revision["reviewer"] = reviewer
        revision["signoff"] = record
        _write_json(target / "revision.json", revision)
        index = _load_index(case_dir)
        for item in index.get("revisions") or []:
            if item.get("revision_id") == revision["revision_id"]:
                item["status"] = state
                item["closed_at"] = decided_at
                item["reviewer"] = reviewer
                break
        _write_index(case_dir, index)
    return record
