from __future__ import annotations

import hashlib
import copy
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import __version__, canonical_finance, period_intake


class ProofExportError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bytes(value: Dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _hash_file(path: Path) -> str:
    return _hash_bytes(path.read_bytes())


def semantic_fingerprint(result: Dict[str, Any]) -> str:
    normalized = copy.deepcopy(result)
    intake_report = normalized.get("intake_report")
    if isinstance(intake_report, dict):
        intake_report.pop("generated_at", None)
    payload = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return _hash_bytes(payload)


def _safe_id(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in value).strip("-") or "case"


def _write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_bytes(value))


def _status(checks: List[Dict[str, Any]]) -> str:
    statuses = {item["status"] for item in checks}
    if "FAIL" in statuses:
        return "FAIL"
    if statuses & {"UNCHECKED", "INDETERMINATE"}:
        return "BLOCKED"
    if "EXPIRED" in statuses:
        return "EXPIRED"
    return "PASS"


def _mapped_status(value: str) -> str:
    if value in ("PASS", "FAIL"):
        return value
    if value in ("UNKNOWN", "UNPROVABLE"):
        return "INDETERMINATE"
    return "UNCHECKED"


def _check(check_id: str, level: str, status: str, claim: str, independence: str,
           refs: List[str], scope: str, reason: str = "") -> Dict[str, Any]:
    return {
        "check_id": check_id, "level": level, "status": status, "claim": claim,
        "checker": {"name": "verovo canonical_finance", "version": __version__,
                    "independence": independence},
        "evidence_refs": refs, "scope": scope, "reason": reason, "valid_until": None,
    }


def _source_inputs(bundle: Path, manifest: Dict[str, Any]) -> List[Tuple[str, Path]]:
    values = [("manifest.json", bundle / "manifest.json")]
    if manifest.get("rule_map"):
        values.append((str(manifest["rule_map"]), bundle / str(manifest["rule_map"])))
        rule_map = json.loads((bundle / str(manifest["rule_map"])).read_text(encoding="utf-8"))
        for rule in rule_map.get("rules") or []:
            driver = rule.get("driver") if isinstance(rule, dict) else None
            source_ref = str((driver or {}).get("source_ref") or "")
            if source_ref:
                relative = source_ref.split("#", 1)[0]
                values.append((relative, bundle / relative))
    for entries in (manifest.get("sources") or {}).values():
        for entry in entries:
            values.append((str(entry["path"]), bundle / str(entry["path"])))
    unique = []
    seen = set()
    for relative, path in values:
        if relative in seen:
            continue
        seen.add(relative)
        resolved = path.resolve()
        try:
            resolved.relative_to(bundle.resolve())
        except ValueError as exc:
            raise ProofExportError("UNSAFE_PATH: period source escapes its bundle") from exc
        if not resolved.is_file():
            raise ProofExportError("INPUT_REQUIRED: missing period source %s" % relative)
        unique.append((relative, resolved))
    return unique


def export_proof(bundle_dir: str | Path, out_dir: str | Path, *, by: str,
                 classification: str = "private") -> Dict[str, Any]:
    if not by.strip():
        raise ProofExportError("REVIEW_REQUIRED: named proof reviewer is required")
    if classification == "public-redacted":
        raise ProofExportError(
            "PUBLIC_PROJECTION_REQUIRED: canonical public-redacted export is refused until a whitelist projection exists")
    if classification not in ("private", "synthetic"):
        raise ProofExportError("INPUT_REQUIRED: unknown classification")
    bundle_dir = Path(bundle_dir).resolve()
    out_dir = Path(out_dir).resolve()
    if out_dir.exists():
        raise ProofExportError("OUTPUT_EXISTS: choose a new output directory")
    manifest = period_intake.load_manifest(bundle_dir)
    sources = _source_inputs(bundle_dir, manifest)
    result = canonical_finance.run(bundle_dir)
    generated_at = _now()
    case_id = str(result["case_id"])
    safe_case = _safe_id(case_id)
    contract_id = "verovo-%s-proof" % safe_case
    trace_id = "trace-%s-proof" % safe_case
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=out_dir.name + ".tmp-", dir=str(out_dir.parent)))
    try:
        canonical_path = temp / "artifacts" / "canonical.json"
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_path.write_bytes(_bytes(result))
        canonical_hash = _hash_file(canonical_path)
        buyer = manifest.get("buyer") or {}
        criterion = "Canonical 账期产物、来源哈希、四完成轴和重放清单可读取"
        contract = {
            "schema": "ovo.task-contract/1", "contract_id": contract_id, "version": "1",
            "created_at": generated_at, "owner": str(buyer.get("name") or buyer.get("org_id") or "buyer"),
            "product": "verovo",
            "goal": "复核供应商 %s、账户 %s、币种 %s 的一个账期" % (
                manifest.get("vendor"), manifest.get("account_ref"), manifest.get("currency")),
            "inputs": [{"ref": relative, "kind": "period_source"} for relative, _ in sources],
            "deliverables": [{
                "id": "canonical-period-proof", "description": "canonical 结果、分层检查和重放清单",
                "acceptance_criteria": [criterion],
                "out_of_scope": ["自动付款、退款、争议或法律签章", "把完成轴当业务 PASS", "跨币种、跨账户或未批准规则"],
            }],
            "approved_by": by,
        }
        steps = [{
            "step_id": "canonical-finance", "action": "canonical_finance.run",
            "started_at": generated_at, "ended_at": generated_at, "status": "ok",
            "tool": "verovo canonical_finance", "output_ref": "artifacts/canonical.json",
        }]
        trace = {
            "schema": "ovo.execution-trace/1", "trace_id": trace_id, "contract_id": contract_id,
            "started_at": generated_at, "finished_at": generated_at,
            "actor": "verovo proof export", "steps": steps,
        }
        entries = []
        evidence_by_ref = {}
        for relative, path in sources:
            entry_id = "source-" + _safe_id(relative)
            evidence_by_ref[relative] = entry_id
            entries.append({
                "evidence_id": entry_id, "kind": "source_document", "source_ref": relative,
                "source_hash": _hash_file(path), "observed_at": generated_at,
                "collected_by": "verovo proof export", "trust": "observed",
            })
        entries.append({
            "evidence_id": "canonical-artifact", "kind": "source_document",
            "source_ref": "artifacts/canonical.json", "source_hash": canonical_hash,
            "observed_at": generated_at, "collected_by": "verovo proof export", "trust": "derived",
        })
        checks = [_check(
            "canonical-artifact", "P1", "PASS", "canonical 账期产物存在且可读取",
            "executor_observed", ["canonical-artifact"], "仅当前输入、规则和算法版本")]
        groups = (
            ("Rule Repriced", "rule-repriced", "P1", "executor_observed"),
            ("Invoice Reconciled", "invoice-reconciled", "P2", "independent_state"),
            ("Vendor Reconciled", "vendor-reconciled", "P1", "executor_observed"),
            ("Buyer Verified", "buyer-verified", "P1", "executor_observed"),
            ("Delivery Accepted", "delivery-accepted", "P4", "external_observation"),
        )
        conclusions = result.get("evidence_conclusions") or {}
        for name, prefix, level, independence in groups:
            rows = conclusions.get(name) or []
            if not rows:
                checks.append(_check(prefix + ":coverage", level, "UNCHECKED", name,
                                     independence, [], "该账期没有适用记录", "没有可检查的结论行"))
            for row in rows:
                row_id = str(row.get("id") or "unknown")
                status = _mapped_status(str(row.get("status") or "UNKNOWN"))
                refs = ["canonical-artifact"]
                source_path = str(row.get("source_path") or "")
                if source_path in evidence_by_ref:
                    refs.append(evidence_by_ref[source_path])
                checks.append(_check(
                    prefix + ":" + row_id, level, status,
                    "%s / %s" % (name, row_id), independence, refs,
                    "仅该账期、来源、规则和结论行；不外推其他账期",
                    str(row.get("reason") or "") if status not in ("PASS", "FAIL") else ""))
        for prefix in ("rule-repriced", "buyer-verified"):
            checks.append(_check(
                "independent-review:" + prefix, "P3", "UNCHECKED",
                "独立专业复核者接受该账期的规则与买方资格判断", "independent_validator", [],
                "P1 保留 canonical 自检；只有另行登记的独立复核才能改变此状态",
                "没有随包提供独立专业复核记录"))
        open_items = (result.get("case") or {}).get("open_items") or []
        checks.append(_check(
            "coverage:canonical", "P1", "INDETERMINATE" if open_items else "PASS",
            "canonical 输入与 open items 的覆盖状态", "executor_observed", ["canonical-artifact"],
            "仅当前账期已知输入；未知来源覆盖不被假定为完整",
            "存在 %d 个 open items" % len(open_items) if open_items else ""))
        for axis in ("finance_status", "verification_status", "delivery_status", "economics_status"):
            value = str(result.get(axis) or "blocked")
            axis_status = ("PASS" if value == "complete" else
                           "UNCHECKED" if value == "not_applicable" else
                           "INDETERMINATE" if value == "partial" else "FAIL")
            checks.append(_check(
                "completion-axis:" + axis, "P1", axis_status,
                "%s 的完成轴为 %s" % (axis, value), "executor_observed", ["canonical-artifact"],
                "完成轴只表示该轴是否闭合，不表示业务结果有利",
                "该轴不适用" if value == "not_applicable" else value if axis_status != "PASS" else ""))
        checks.append(_check(
            "history:close-process", "P1", "UNCHECKED",
            "导入、映射审核、复核、签核与修订历史", "executor_observed", [],
            "canonical 包只记录当前运行；close 历史需另行提供",
            "当前输入没有完整 close 过程轨迹"))
        bundle = {
            "schema": "ovo.acceptance-bundle/1", "task_contract": contract,
            "execution_trace": trace,
            "evidence_ledger": {"schema": "ovo.evidence-ledger/1", "ledger_id": "ledger-" + safe_case,
                                "contract_id": contract_id, "entries": entries},
            "acceptances": [{
                "schema": "ovo.acceptance-record/1", "acceptance_id": "accept-canonical-proof",
                "contract_id": contract_id, "trace_id": trace_id,
                "deliverable_id": "canonical-period-proof", "criterion": criterion,
                "verdict": "PASS", "evidence_refs": ["canonical-artifact"],
                "checked_by": by, "checked_at": generated_at,
                "notes": "只验收 canonical 投影与重放元数据，不替代买方业务签核",
            }],
            "escalations": [],
        }
        proof = {
            "schema": "ovo.proof/1", "proof_id": "proof-" + safe_case,
            "contract_id": contract_id, "product": "verovo", "generated_at": generated_at,
            "environment": {"product_version": __version__, "runtime": "Python " + sys.version.split()[0],
                            "platform": sys.platform},
            "artifacts": [{"artifact_id": "canonical", "path": "artifacts/canonical.json",
                           "sha256": canonical_hash, "classification": classification}],
            "replay": {"manifest": "replay/manifest.json", "status": "AVAILABLE", "missing": []},
            "limitations": [
                "财务完成轴不是业务 PASS/FAIL；费用、交付、覆盖与买方决定分开",
                "规则复算不是跨源对账；供应商自认不是买方独立验收",
                "P4 外部结果不证明由 Verovo 导致；causality=not_claimed",
                "重放语义指纹只排除 intake_report.generated_at 时钟；其余财务字段全部参与哈希",
                "签核不授权付款、退款或争议",
            ],
        }
        replay = {
            "schema": "ovo.replay-manifest/1", "contract_id": contract_id, "status": "AVAILABLE",
            "commands": [
                {"name": "replay-canonical", "argv": ["verovo", "proof", "replay", "${PROOF_DIR}", "--bundle", "${PERIOD_BUNDLE}"]},
                {"name": "validate-proof", "argv": ["python3", "-m", "ovo_acceptance", "proof", "${PROOF_DIR}"]},
            ],
            "inputs": [{"ref": relative, "sha256": _hash_file(path)} for relative, path in sources],
            "expected_output": {
                "artifact_id": "canonical",
                "semantic_sha256": semantic_fingerprint(result),
                "excluded": ["intake_report.generated_at"],
            },
            "missing": [],
        }
        _write_json(temp / "bundle.json", bundle)
        _write_json(temp / "contract.json", contract)
        with (temp / "trace.jsonl").open("w", encoding="utf-8") as handle:
            for step in steps:
                handle.write(json.dumps(step, ensure_ascii=False) + "\n")
        _write_json(temp / "checks.json", {"schema": "ovo.proof-checks/1", "contract_id": contract_id, "checks": checks})
        _write_json(temp / "proof.json", proof)
        _write_json(temp / "replay" / "manifest.json", replay)
        temp.rename(out_dir)
        return {"status": "exported", "schema": "verovo.proof-export/1", "out": str(out_dir),
                "proof_status": _status(checks), "case_id": case_id,
                "canonical_sha256": canonical_hash, "source_inputs_mutated": False,
                "causality": "not_claimed"}
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def _proof_structure_errors(proof_dir: Path) -> List[Dict[str, str]]:
    errors = []
    try:
        proof = json.loads((proof_dir / "proof.json").read_text(encoding="utf-8"))
        replay = json.loads((proof_dir / "replay" / "manifest.json").read_text(encoding="utf-8"))
        contract = json.loads((proof_dir / "contract.json").read_text(encoding="utf-8"))
        checks = json.loads((proof_dir / "checks.json").read_text(encoding="utf-8"))
        bundle = json.loads((proof_dir / "bundle.json").read_text(encoding="utf-8"))
        trace = [json.loads(line) for line in (proof_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        return [{"reason": "missing_or_invalid", "detail": str(exc)}]
    contract_id = contract.get("contract_id")
    for name, doc in (("proof", proof), ("replay", replay), ("checks", checks)):
        if doc.get("contract_id") != contract_id:
            errors.append({"reason": "contract_mismatch", "detail": name})
    if proof.get("schema") != "ovo.proof/1" or replay.get("schema") != "ovo.replay-manifest/1" \
            or checks.get("schema") != "ovo.proof-checks/1":
        errors.append({"reason": "schema", "detail": "proof/replay/checks schema mismatch"})
    if contract != bundle.get("task_contract"):
        errors.append({"reason": "contract_projection", "detail": "contract.json differs from bundle"})
    if trace != (bundle.get("execution_trace") or {}).get("steps"):
        errors.append({"reason": "trace_projection", "detail": "trace.jsonl differs from bundle"})
    if not str((proof.get("environment") or {}).get("product_version") or "").strip():
        errors.append({"reason": "algorithm_version", "detail": "missing product_version"})
    expected = replay.get("expected_output") or {}
    if not model_hash(expected.get("semantic_sha256")):
        errors.append({"reason": "expected_output", "detail": "missing semantic_sha256"})
    if expected.get("excluded") != ["intake_report.generated_at"]:
        errors.append({"reason": "semantic_exclusions", "detail": "unexpected fingerprint exclusions"})
    artifacts = proof.get("artifacts") or []
    canonical = next((item for item in artifacts if item.get("artifact_id") == "canonical"), None)
    if canonical is None:
        errors.append({"reason": "artifact", "detail": "canonical artifact missing from proof"})
    else:
        path = (proof_dir / str(canonical.get("path") or "")).resolve()
        try:
            path.relative_to(proof_dir)
        except ValueError:
            errors.append({"reason": "unsafe_artifact", "detail": "canonical path escapes proof"})
        else:
            if not path.is_file() or not model_hash(canonical.get("sha256")):
                errors.append({"reason": "artifact", "detail": "canonical artifact missing or hash malformed"})
            elif _hash_file(path) != canonical.get("sha256"):
                errors.append({"reason": "artifact_hash", "detail": "canonical artifact bytes changed"})
    if not checks.get("checks"):
        errors.append({"reason": "checks", "detail": "no checks registered"})
    return errors


def model_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(re.match(r"^sha256:[0-9a-f]{64}$", value))


def replay_proof(proof_dir: str | Path, bundle_dir: str | Path) -> Dict[str, Any]:
    proof_dir = Path(proof_dir).resolve()
    bundle_dir = Path(bundle_dir).resolve()
    structure_errors = _proof_structure_errors(proof_dir)
    if structure_errors:
        return {"status": "INDETERMINATE", "output_match": False,
                "mismatches": structure_errors, "proof_mutated": False}
    replay = json.loads((proof_dir / "replay" / "manifest.json").read_text(encoding="utf-8"))
    mismatches = []
    for item in replay.get("inputs") or []:
        relative = str(item.get("ref") or "")
        path = (bundle_dir / relative).resolve()
        try:
            path.relative_to(bundle_dir)
        except ValueError:
            mismatches.append({"ref": relative, "reason": "unsafe_path"})
            continue
        if not path.is_file():
            mismatches.append({"ref": relative, "reason": "missing"})
            continue
        if _hash_file(path) != item.get("sha256"):
            mismatches.append({"ref": relative, "reason": "hash_changed"})
    if mismatches:
        return {"status": "EXPIRED", "output_match": False, "mismatches": mismatches,
                "proof_mutated": False}
    result = canonical_finance.run(bundle_dir)
    expected = replay["expected_output"]
    actual_hash = semantic_fingerprint(result)
    matches = actual_hash == expected["semantic_sha256"]
    return {"status": "PASS" if matches else "FAIL", "output_match": matches,
            "expected_sha256": expected["semantic_sha256"], "actual_sha256": actual_hash,
            "semantic_exclusions": expected.get("excluded") or [],
            "mismatches": [] if matches else [{"ref": "artifacts/canonical.json", "reason": "output_changed"}],
            "proof_mutated": False, "causality": "not_claimed"}
