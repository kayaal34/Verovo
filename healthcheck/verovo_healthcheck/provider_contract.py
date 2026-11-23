"""Validate REA-inspired Provider Manifests and compute local admission."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List


DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
SNAPSHOT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
INTEGRATION_MODES = {"file", "process", "http", "embedded"}
CHECK_STATUSES = {"pass", "fail", "unknown"}
CLEANUP_STATUSES = {"released", "retained", "unknown"}
COVERAGE_STATUSES = {"complete", "partial", "unknown"}
LIMIT_FIELDS = ("input_bytes", "output_bytes", "depth", "cpu_seconds", "timeout_ms")
COVERAGE_COUNTS = ("matched", "unmatched", "duplicates", "rejected", "omitted")


class ProviderContractError(ValueError):
    pass


def _object(value: Any, required: List[str], optional: List[str], label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderContractError("%s must be an object" % label)
    for field in required:
        if field not in value:
            raise ProviderContractError("%s.%s: missing required field" % (label, field))
    unknown = set(value) - set(required) - set(optional)
    if unknown:
        raise ProviderContractError("%s: unknown field %s" % (label, sorted(unknown)[0]))
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderContractError("%s must be a non-empty string" % label)
    return value


def _integer(value: Any, label: str, positive: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderContractError("%s must be an integer" % label)
    if positive and value <= 0:
        raise ProviderContractError("%s must be positive" % label)
    if not positive and value < 0:
        raise ProviderContractError("%s must be non-negative" % label)
    return value


def _validate(document: Any) -> Dict[str, Any]:
    root = _object(document, [
        "schema",
        "adapter_id",
        "engine",
        "source",
        "readiness_checks",
        "limits",
        "coverage",
        "cleanup",
        "raw_snapshot_refs",
        "normalization_rule_version",
    ], [], "manifest")
    if root["schema"] != "verovo.provider-manifest/0.1":
        raise ProviderContractError("schema must be verovo.provider-manifest/0.1")
    _text(root["adapter_id"], "adapter_id")

    engine = _object(root["engine"], ["id", "version", "integration_mode"], ["executable_sha256"], "engine")
    _text(engine["id"], "engine.id")
    _text(engine["version"], "engine.version")
    if engine["integration_mode"] not in INTEGRATION_MODES:
        raise ProviderContractError("engine.integration_mode is unsupported")
    digest = engine.get("executable_sha256")
    if engine["integration_mode"] == "process" and digest is None:
        raise ProviderContractError("engine.executable_sha256 is required for process engines")
    if digest is not None and (not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest)):
        raise ProviderContractError("engine.executable_sha256 must be 64 lowercase hex characters")

    source = _object(root["source"], [
        "party", "control_domain", "collector_party", "collection_method",
    ], [], "source")
    for field in ("party", "control_domain", "collector_party", "collection_method"):
        _text(source[field], "source.%s" % field)

    checks = root["readiness_checks"]
    if not isinstance(checks, list) or not checks:
        raise ProviderContractError("readiness_checks must be a non-empty array")
    for index, check_value in enumerate(checks):
        label = "readiness_checks[%d]" % index
        check = _object(check_value, ["check_id", "status", "required", "observed"], [], label)
        _text(check["check_id"], "%s.check_id" % label)
        if check["status"] not in CHECK_STATUSES:
            raise ProviderContractError("%s.status is unsupported" % label)
        if not isinstance(check["required"], bool):
            raise ProviderContractError("%s.required must be boolean" % label)
        _text(check["observed"], "%s.observed" % label)

    limits = _object(root["limits"], list(LIMIT_FIELDS), [], "limits")
    for field in LIMIT_FIELDS:
        _integer(limits[field], "limits.%s" % field, positive=True)

    coverage = _object(root["coverage"], ["status"] + list(COVERAGE_COUNTS), [], "coverage")
    if coverage["status"] not in COVERAGE_STATUSES:
        raise ProviderContractError("coverage.status is unsupported")
    for field in COVERAGE_COUNTS:
        _integer(coverage[field], "coverage.%s" % field, positive=False)

    cleanup = _object(root["cleanup"], ["status"], ["reason"], "cleanup")
    if cleanup["status"] not in CLEANUP_STATUSES:
        raise ProviderContractError("cleanup.status is unsupported")
    if "reason" in cleanup:
        _text(cleanup["reason"], "cleanup.reason")

    refs = root["raw_snapshot_refs"]
    if not isinstance(refs, list) or not refs:
        raise ProviderContractError("raw_snapshot_refs must be a non-empty array")
    for ref in refs:
        if not isinstance(ref, str) or not SNAPSHOT_RE.fullmatch(ref):
            raise ProviderContractError("raw_snapshot_refs must contain sha256 digests")
    if len(set(refs)) != len(refs):
        raise ProviderContractError("raw_snapshot_refs must be unique")
    _text(root["normalization_rule_version"], "normalization_rule_version")
    return root


def evaluate_manifest(document: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a manifest and return local admission without contacting an engine."""
    manifest = _validate(document)
    reasons = []
    admission = "admit"
    cleanup_status = manifest["cleanup"]["status"]
    if cleanup_status != "released":
        admission = "refuse"
        reasons.append("cleanup status is %s; a new run is not admitted" % cleanup_status)
    for check in manifest["readiness_checks"]:
        if check["status"] == "pass":
            continue
        reason = "%s check %s: %s" % (
            "required" if check["required"] else "optional",
            check["check_id"],
            check["status"],
        )
        reasons.append(reason)
        if check["required"]:
            admission = "refuse"
        elif admission != "refuse":
            admission = "degrade"
    return {
        "schema": "verovo.provider-admission/0.1",
        "adapter_id": manifest["adapter_id"],
        "admission": admission,
        "reasons": reasons,
        "coverage": dict(manifest["coverage"]),
        "cleanup_status": cleanup_status,
        "live_engine_contacted": False,
    }


def load_manifest(path: str) -> Dict[str, Any]:
    """Read and evaluate one local JSON manifest."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderContractError("cannot read manifest: %s" % exc) from exc
    return evaluate_manifest(document)
