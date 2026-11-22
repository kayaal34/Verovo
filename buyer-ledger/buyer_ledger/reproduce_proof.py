"""Read-only Prove Anything projection of a reproduce bundle.

The projection proves artifact integrity and the recorded replay verdict. It never
turns usage replay into delivery acceptance, external adoption or buyer approval.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from . import reproduce


class ProofExportError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _pick(source: Dict[str, Any], fields: tuple[str, ...]) -> Dict[str, Any]:
    return {field: source[field] for field in fields if field in source}


def _project_manifest(source: Dict[str, Any]) -> Dict[str, Any]:
    projected = _pick(source, (
        "schema", "generator", "generator_version", "generated_at", "sharing", "period",
        "closed_at", "previous_revision", "input_fingerprint", "revision_id", "rule_map",
        "replay_counts", "replay_metrics", "replay_integrity", "charge_verdict",
        "identity", "discrepancies",
    ))
    projected["files"] = [
        _pick(row, ("path", "role", "sha256", "bytes"))
        for row in source.get("files") or []
    ]
    return projected


def _project_replay(source: Dict[str, Any]) -> Dict[str, Any]:
    return _pick(source, (
        "schema", "generator", "generator_version", "generated_at", "input_fingerprint",
        "period", "rule_map", "counts", "metrics", "aggregation_state", "revision",
        "delivery_state", "delivery_note", "boundary",
    ))


def _check(check_id: str, status: str, claim: str, refs: list[str], reason: str = "") -> Dict[str, Any]:
    return {
        "check_id": check_id, "level": "P1" if check_id != "external-result" else "P4",
        "status": status, "claim": claim,
        "checker": {"name": "buyer_ledger.reproduce", "version": reproduce.GENERATOR_VERSION,
                    "independence": "independent_validator" if check_id == "replay-integrity"
                    else "executor_observed"},
        "evidence_refs": refs,
        "scope": "Only this reproduce bundle and its recorded input/rule versions",
        "reason": reason, "valid_until": None,
    }


def export_proof(source_dir: str | Path, out_dir: str | Path, *, by: str) -> Dict[str, Any]:
    if not by.strip():
        raise ProofExportError("REVIEW_REQUIRED: named proof reviewer is required")
    source = Path(source_dir).resolve()
    out = Path(out_dir).resolve()
    if out.exists():
        raise ProofExportError("OUTPUT_EXISTS: choose a new output directory")
    if out == source or source in out.parents:
        raise ProofExportError("OUTPUT_INSIDE_SOURCE: choose a directory outside the source bundle")
    verification = reproduce.verify_bundle(source)
    source_manifest = json.loads((source / reproduce.MANIFEST_NAME).read_text(encoding="utf-8"))
    source_replay = json.loads((source / "usage-replay.json").read_text(encoding="utf-8"))
    projections = {
        "manifest": _project_manifest(source_manifest),
        "replay": _project_replay(source_replay),
    }
    generated = _now()
    period = str((source_manifest.get("period") or {}).get("from") or "period")[:7]
    safe_period = "".join(c if c.isalnum() or c in "-_" else "-" for c in period)
    contract_id = "verovo-reproduce-%s" % safe_period
    trace_id = "trace-%s" % safe_period
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=out.name + ".tmp-", dir=str(out.parent)))
    try:
        artifact_ids = {"manifest": "manifest.json", "replay": "usage-replay.json"}
        evidence = []
        artifacts = []
        for evidence_id, name in artifact_ids.items():
            path = source / name
            if not path.is_file():
                raise ProofExportError("INPUT_REQUIRED: missing %s" % name)
            target = temp / "artifacts" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            _write(target, projections[evidence_id])
            digest = _sha(target)
            evidence.append({
                "evidence_id": evidence_id, "kind": "source_document",
                "source_ref": "artifacts/" + name, "source_hash": digest,
                "observed_at": generated, "collected_by": by, "trust": "observed",
            })
            artifacts.append({"artifact_id": evidence_id, "path": "artifacts/" + name,
                              "sha256": digest,
                              "classification": (source_manifest.get("sharing") or {}).get(
                                  "data_classification") or "unknown"})
        verdict = verification.get("verdict")
        trace_status = "ok" if verdict == "PASS" else ("failed" if verdict == "FAIL" else "unknown")
        step = {
            "step_id": "verify-reproduce", "action": "verify_reproduce_bundle",
            "started_at": generated, "ended_at": generated, "status": trace_status,
            "tool": "buyer_ledger.reproduce.verify_bundle", "output_ref": "artifacts/usage-replay.json",
        }
        checks = [
            _check("artifact-integrity", "PASS" if verdict == "PASS" else "FAIL",
                   "Manifest and replay artifacts are present and their hashes can be checked",
                   ["manifest", "replay"]),
            _check("replay-integrity", "PASS" if verdict == "PASS" else
                   ("FAIL" if verdict == "FAIL" else "INDETERMINATE"),
                   "The reproduce verifier reports the recorded replay integrity verdict",
                   ["manifest", "replay"],
                   "" if verdict == "PASS" else "The source verifier did not return PASS"),
            _check("external-result", "UNCHECKED",
                   "External acceptance, adoption and buyer action are not observed by this projection", [],
                   "Usage replay proves neither delivery acceptance nor an external result"),
        ]
        contract = {
            "schema": "ovo.task-contract/1", "contract_id": contract_id, "version": "1",
            "created_at": generated, "owner": by, "product": "verovo",
            "goal": "Independently inspect a Verovo usage-replay bundle and its integrity verdict",
            "inputs": [{"ref": "artifacts/manifest.json", "kind": "reproduce_manifest"},
                       {"ref": "artifacts/usage-replay.json", "kind": "usage_replay"}],
            "deliverables": [{"id": "proof", "description": "Portable replay-integrity projection",
                              "acceptance_criteria": ["Artifacts and replay verdict remain inspectable"],
                              "out_of_scope": ["Delivery acceptance", "Buyer action", "External adoption"]}],
            "approved_by": by,
        }
        bundle = {
            "schema": "ovo.acceptance-bundle/1", "task_contract": contract,
            "execution_trace": {"schema": "ovo.execution-trace/1", "trace_id": trace_id,
                                "contract_id": contract_id, "started_at": generated,
                                "finished_at": generated, "actor": "buyer_ledger reproduce proof",
                                "steps": [step]},
            "evidence_ledger": {"schema": "ovo.evidence-ledger/1", "ledger_id": "ledger-" + safe_period,
                                "contract_id": contract_id, "entries": evidence},
            "acceptances": [{"schema": "ovo.acceptance-record/1", "acceptance_id": "accept-proof",
                             "contract_id": contract_id, "trace_id": trace_id, "deliverable_id": "proof",
                             "criterion": "Artifacts and replay verdict remain inspectable",
                             "verdict": "PASS", "evidence_refs": ["manifest", "replay"],
                             "checked_by": by, "checked_at": generated,
                             "notes": "This accepts the projection only, not the underlying business task"}],
            "escalations": [],
        }
        replay = {
            "schema": "ovo.replay-manifest/1", "contract_id": contract_id,
            "status": "UNAVAILABLE",
            "commands": [{"name": "validate-projection",
                          "argv": ["python3", "-m", "ovo_acceptance", "proof", "${PROOF_DIR}"]}],
            "inputs": [], "missing": ["self-contained source inputs are not copied into this projection"],
        }
        proof = {
            "schema": "ovo.proof/1", "proof_id": "proof-" + safe_period,
            "contract_id": contract_id, "product": "verovo", "generated_at": generated,
            "environment": {"generator": reproduce.GENERATOR,
                            "generator_version": reproduce.GENERATOR_VERSION},
            "artifacts": artifacts,
            "replay": {"manifest": "replay/manifest.json", "status": "UNAVAILABLE",
                       "missing": list(replay["missing"])},
            "limitations": ["Usage replay is not delivery acceptance or an external result",
                            "The projection does not copy the source's private input files",
                            "Source paths, row-level events and subjects are excluded by a field whitelist",
                            "No causal or commercial claim is made"],
        }
        _write(temp / "bundle.json", bundle)
        _write(temp / "contract.json", contract)
        _write(temp / "checks.json", {"schema": "ovo.proof-checks/1",
                                      "contract_id": contract_id, "checks": checks})
        _write(temp / "proof.json", proof)
        _write(temp / "replay/manifest.json", replay)
        (temp / "trace.jsonl").write_text(json.dumps(step, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.rename(out)
        return {"status": "exported", "out": str(out), "source_verdict": verdict,
                "source_mutated": False, "external_action_executed": False,
                "causality": "not_claimed"}
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
