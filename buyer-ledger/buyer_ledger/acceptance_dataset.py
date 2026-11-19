"""V-NEXT-3: buyer acceptance dataset and human/machine score calibration.

The module builds immutable evaluation datasets, runs a local scoring pass,
keeps human labels strictly separate from automatic labels, routes
disagreements to an adjudication queue and reports agreement only over
samples a human actually labelled.

It never pays, renews, signs off or writes Buyer Verified / Delivery Accepted.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DATASET_SCHEMA = "verovo.buyer-acceptance-dataset/1"
RUN_SCHEMA = "verovo.evaluation-run/1"
CALIBRATION_SCHEMA = "verovo.calibration-summary/1"
GENERATOR = "buyer_ledger.acceptance_dataset"
GENERATOR_VERSION = "1"

LABELS = ("PASS", "FAIL", "UNKNOWN")
SCORE_KINDS = ("pass_fail", "numeric")
DISAGREEMENT_STATES = ("none", "disagreement", "adjudicated", "unknown")
REFERENCE_STATES = ("OK", "BROKEN_REFERENCE", "EXTERNAL_REFERENCE", "UNCHECKED")
CRITERIA_STATES = ("OK", "CRITERIA_MISSING")
SENSITIVE_KEYS = ("email", "phone", "mobile", "card_number", "id_number",
                  "address", "customer_name", "contact")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"\+?\d[\d\s-]{6,}\d")

BOUNDARY = ("本地只读评测：不付款、不续约、不签核；自动 PASS 不等于人工验收，"
            "不改变 Delivery。")


class EvaluationError(ValueError):
    """A dataset, run or label violated a stable, reportable contract."""

    def __init__(self, message: str, code: str = "INPUT_REQUIRED") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _now_iso(now: Optional[datetime] = None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _sha(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _deep(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _redact(text: str) -> str:
    text = EMAIL_RE.sub("[redacted-email]", text)
    return PHONE_RE.sub("[redacted-number]", text)


def _csv_safe(value: Any) -> str:
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _decimal(value: Any, field: str, *, allow_none: bool = True) -> Optional[Decimal]:
    if value is None or _text(value) == "":
        if allow_none:
            return None
        raise EvaluationError("%s is required" % field)
    if isinstance(value, float):
        raise EvaluationError("refuse float in %s: pass a decimal string" % field,
                              "FLOAT_REFUSED")
    try:
        parsed = Decimal(_text(value))
    except InvalidOperation as exc:
        raise EvaluationError("%s is not a decimal string: %r" % (field, value),
                              "SCORE_FORMAT_INVALID") from exc
    if not parsed.is_finite():
        raise EvaluationError("%s must be finite" % field,
                              "SCORE_FORMAT_INVALID")
    return parsed


def _parse_dt(value: Any, field: str) -> str:
    raw = _text(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise EvaluationError("%s must be ISO 8601: %s" % (field, exc),
                              "TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvaluationError("%s must carry a timezone offset" % field,
                              "TIMESTAMP_INVALID")
    return parsed.isoformat()


def _looks_like_path(ref: str) -> bool:
    if ref.startswith(("http://", "https://")):
        return False
    return "/" in ref or ref.endswith((".json", ".csv", ".md", ".txt", ".html"))


def _check_sensitive(sample: Dict[str, Any]) -> None:
    for key in SENSITIVE_KEYS:
        if _text(sample.get(key)):
            raise EvaluationError(
                "sample %s carries sensitive field %r; pass a reference instead"
                % (sample.get("sample_id"), key), "SENSITIVE_FIELD_REJECTED")


def _reference_state(sample: Dict[str, Any], root: Optional[Path]) -> str:
    if root is None:
        return "UNCHECKED"
    refs = list(sample.get("artifact_refs") or [])
    if sample.get("trace_ref"):
        refs.append(sample["trace_ref"])
    if sample.get("input_ref") and _looks_like_path(_text(sample["input_ref"])):
        refs.append(sample["input_ref"])
    state = "OK"
    for ref in refs:
        ref = _text(ref)
        if not ref:
            continue
        if ref.startswith(("http://", "https://")):
            if state == "OK":
                state = "EXTERNAL_REFERENCE"
            continue
        path = Path(ref)
        if not path.is_absolute():
            path = Path(root) / path
        if not path.exists():
            return "BROKEN_REFERENCE"
    return state


# ---------------------------------------------------------------------------
# dataset
# ---------------------------------------------------------------------------

def _dataset_content(dataset: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "dataset_id": dataset.get("dataset_id"),
        "dataset_version": dataset.get("dataset_version"),
        "window": dataset.get("window"),
        "criteria": dataset.get("criteria"),
        "samples": dataset.get("samples"),
    }


def dataset_hash(dataset: Dict[str, Any]) -> str:
    return _sha(_dataset_content(dataset))


def _criteria_block(criteria: Optional[Dict[str, Any]],
                    samples: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if criteria is None:
        return None
    if not isinstance(criteria, dict):
        raise EvaluationError("criteria must be an object", "CRITERIA_INVALID")
    criteria_id = _text(criteria.get("criteria_id"))
    version = _text(criteria.get("criteria_version"))
    if not criteria_id or not version:
        raise EvaluationError("criteria needs criteria_id and criteria_version",
                              "CRITERIA_INVALID")
    checks = criteria.get("checks") or []
    if not isinstance(checks, list) or not checks:
        raise EvaluationError("criteria.checks must be a non-empty list",
                              "CRITERIA_INVALID")
    for check in checks:
        if not isinstance(check, dict) or not _text(check.get("check_id")):
            raise EvaluationError("each check needs check_id",
                                  "CRITERIA_INVALID")
    for sample in samples:
        ref = _text(sample.get("buyer_criteria_ref"))
        if ref and ref != criteria_id:
            raise EvaluationError(
                "sample %s references criteria %s, not %s"
                % (sample["sample_id"], ref, criteria_id),
                "CRITERIA_MISMATCH")
        sample_version = _text(sample.get("criteria_version"))
        if sample_version and sample_version != version:
            raise EvaluationError(
                "sample %s references criteria version %s, not %s"
                % (sample["sample_id"], sample_version, version),
                "CRITERIA_MISMATCH")
    return {"criteria_id": criteria_id, "criteria_version": version,
            "checks": _deep(checks)}


def build_dataset(samples: Iterable[Any], *, dataset_id: str, dataset_version: str,
                  window: Optional[Dict[str, str]] = None,
                  criteria: Optional[Dict[str, Any]] = None,
                  previous: Optional[Dict[str, Any]] = None,
                  reference_root: Optional[Path] = None,
                  generated_at: Optional[datetime] = None) -> Dict[str, Any]:
    """Build an immutable dataset; same version with new content is refused."""
    dataset_id = _text(dataset_id)
    dataset_version = _text(dataset_version)
    if not dataset_id or not dataset_version:
        raise EvaluationError("dataset_id and dataset_version are required",
                              "INPUT_REQUIRED")
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for index, raw in enumerate(samples):
        if not isinstance(raw, dict):
            raise EvaluationError("sample #%d must be an object" % index,
                                  "INPUT_INVALID")
        sample_id = _text(raw.get("sample_id"))
        if not sample_id:
            raise EvaluationError("sample #%d needs sample_id" % index,
                                  "INPUT_INVALID")
        if sample_id in seen:
            raise EvaluationError("duplicate sample_id: %s" % sample_id,
                                  "INPUT_INVALID")
        seen.add(sample_id)
        _check_sensitive(raw)
        input_ref = _text(raw.get("input_ref"))
        if not input_ref:
            raise EvaluationError("sample %s needs input_ref" % sample_id,
                                  "INPUT_INVALID")
        input_hash = _text(raw.get("input_hash"))
        if input_hash and not input_hash.startswith("sha256:"):
            raise EvaluationError(
                "sample %s input_hash must start with sha256:" % sample_id,
                "INPUT_INVALID")
        artifact_refs = raw.get("artifact_refs") or []
        if not isinstance(artifact_refs, list):
            raise EvaluationError("sample %s artifact_refs must be a list"
                                  % sample_id, "INPUT_INVALID")
        row = {
            "sample_id": sample_id,
            "input_ref": input_ref,
            "input_hash": input_hash or None,
            "buyer_criteria_ref": _text(raw.get("buyer_criteria_ref")) or None,
            "criteria_version": _text(raw.get("criteria_version")) or None,
            "trace_ref": _text(raw.get("trace_ref")) or None,
            "artifact_refs": [_text(item) for item in artifact_refs if _text(item)],
            "note": _redact(_text(raw.get("note"))),
        }
        rows.append(row)

    criteria_block = _criteria_block(criteria, rows)
    for row in rows:
        # A sample-carried ref only documents provenance; without an explicit
        # criteria block the standard itself is missing, so the sample cannot
        # be scored against it.
        row["criteria_state"] = ("OK" if criteria_block is not None
                                 else "CRITERIA_MISSING")
        row["reference_state"] = _reference_state(row, reference_root)

    dataset = {
        "schema": DATASET_SCHEMA,
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "generated_at": _now_iso(generated_at),
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "window": _deep(window) if window else None,
        "criteria": criteria_block,
        "samples": rows,
        "counts": {
            "samples": len(rows),
            "criteria_missing": sum(1 for row in rows
                                    if row["criteria_state"] != "OK"),
            "broken_references": sum(1 for row in rows
                                     if row["reference_state"] == "BROKEN_REFERENCE"),
        },
        "effects": {"payment": "none", "renewal": "none", "signoff": "none"},
        "delivery_state": "UNKNOWN",
        "boundary": BOUNDARY,
    }
    dataset["dataset_hash"] = dataset_hash(dataset)

    if previous:
        if (previous.get("dataset_id") == dataset_id
                and previous.get("dataset_version") == dataset_version
                and previous.get("dataset_hash") != dataset["dataset_hash"]):
            raise EvaluationError(
                "dataset %s v%s changed content; bump dataset_version"
                % (dataset_id, dataset_version), "DATASET_VERSION_CONFLICT")
    return dataset


def validate_dataset(dataset: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(dataset, dict):
        raise EvaluationError("dataset must be a JSON object", "DATASET_INVALID")
    if dataset.get("schema") != DATASET_SCHEMA:
        raise EvaluationError("unsupported dataset schema: %r"
                              % dataset.get("schema"), "DATASET_INVALID")
    for field in ("dataset_id", "dataset_version", "samples"):
        if dataset.get(field) in (None, "", []):
            raise EvaluationError("dataset is missing %s" % field,
                                  "DATASET_INVALID")
    normalized = _deep(dataset)
    computed = dataset_hash(normalized)
    declared = _text(normalized.get("dataset_hash"))
    if declared and declared != computed:
        raise EvaluationError("dataset_hash does not match content",
                              "DATASET_HASH_MISMATCH")
    normalized["computed_dataset_hash"] = computed
    normalized["dataset_hash"] = declared or computed
    return normalized


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------

def _score_rows(scores: Iterable[Any], sample_ids: set) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for index, raw in enumerate(scores):
        if not isinstance(raw, dict):
            raise EvaluationError("score #%d must be an object" % index,
                                  "INPUT_INVALID")
        sample_id = _text(raw.get("sample_id"))
        if sample_id not in sample_ids:
            raise EvaluationError("score references unknown sample: %s"
                                  % sample_id, "SAMPLE_UNKNOWN")
        if sample_id in rows:
            raise EvaluationError("duplicate score for %s" % sample_id,
                                  "INPUT_INVALID")
        label = _text(raw.get("auto_label")).upper()
        if label not in LABELS:
            raise EvaluationError(
                "auto_label must be one of %s (got %r)"
                % (", ".join(LABELS), raw.get("auto_label")),
                "SCORE_FORMAT_INVALID")
        kind = _text(raw.get("score_kind")) or "pass_fail"
        if kind not in SCORE_KINDS:
            raise EvaluationError("unsupported score_kind: %r" % kind,
                                  "SCORE_FORMAT_INVALID")
        score = _decimal(raw.get("auto_score"), "auto_score")
        if score is not None and not (Decimal("0") <= score <= Decimal("1")):
            raise EvaluationError("auto_score must be within [0, 1]",
                                  "SCORE_FORMAT_INVALID")
        rows[sample_id] = {"auto_label": label, "score_kind": kind,
                           "auto_score": (None if score is None
                                          else format(score, "f")),
                           "evaluator_note": _redact(_text(raw.get("note")))}
    return rows


def _human_rows(labels: Iterable[Any], sample_ids: set
                ) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    problems: List[str] = []
    for index, raw in enumerate(labels):
        if not isinstance(raw, dict):
            raise EvaluationError("human label #%d must be an object" % index,
                                  "INPUT_INVALID")
        sample_id = _text(raw.get("sample_id"))
        if sample_id not in sample_ids:
            raise EvaluationError("label references unknown sample: %s"
                                  % sample_id, "SAMPLE_UNKNOWN")
        label = _text(raw.get("label") or raw.get("human_label")).upper()
        if label not in LABELS:
            raise EvaluationError("human label must be one of %s" % ", ".join(LABELS),
                                  "SCORE_FORMAT_INVALID")
        reviewer = _text(raw.get("reviewer"))
        if not reviewer:
            raise EvaluationError("human label for %s needs reviewer" % sample_id,
                                  "INPUT_REQUIRED")
        record = {"label": label, "reviewer": reviewer,
                  "reviewed_at": _parse_dt(raw.get("reviewed_at"),
                                           "reviewed_at"),
                  "note": _redact(_text(raw.get("note")))}
        existing = grouped.setdefault(sample_id, [])
        if any(item["reviewer"] == reviewer for item in existing):
            raise EvaluationError("duplicate reviewer %s for %s"
                                  % (reviewer, sample_id), "INPUT_INVALID")
        existing.append(record)
    return grouped, problems


def _adjudication_rows(adjudications: Iterable[Any], sample_ids: set
                       ) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for index, raw in enumerate(adjudications):
        if not isinstance(raw, dict):
            raise EvaluationError("adjudication #%d must be an object" % index,
                                  "INPUT_INVALID")
        sample_id = _text(raw.get("sample_id"))
        if sample_id not in sample_ids:
            raise EvaluationError("adjudication references unknown sample: %s"
                                  % sample_id, "SAMPLE_UNKNOWN")
        label = _text(raw.get("label")).upper()
        if label not in LABELS:
            raise EvaluationError("adjudicated label must be one of %s"
                                  % ", ".join(LABELS), "SCORE_FORMAT_INVALID")
        adjudicator = _text(raw.get("adjudicator"))
        if not adjudicator:
            raise EvaluationError("adjudication for %s needs adjudicator"
                                  % sample_id, "INPUT_REQUIRED")
        if sample_id in rows:
            raise EvaluationError("duplicate adjudication for %s" % sample_id,
                                  "INPUT_INVALID")
        rows[sample_id] = {"label": label, "adjudicator": adjudicator,
                           "adjudicated_at": _parse_dt(raw.get("adjudicated_at"),
                                                       "adjudicated_at"),
                           "note": _redact(_text(raw.get("note")))}
    return rows


def _human_decision(labels: List[Dict[str, Any]],
                    adjudication: Optional[Dict[str, Any]]
                    ) -> Tuple[Optional[str], str, Optional[Dict[str, Any]]]:
    distinct = sorted({item["label"] for item in labels})
    if adjudication is not None:
        reviewers = {item["reviewer"] for item in labels}
        if adjudication["adjudicator"] in reviewers:
            raise EvaluationError(
                "adjudicator %s must be independent from reviewers"
                % adjudication["adjudicator"], "ADJUDICATOR_CONFLICT")
        return adjudication["label"], "adjudicated", adjudication
    if not distinct:
        return None, "unknown", None
    if len(distinct) == 1:
        return distinct[0], "none", None
    return None, "disagreement", None


def evaluate(dataset: Dict[str, Any], *, service_version: str,
             evaluator_version: str, scores: Iterable[Any],
             human_labels: Iterable[Any] = (), adjudications: Iterable[Any] = (),
             evaluated_at: Optional[datetime] = None,
             previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Score a dataset; human labels stay separate and disagreements queue up."""
    data = validate_dataset(dataset)
    service_version = _text(service_version)
    evaluator_version = _text(evaluator_version)
    if not service_version or not evaluator_version:
        raise EvaluationError("service_version and evaluator_version are required",
                              "INPUT_REQUIRED")
    sample_ids = {row["sample_id"] for row in data["samples"]}
    score_map = _score_rows(scores or [], sample_ids)
    label_map, _ = _human_rows(human_labels or [], sample_ids)
    adjudication_map = _adjudication_rows(adjudications or [], sample_ids)

    results: List[Dict[str, Any]] = []
    review_queue: List[Dict[str, Any]] = []
    for sample in data["samples"]:
        sample_id = sample["sample_id"]
        score = score_map.get(sample_id)
        auto_label = score["auto_label"] if score else None
        auto_score = score["auto_score"] if score else None
        score_state = "OK"
        score_reason = None
        if sample["criteria_state"] != "OK":
            score_state, score_reason = "UNKNOWN", "CRITERIA_MISSING"
        elif sample["reference_state"] == "BROKEN_REFERENCE":
            score_state, score_reason = "UNKNOWN", "BROKEN_REFERENCE"
        elif score is None:
            score_state, score_reason = "UNKNOWN", "SCORE_MISSING"
        if score_state != "OK":
            auto_label, auto_score = None, None

        labels = label_map.get(sample_id, [])
        human_label, disagreement_state, adjudication = _human_decision(
            labels, adjudication_map.get(sample_id))
        agreement = "unknown"
        if auto_label and human_label:
            agreement = "agree" if auto_label == human_label else "disagree"
        result = {
            "sample_id": sample_id,
            "input_ref": sample["input_ref"],
            "input_hash": sample["input_hash"],
            "buyer_criteria_ref": sample["buyer_criteria_ref"],
            "criteria_version": sample["criteria_version"],
            "criteria_state": sample["criteria_state"],
            "reference_state": sample["reference_state"],
            "score_state": score_state,
            "score_reason": score_reason,
            "auto_label": auto_label,
            "auto_score": auto_score,
            "score_kind": score["score_kind"] if score else None,
            "evaluator_note": score["evaluator_note"] if score else None,
            "human_label": human_label,
            "human_labels": labels,
            "agreement": agreement,
            "disagreement_state": disagreement_state,
            "adjudication": adjudication,
            "requires_human_acceptance": True,
            "buyer_verified": "UNKNOWN",
            "delivery_accepted": "UNKNOWN",
            "verification_effect": "none",
        }
        results.append(result)
        if disagreement_state == "disagreement":
            review_queue.append({"sample_id": sample_id,
                                 "reason": "HUMAN_DISAGREEMENT",
                                 "labels": labels})
        elif agreement == "disagree":
            review_queue.append({"sample_id": sample_id,
                                 "reason": "AUTO_HUMAN_DISAGREEMENT",
                                 "auto_label": auto_label,
                                 "human_label": human_label})
        elif score_state != "OK":
            review_queue.append({"sample_id": sample_id,
                                 "reason": score_reason})

    summary = _summarize(results)
    fingerprint = _sha({"dataset": data["dataset_hash"],
                        "service_version": service_version,
                        "evaluator_version": evaluator_version,
                        "scores": score_map, "labels": label_map,
                        "adjudications": adjudication_map})
    run = {
        "schema": RUN_SCHEMA,
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "generated_at": _now_iso(evaluated_at),
        "run_id": "ev-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12],
        "dataset": {"dataset_id": data["dataset_id"],
                    "dataset_version": data["dataset_version"],
                    "dataset_hash": data["dataset_hash"]},
        "window": data.get("window"),
        "service_version": service_version,
        "evaluator_version": evaluator_version,
        "input_fingerprint": fingerprint,
        "results": results,
        "summary": summary,
        "review_queue": review_queue,
        "calibration": _calibration(results, data),
        "delivery_state": "UNKNOWN",
        "effects": {"payment": "none", "renewal": "none", "signoff": "none",
                    "canonical": "none"},
        "boundary": BOUNDARY,
    }
    if previous:
        same_pair = (previous.get("service_version") == service_version
                     and previous.get("evaluator_version") == evaluator_version)
        if same_pair and previous.get("input_fingerprint") != fingerprint:
            raise EvaluationError(
                "evaluator %s changed behavior on the same service version; "
                "bump evaluator_version" % evaluator_version,
                "EVALUATOR_VERSION_CONFLICT")
    return run


def _summarize(results: List[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "samples": len(results),
        "scored": sum(1 for item in results if item["score_state"] == "OK"),
        "unscored": sum(1 for item in results if item["score_state"] != "OK"),
        "labelled": sum(1 for item in results if item["human_label"] is not None),
        "unlabelled": sum(1 for item in results
                          if item["human_label"] is None),
        "agree": sum(1 for item in results if item["agreement"] == "agree"),
        "disagree": sum(1 for item in results if item["agreement"] == "disagree"),
        "human_disagreements": sum(1 for item in results
                                   if item["disagreement_state"] == "disagreement"),
    }


def _calibration(results: List[Dict[str, Any]],
                 dataset: Dict[str, Any]) -> Dict[str, Any]:
    summary = _summarize(results)
    denominator = summary["agree"] + summary["disagree"]
    rate = (None if not denominator
            else format(Decimal(summary["agree"]) / Decimal(denominator),
                        ".4f"))
    return {
        "schema": CALIBRATION_SCHEMA,
        "window": dataset.get("window"),
        "samples_total": summary["samples"],
        "labelled": summary["labelled"],
        "unlabelled": summary["unlabelled"],
        "agreement_denominator": denominator,
        "agreement_rate": rate,
        "human_disagreements": summary["human_disagreements"],
        "note": ("一致率只覆盖已人工标签且可自动评分的样本；未标签样本不进分母，"
                 "也不补 0。"),
    }


def calibrate(run: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(run, dict) or run.get("schema") != RUN_SCHEMA:
        raise EvaluationError("run must be %s" % RUN_SCHEMA, "RUN_INVALID")
    calibration = run.get("calibration")
    if not calibration:
        raise EvaluationError("run has no calibration block", "RUN_INVALID")
    return _deep(calibration)


def delivery_view(run: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only Delivery comparison; never upgrades Delivery or acceptance."""
    return {
        "run_id": run.get("run_id"),
        "delivery_state": "UNKNOWN",
        "buyer_verified": "UNKNOWN",
        "delivery_accepted": "UNKNOWN",
        "auto_pass_is_acceptance": False,
        "note": "自动评分只是对照，人工验收与 Delivery 结论仍由既有真相源决定。",
    }


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------

def render_calibration(run: Dict[str, Any]) -> str:
    calibration = run.get("calibration") or {}
    summary = run.get("summary") or {}
    lines = ["# 评分校准 %s" % run.get("run_id"),
             "",
             "- 数据集：%s v%s（%s）"
             % ((run.get("dataset") or {}).get("dataset_id"),
                (run.get("dataset") or {}).get("dataset_version"),
                (run.get("dataset") or {}).get("dataset_hash")),
             "- 服务版本：%s；评分器版本：%s"
             % (run.get("service_version"), run.get("evaluator_version")),
             "- 窗口：%s" % (json.dumps(run.get("window"), ensure_ascii=False)
                            if run.get("window") else "未登记"),
             "- 样本：%d（已评分 %d / 未评分 %d）"
             % (summary.get("samples", 0), summary.get("scored", 0),
                summary.get("unscored", 0)),
             "- 人工标签：%d（未标 %d，不进一致率分母）"
             % (summary.get("labelled", 0), summary.get("unlabelled", 0)),
             "- 一致率：%s（分母 %d）"
             % (calibration.get("agreement_rate") or "null",
                calibration.get("agreement_denominator", 0)),
             "- 人工分歧：%d" % calibration.get("human_disagreements", 0),
             "- Delivery：UNKNOWN（自动评分不改变交付结论）",
             ""]
    queue = run.get("review_queue") or []
    if queue:
        lines.append("## 待复核")
        for item in queue:
            lines.append("- `%s` %s" % (item.get("sample_id"), item.get("reason")))
        lines.append("")
    lines.append(calibration.get("note", ""))
    return "\n".join(lines).rstrip() + "\n"


def write_dataset(dataset: Dict[str, Any], out_dir: Path) -> Dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "acceptance-dataset.json"
    csv_path = out / "acceptance-samples.csv"
    json_path.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    columns = ("sample_id", "input_ref", "input_hash", "buyer_criteria_ref",
               "criteria_version", "trace_ref", "artifact_refs",
               "reference_state", "criteria_state", "note")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for sample in dataset.get("samples") or []:
        row = dict(sample)
        row["artifact_refs"] = ";".join(sample.get("artifact_refs") or [])
        writer.writerow({key: _csv_safe(row.get(key)) for key in columns})
    csv_path.write_text(buffer.getvalue(), encoding="utf-8")
    return {"json": str(json_path), "csv": str(csv_path)}


def write_run(run: Dict[str, Any], out_dir: Path) -> Dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "evaluation-run.json"
    queue_path = out / "review-queue.md"
    calibration_path = out / "calibration.md"
    json_path.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    lines = ["# 复核队列 %s" % run.get("run_id"), ""]
    for item in run.get("review_queue") or []:
        lines.append("- `%s` %s" % (item.get("sample_id"), item.get("reason")))
    queue_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    calibration_path.write_text(render_calibration(run), encoding="utf-8")
    return {"json": str(json_path), "queue": str(queue_path),
            "calibration": str(calibration_path)}


def load_json(path: Path) -> Any:
    p = Path(path)
    if not p.is_file():
        raise EvaluationError("input not found: %s" % p)
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise EvaluationError("input is not JSON: %s" % exc,
                              "INPUT_INVALID") from exc


def load_dataset(path: Path) -> Dict[str, Any]:
    return validate_dataset(load_json(path))


__all__ = ["DATASET_SCHEMA", "RUN_SCHEMA", "CALIBRATION_SCHEMA", "GENERATOR",
           "GENERATOR_VERSION", "LABELS", "EvaluationError", "dataset_hash",
           "build_dataset", "validate_dataset", "evaluate", "calibrate",
           "delivery_view", "render_calibration", "write_dataset", "write_run",
           "load_json", "load_dataset"]
