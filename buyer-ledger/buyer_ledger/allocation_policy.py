"""V-NEXT-2 allocation policy versions, fingerprints and deterministic allocation.

An allocation policy is content-addressed: changing any result-affecting field
requires a new policy version and a new hash.  Allocation is a local projection
over explicit charges and an explicit driver; it never changes the Charges
difference, never changes Delivery or signoff, and never invents a driver where
one is missing:

    allocatable = allocated + unallocated + non_allocatable

Rounding residuals are listed as their own unallocated entry, never added on
top of the identity.  A missing driver, an unapproved policy or a zero driver
total leaves money unallocated and marks the unit cost unverifiable.  ROI is
null with a reason whenever the economics are incomplete.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA = "verovo.allocation-policy/1"
RESULT_SCHEMA = "verovo.allocation-result/1"
INPUT_SCHEMA = "verovo.allocation-input/1"
GENERATOR = "buyer_ledger.allocation_policy"
GENERATOR_VERSION = "1"

STATES = ("draft", "needs_review", "approved", "superseded")
IDLE_HANDLING = ("allocate", "hold_unallocated")
SHARED_HANDLING = ("proportional",)
OVERHEAD_HANDLING = ("exclude", "allocate")
NEGATIVE_HANDLING = ("allocate", "hold_unallocated", "reject")
ROUNDING_MODES = ("down", "nearest", "up")
RESIDUAL_RULES = ("rounding_to_unallocated",)

RESULT_FIELDS = (
    "scope", "window", "timezone", "resolution", "driver_metric",
    "driver_source", "driver_unit", "idle_handling", "shared_cost_handling",
    "overhead_handling", "rounding", "negative_amount_handling",
    "exclusion_rule", "residual_rule")

HASH_FIELDS = RESULT_FIELDS + ("residual_limit",)

REQUIRED_FIELDS = ("policy_id", "policy_version") + RESULT_FIELDS

BOUNDARY = (
    "L0 本地分摊投影：只读取显式费用与显式 driver 写本地结果；不修改 Charges "
    "差额、不改变 Delivery、不转移或生成签核、不生成 invoice、不付款。driver "
    "缺失或为零时金额留 unallocated，不按 0 或估算补齐；ROI 不完整即为 null。")

_ROUND = {"down": ROUND_DOWN, "nearest": ROUND_HALF_UP, "up": ROUND_UP}


class AllocationError(ValueError):
    """An allocation rule or input violated a stable, reportable contract."""

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
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _deep(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _dec(value: Any, field: str) -> Decimal:
    if value is None or _text(value) == "":
        raise AllocationError("%s is required" % field)
    if isinstance(value, float):
        raise AllocationError("refuse float in %s: pass a decimal string" % field,
                              "FLOAT_REFUSED")
    try:
        parsed = Decimal(_text(value))
    except InvalidOperation as exc:
        raise AllocationError("%s is not a decimal string: %r" % (field, value),
                              "DECIMAL_INVALID") from exc
    if not parsed.is_finite():
        raise AllocationError("%s must be finite" % field, "DECIMAL_INVALID")
    return parsed


def _money(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.0001")), "f")


def _csv_safe(value: Any) -> str:
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _parse_date(value: Any, field: str) -> str:
    raw = _text(value)
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as exc:
        raise AllocationError("%s must be YYYY-MM-DD: %r" % (field, raw),
                              "DATE_INVALID") from exc
    return raw


def _parse_dt(value: Any, field: str) -> str:
    raw = _text(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AllocationError("%s must be ISO 8601: %s" % (field, exc),
                              "TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AllocationError("%s must carry a timezone offset" % field,
                              "TIMESTAMP_INVALID")
    return parsed.isoformat()


# ---------------------------------------------------------------------------
# policy content
# ---------------------------------------------------------------------------

def content(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Return exactly the result-affecting fields, in a stable order."""
    return {field: _deep(policy.get(field)) for field in HASH_FIELDS}


def policy_hash(policy: Dict[str, Any]) -> str:
    return _sha(content(policy))


def policy_state(policy: Dict[str, Any]) -> str:
    if _text(policy.get("state")) == "superseded":
        return "superseded"
    if (_text(policy.get("approved_by")) and _text(policy.get("approved_at"))
            and _text(policy.get("approval_ref"))):
        return "approved"
    if _text(policy.get("approval_ref")) and not _text(policy.get("approved_by")):
        return "needs_review"
    return "draft"


def _validate_window(policy: Dict[str, Any]) -> Tuple[str, str]:
    window = policy.get("window")
    if not isinstance(window, dict):
        raise AllocationError("window must be an object with from/to",
                              "POLICY_INVALID")
    start = _parse_date(window.get("from"), "window.from")
    end = _parse_date(window.get("to"), "window.to")
    if not start < end:
        raise AllocationError("window must be a half-open [from, to)",
                              "POLICY_INVALID")
    return start, end


def _validate_rounding(policy: Dict[str, Any]) -> Tuple[str, str]:
    rounding = policy.get("rounding")
    if not isinstance(rounding, dict):
        raise AllocationError("rounding must be an object",
                              "POLICY_INVALID")
    quantum = _text(rounding.get("quantum")) or "0.01"
    _dec(quantum, "rounding.quantum")
    if Decimal(quantum) <= 0:
        raise AllocationError("rounding.quantum must be positive",
                              "POLICY_INVALID")
    mode = _text(rounding.get("mode")) or "down"
    if mode not in ROUNDING_MODES:
        raise AllocationError("unsupported rounding mode: %r" % mode,
                              "POLICY_INVALID")
    return quantum, mode


def validate_policy(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Validate required fields and enums; return the normalized policy copy."""
    if not isinstance(policy, dict):
        raise AllocationError("policy must be a JSON object", "POLICY_INVALID")
    if policy.get("schema") not in (None, SCHEMA):
        raise AllocationError("unsupported policy schema: %r" % policy.get("schema"),
                              "POLICY_INVALID")
    missing = [field for field in REQUIRED_FIELDS if policy.get(field) in (None, "")]
    if missing:
        raise AllocationError("policy is missing: %s" % ", ".join(missing),
                              "POLICY_INVALID")
    normalized = _deep(policy)
    normalized["schema"] = SCHEMA
    _validate_window(normalized)
    _validate_rounding(normalized)
    enums = (("idle_handling", IDLE_HANDLING),
             ("shared_cost_handling", SHARED_HANDLING),
             ("overhead_handling", OVERHEAD_HANDLING),
             ("negative_amount_handling", NEGATIVE_HANDLING),
             ("residual_rule", RESIDUAL_RULES))
    for field, allowed in enums:
        if _text(normalized.get(field)) not in allowed:
            raise AllocationError(
                "%s must be one of %s (got %r)"
                % (field, ", ".join(allowed), normalized.get(field)),
                "POLICY_INVALID")
    if not _text(normalized.get("timezone")):
        raise AllocationError("timezone is required", "POLICY_INVALID")
    if _text(normalized.get("residual_limit")):
        if _dec(normalized["residual_limit"], "residual_limit") <= 0:
            raise AllocationError("residual_limit must be positive",
                                  "POLICY_INVALID")
    exclusion = normalized.get("exclusion_rule")
    if exclusion is None:
        normalized["exclusion_rule"] = {"categories": []}
    elif not isinstance(exclusion, dict):
        raise AllocationError("exclusion_rule must be an object",
                              "POLICY_INVALID")
    categories = normalized["exclusion_rule"].get("categories") or []
    if not isinstance(categories, list):
        raise AllocationError("exclusion_rule.categories must be a list",
                              "POLICY_INVALID")
    normalized["exclusion_rule"]["categories"] = [_text(item)
                                                 for item in categories if _text(item)]
    default_hash = normalized.get("policy_hash") or policy_hash(normalized)
    computed = policy_hash(normalized)
    normalized["computed_policy_hash"] = computed
    normalized["policy_hash"] = _text(default_hash) or computed
    normalized["state"] = policy_state(normalized)
    return normalized


def check_policy(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Return validation plus verifiability diagnostics; never raises for gaps."""
    diagnostics: List[Dict[str, str]] = []
    try:
        normalized = validate_policy(policy)
    except AllocationError as exc:
        return {"ok": False,
                "diagnostics": [{"code": exc.code, "detail": str(exc)}],
                "policy": None}
    computed = normalized["computed_policy_hash"]
    declared = _text(policy.get("policy_hash"))
    if declared and declared != computed:
        diagnostics.append({"code": "POLICY_HASH_MISMATCH",
                            "detail": "声明 %s，重算 %s" % (declared, computed)})
    if normalized["state"] != "approved":
        diagnostics.append({"code": "POLICY_NOT_APPROVED",
                            "detail": "未具名批准，只能生成草稿"})
    if not normalized.get("source_hashes"):
        diagnostics.append({"code": "SOURCE_HASH_MISSING",
                            "detail": "缺少来源哈希，窗口不可复核"})
    if not _text(normalized.get("rule_map_hash")):
        diagnostics.append({"code": "RULE_MAP_HASH_MISSING",
                            "detail": "缺少 rule_map_hash，规则版本不可复核"})
    start, end = _validate_window(normalized)
    return {"ok": True,
            "diagnostics": diagnostics,
            "policy": normalized,
            "window": {"from": start, "to": end, "to_exclusive": True},
            "policy_hash": computed,
            "state": normalized["state"]}


def revise_policy(policy: Dict[str, Any], changes: Dict[str, Any], *,
                  policy_version: str, reason: str = "") -> Dict[str, Any]:
    """Return a new draft policy; result-affecting changes require a new version."""
    if not isinstance(changes, dict) or not changes:
        raise AllocationError("changes must be a non-empty object",
                              "INPUT_REQUIRED")
    normalized = validate_policy(policy)
    before_hash = normalized["computed_policy_hash"]
    updated = _deep(normalized)
    for key, value in changes.items():
        updated[key] = _deep(value)
    updated["policy_version"] = _text(policy_version)
    if not updated["policy_version"]:
        raise AllocationError("policy_version is required", "INPUT_REQUIRED")
    updated["parent_policy_version"] = _text(normalized.get("policy_version"))
    updated["change_reason"] = _text(reason)
    updated.pop("policy_hash", None)
    updated.pop("computed_policy_hash", None)
    for field in ("approved_by", "approved_at", "approval_ref"):
        updated.pop(field, None)
    updated["state"] = "draft"
    updated = validate_policy(updated)
    after_hash = updated["computed_policy_hash"]
    if after_hash != before_hash and updated["policy_version"] == normalized.get(
            "policy_version"):
        raise AllocationError(
            "result-affecting change requires a new policy_version",
            "POLICY_VERSION_CONFLICT")
    return updated


def approve_policy(policy: Dict[str, Any], *, approved_by: str,
                   approved_at: str, approval_ref: str) -> Dict[str, Any]:
    if not (_text(approved_by) and _text(approved_at) and _text(approval_ref)):
        raise AllocationError(
            "approved_by, approved_at and approval_ref are all required",
            "APPROVAL_REQUIRED")
    normalized = validate_policy(policy)
    normalized["approved_by"] = _text(approved_by)
    normalized["approved_at"] = _parse_dt(approved_at, "approved_at")
    normalized["approval_ref"] = _text(approval_ref)
    normalized["state"] = "approved"
    normalized["policy_hash"] = normalized["computed_policy_hash"]
    return normalized


def classify_changes(before: Dict[str, Any],
                     after: Dict[str, Any]) -> List[Dict[str, Any]]:
    changes = []
    for field in HASH_FIELDS:
        old, new = before.get(field), after.get(field)
        if _canonical(old) != _canonical(new):
            changes.append({"field": field, "before": old, "after": new,
                            "result_affecting": True})
    for field in ("policy_version", "approved_by", "approval_ref", "state"):
        old, new = before.get(field), after.get(field)
        if _canonical(old) != _canonical(new):
            changes.append({"field": field, "before": old, "after": new,
                            "result_affecting": False})
    return changes


def diff_policies(before: Dict[str, Any],
                  after: Dict[str, Any]) -> Dict[str, Any]:
    before_n = validate_policy(before)
    after_n = validate_policy(after)
    changes = classify_changes(before_n, after_n)
    affecting = [item for item in changes if item["result_affecting"]]
    return {
        "policy_before": {"policy_id": before_n.get("policy_id"),
                          "policy_version": before_n.get("policy_version"),
                          "policy_hash": before_n["computed_policy_hash"]},
        "policy_after": {"policy_id": after_n.get("policy_id"),
                         "policy_version": after_n.get("policy_version"),
                         "policy_hash": after_n["computed_policy_hash"]},
        "hash_changed": before_n["computed_policy_hash"] != after_n[
            "computed_policy_hash"],
        "version_changed": before_n.get("policy_version") != after_n.get(
            "policy_version"),
        "changes": changes,
        "result_affecting_changes": affecting,
        "explanation": ("结果字段变化 %d 项，均可在两版策略中逐项复核；"
                        "分摊差异只描述规则变化，不归责。") % len(affecting),
        "note": "差异只描述策略，不说明业务好坏。",
    }


# ---------------------------------------------------------------------------
# allocation input
# ---------------------------------------------------------------------------

def _charges_block(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, list):
        return {"schema": INPUT_SCHEMA, "currency": None, "charges": payload}
    if not isinstance(payload, dict):
        raise AllocationError("charges input must be a list or object",
                              "INPUT_INVALID")
    if payload.get("schema") not in (None, INPUT_SCHEMA):
        raise AllocationError("unsupported allocation input schema",
                              "INPUT_INVALID")
    charges = payload.get("charges")
    if not isinstance(charges, list) or not charges:
        raise AllocationError("charges must be a non-empty list",
                              "INPUT_INVALID")
    return payload


def _driver_block(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, list):
        return {"values": payload}
    if not isinstance(payload, dict):
        raise AllocationError("driver input must be a list or object",
                              "INPUT_INVALID")
    if payload.get("schema") not in (None, "verovo.driver-input/1"):
        raise AllocationError("unsupported driver input schema",
                              "INPUT_INVALID")
    if not isinstance(payload.get("values"), list) or not payload["values"]:
        raise AllocationError("driver values must be a non-empty list",
                              "INPUT_INVALID")
    return payload


# ---------------------------------------------------------------------------
# allocation
# ---------------------------------------------------------------------------

def _round_amount(amount: Decimal, quantum: str, mode: str) -> Decimal:
    return amount.quantize(Decimal(quantum), rounding=_ROUND[mode])


def allocate(policy: Dict[str, Any], *, charges: Any, driver: Any,
             economics: Optional[Dict[str, Any]] = None,
             generated_at: Optional[datetime] = None) -> Dict[str, Any]:
    """Allocate explicit charges over an explicit driver; never guesses."""
    normalized = validate_policy(policy)
    block = _charges_block(charges)
    driver_block = _driver_block(driver)
    quantum, mode = _validate_rounding(normalized)
    state = normalized["state"]
    diagnostics = [item["code"] for item in check_policy(normalized)["diagnostics"]
                   if item["code"] in ("SOURCE_HASH_MISSING", "RULE_MAP_HASH_MISSING",
                                       "POLICY_HASH_MISMATCH")]

    currency = _text(block.get("currency")).upper()
    charge_rows: List[Dict[str, Any]] = []
    for index, raw in enumerate(block["charges"]):
        if not isinstance(raw, dict):
            raise AllocationError("charge #%d must be an object" % index,
                                  "INPUT_INVALID")
        charge_id = _text(raw.get("charge_id"))
        if not charge_id:
            raise AllocationError("charge #%d needs charge_id" % index,
                                  "INPUT_INVALID")
        amount = _dec(raw.get("amount"), "charge %s amount" % charge_id)
        row_currency = _text(raw.get("currency")).upper() or currency
        if currency and row_currency and row_currency != currency:
            raise AllocationError("charge %s currency %s != %s"
                                  % (charge_id, row_currency, currency),
                                  "CURRENCY_MISMATCH")
        currency = currency or row_currency
        if not row_currency:
            raise AllocationError("charge %s needs a currency" % charge_id,
                                  "INPUT_INVALID")
        charge_rows.append({
            "charge_id": charge_id,
            "amount": amount,
            "category": _text(raw.get("category")) or "usage",
            "cost_center": _text(raw.get("cost_center")),
            "currency": row_currency,
        })

    driver_metric = _text(driver_block.get("metric_code"))
    driver_unit = _text(driver_block.get("unit"))
    if driver_metric and driver_metric != _text(normalized["driver_metric"]):
        raise AllocationError(
            "driver metric %s does not match policy %s"
            % (driver_metric, normalized["driver_metric"]),
            "DRIVER_METRIC_MISMATCH")
    if driver_unit and driver_unit != _text(normalized["driver_unit"]):
        raise AllocationError(
            "driver unit %s does not match policy %s"
            % (driver_unit, normalized["driver_unit"]),
            "DRIVER_UNIT_MISMATCH")
    driver_values: Dict[str, Decimal] = {}
    for index, raw in enumerate(driver_block["values"]):
        if not isinstance(raw, dict):
            raise AllocationError("driver #%d must be an object" % index,
                                  "INPUT_INVALID")
        key = _text(raw.get("cost_center"))
        if not key:
            raise AllocationError("driver #%d needs cost_center" % index,
                                  "INPUT_INVALID")
        value = _dec(raw.get("value"), "driver %s value" % key)
        if value < 0:
            raise AllocationError("driver %s must not be negative" % key,
                                  "DRIVER_INVALID")
        if key in driver_values:
            raise AllocationError("duplicate driver cost_center: %s" % key,
                                  "DRIVER_INVALID")
        driver_values[key] = value
    driver_total = sum(driver_values.values(), Decimal("0"))

    excluded = {_text(item) for item in
                normalized["exclusion_rule"].get("categories") or []}
    charges_total = sum((row["amount"] for row in charge_rows), Decimal("0"))
    non_allocatable_items: List[Dict[str, Any]] = []
    allocatable: List[Dict[str, Any]] = []
    unallocated_items: List[Dict[str, Any]] = []
    for row in charge_rows:
        if row["category"] in excluded:
            non_allocatable_items.append({
                "charge_id": row["charge_id"], "category": row["category"],
                "amount": _money(row["amount"]),
                "reason": "EXCLUDED_CATEGORY"})
            continue
        if row["amount"] < 0 and normalized["negative_amount_handling"] == "reject":
            raise AllocationError(
                "negative charge %s rejected by policy" % row["charge_id"],
                "NEGATIVE_AMOUNT_REJECTED")
        if row["amount"] < 0 and normalized["negative_amount_handling"] == "hold_unallocated":
            non_allocatable_items.append({
                "charge_id": row["charge_id"], "category": row["category"],
                "amount": _money(row["amount"]), "reason": "NEGATIVE_HELD"})
            continue
        allocatable.append(row)

    eligible_total = sum((row["amount"] for row in allocatable), Decimal("0"))
    non_allocatable_total = sum(
        (Decimal(item["amount"]) for item in non_allocatable_items), Decimal("0"))

    allocations: List[Dict[str, Any]] = []

    def _append_allocation(row: Dict[str, Any], center: str, value: Decimal,
                           amount: Decimal) -> None:
        unit_cost = (amount / value).quantize(Decimal("0.0001"),
                                              rounding=ROUND_HALF_UP)
        allocations.append({
            "charge_id": row["charge_id"],
            "cost_center": center,
            "driver_value": _money(value),
            "amount": _money(amount),
            "unit_cost": format(unit_cost, "f"),
            "currency": row["currency"],
            "unit_cost_verifiable": False})

    if state != "approved":
        for row in allocatable:
            unallocated_items.append({
                "charge_id": row["charge_id"],
                "cost_center": row["cost_center"] or None,
                "amount": _money(row["amount"]),
                "reason": "POLICY_NOT_APPROVED"})
    elif driver_total == 0:
        for row in allocatable:
            unallocated_items.append({
                "charge_id": row["charge_id"],
                "cost_center": row["cost_center"] or None,
                "amount": _money(row["amount"]),
                "reason": "DRIVER_ZERO_TOTAL"})
    else:
        centers = sorted(driver_values)
        for row in allocatable:
            if row["cost_center"]:
                value = driver_values.get(row["cost_center"])
                if not value:
                    unallocated_items.append({
                        "charge_id": row["charge_id"],
                        "cost_center": row["cost_center"],
                        "amount": _money(row["amount"]),
                        "reason": "DRIVER_MISSING"})
                    continue
                _append_allocation(row, row["cost_center"], value,
                                   _round_amount(row["amount"], quantum, mode))
                continue
            # Shared cost: split across every driver cost center.
            for center in centers:
                value = driver_values[center]
                if value == 0:
                    continue
                share = row["amount"] * value / driver_total
                _append_allocation(row, center, value,
                                   _round_amount(share, quantum, mode))

    allocated_total = sum((Decimal(item["amount"]) for item in allocations),
                          Decimal("0"))
    unallocated_total = eligible_total - allocated_total
    listed = sum((Decimal(item["amount"]) for item in unallocated_items),
                 Decimal("0"))
    residual = unallocated_total - listed
    if residual != 0:
        unallocated_items.append({
            "charge_id": None,
            "cost_center": None,
            "amount": _money(residual),
            "reason": "ROUNDING_RESIDUAL"})

    identity_difference = (charges_total - allocated_total - unallocated_total
                           - non_allocatable_total)
    identity_holds = identity_difference == 0
    residual_limit = _text(normalized.get("residual_limit"))
    residual_exceeded = False
    if residual_limit:
        residual_exceeded = abs(residual) > _dec(residual_limit, "residual_limit")

    status = "OK"
    status_reasons: List[str] = []
    if state != "approved":
        status = "draft_only"
        status_reasons.append("POLICY_NOT_APPROVED")
    if diagnostics:
        status = "unverifiable" if status == "OK" else status
        status_reasons.extend(diagnostics)
    if state == "approved" and driver_total == 0 and eligible_total != 0:
        if status == "OK":
            status = "unverifiable"
        status_reasons.append("DRIVER_ZERO_TOTAL")
    if not identity_holds:
        status = "not_conserved"
        status_reasons.append("IDENTITY_NOT_CONSERVED")
    elif residual_exceeded and status == "OK":
        status = "residual_exceeded"
        status_reasons.append("RESIDUAL_OVER_LIMIT")
    verifiable = status == "OK"
    for item in allocations:
        item["unit_cost_verifiable"] = verifiable

    roi = _roi(normalized, economics, allocated_total, charges_total)
    result = {
        "schema": RESULT_SCHEMA,
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "generated_at": _now_iso(generated_at),
        "policy": {
            "policy_id": normalized.get("policy_id"),
            "policy_version": normalized.get("policy_version"),
            "policy_hash": normalized["computed_policy_hash"],
            "state": state,
            "approved_by": normalized.get("approved_by") or None,
            "approved_at": normalized.get("approved_at") or None,
            "approval_ref": normalized.get("approval_ref") or None,
        },
        "status": status,
        "status_reasons": sorted(set(status_reasons)),
        "currency": currency,
        "window": normalized.get("window"),
        "driver": {
            "metric_code": normalized["driver_metric"],
            "unit": normalized["driver_unit"],
            "total": _money(driver_total),
            "values": [{"cost_center": key, "value": _money(value)}
                       for key, value in sorted(driver_values.items())],
        },
        "totals": {
            "charges": _money(charges_total),
            "allocatable": _money(charges_total),
            "eligible": _money(eligible_total),
            "allocated": _money(allocated_total),
            "unallocated": _money(unallocated_total),
            "non_allocatable": _money(non_allocatable_total),
        },
        "identity": {
            "allocatable": _money(charges_total),
            "allocated": _money(allocated_total),
            "unallocated": _money(unallocated_total),
            "non_allocatable": _money(non_allocatable_total),
            "holds": identity_holds,
            "difference": _money(identity_difference),
            "residual": _money(residual),
            "residual_limit": residual_limit or None,
            "residual_exceeded": residual_exceeded,
        },
        "allocations": allocations,
        "unallocated_items": unallocated_items,
        "non_allocatable_items": non_allocatable_items,
        "roi": roi,
        "effects": {
            "charges_delta": "none",
            "delivery": "UNKNOWN",
            "signoff": "none",
            "note": "分摊不改 Charges 差额、不改变 Delivery、不转移签核。",
        },
        "inputs": {
            "charges_hash": _sha(block),
            "driver_hash": _sha(driver_block),
            "input_fingerprint": _sha({"charges": block, "driver": driver_block,
                                       "policy": normalized["computed_policy_hash"]}),
        },
        "delivery_state": "UNKNOWN",
        "boundary": BOUNDARY,
    }
    return result


def _roi(policy: Dict[str, Any], economics: Optional[Dict[str, Any]],
         allocated_total: Decimal, charges_total: Decimal) -> Dict[str, Any]:
    if not economics:
        return {"value": None, "reason": "ECONOMICS_MISSING"}
    benefit_raw = economics.get("benefit_amount")
    cost_raw = economics.get("cost_amount")
    if benefit_raw in (None, "") or cost_raw in (None, ""):
        return {"value": None, "reason": "ECONOMICS_MISSING"}
    benefit = _dec(benefit_raw, "economics.benefit_amount")
    cost = _dec(cost_raw, "economics.cost_amount")
    if cost == 0:
        return {"value": None, "reason": "ZERO_COST"}
    value = (benefit / cost).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return {"value": format(value, "f"),
            "benefit_amount": _money(benefit),
            "cost_amount": _money(cost),
            "reason": None,
            "note": "ROI 是可复核比值，不代表因果归因。"}


def render_summary(result: Dict[str, Any]) -> str:
    totals = result.get("totals") or {}
    policy = result.get("policy") or {}
    lines = ["# 分摊结果 %s" % policy.get("policy_id"),
             "",
             "- 策略：v%s %s（%s）"
             % (policy.get("policy_version"), policy.get("policy_hash"),
                policy.get("state")),
             "- 状态：%s %s" % (result.get("status"),
                              ", ".join(result.get("status_reasons") or [])),
             "- 费用总额：%s %s；可分摊 %s；已分摊 %s；未分摊 %s；不可分摊 %s"
             % (totals.get("charges"), result.get("currency"),
                totals.get("eligible"), totals.get("allocated"),
                totals.get("unallocated"), totals.get("non_allocatable")),
             "- 恒等式：allocatable %s = allocated %s + unallocated %s + "
             "non_allocatable %s（%s，差 %s）"
             % ((result.get("identity") or {}).get("allocatable"),
                (result.get("identity") or {}).get("allocated"),
                (result.get("identity") or {}).get("unallocated"),
                (result.get("identity") or {}).get("non_allocatable"),
                "成立" if (result.get("identity") or {}).get("holds") else "不成立",
                (result.get("identity") or {}).get("difference")),
             "- 残差：%s（%s）"
             % ((result.get("identity") or {}).get("residual"),
                "超限" if (result.get("identity") or {}).get("residual_exceeded")
                else "未超限"),
             "- Delivery：%s（分摊不判断交付）" % result.get("delivery_state"),
             ""]
    roi = result.get("roi") or {}
    lines.append("- ROI：%s" % (roi.get("value") if roi.get("value") is not None
                                else "null（%s）" % roi.get("reason")))
    allocations = result.get("allocations") or []
    if allocations:
        lines.append("")
        lines.append("## 已分摊")
        for item in allocations:
            lines.append("- `%s` → %s：%s %s（driver %s %s，单位成本 %s）"
                         % (item["charge_id"], item["cost_center"],
                            item["amount"], item["currency"],
                            item["driver_value"],
                            (result.get("driver") or {}).get("unit"),
                            item["unit_cost"]))
    unallocated = result.get("unallocated_items") or []
    if unallocated:
        lines.append("")
        lines.append("## 未分摊")
        for item in unallocated:
            lines.append("- %s `%s`：%s（%s）"
                         % (item.get("amount"), item.get("charge_id") or "-",
                            item.get("cost_center") or "-", item.get("reason")))
    return "\n".join(lines).rstrip() + "\n"


def write_result(result: Dict[str, Any], out_dir: Path) -> Dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "allocation-result.json"
    csv_path = out / "allocation-lines.csv"
    md_path = out / "allocation-summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    columns = ("line_type", "charge_id", "cost_center", "driver_value",
               "amount", "currency", "unit_cost", "unit_cost_verifiable",
               "reason")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    rows = []
    for item in result.get("allocations") or []:
        rows.append(dict(item, line_type="allocated", reason=""))
    for item in result.get("unallocated_items") or []:
        rows.append(dict(item, line_type="unallocated", currency="",
                         driver_value="", unit_cost="",
                         unit_cost_verifiable=False))
    for item in result.get("non_allocatable_items") or []:
        rows.append(dict(item, line_type="non_allocatable", currency="",
                         cost_center=None, driver_value="", unit_cost="",
                         unit_cost_verifiable=False))
    for item in rows:
        row = {column: item.get(column) for column in columns}
        if isinstance(row["unit_cost_verifiable"], bool):
            row["unit_cost_verifiable"] = ("true" if row["unit_cost_verifiable"]
                                           else "false")
        writer.writerow({key: _csv_safe(value) for key, value in row.items()})
    csv_path.write_text(buffer.getvalue(), encoding="utf-8")
    md_path.write_text(render_summary(result), encoding="utf-8")
    return {"json": str(json_path), "csv": str(csv_path), "summary": str(md_path)}


def load_policy(path: Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise AllocationError("policy not found: %s" % p)
    try:
        payload = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AllocationError("policy is not JSON: %s" % exc,
                              "POLICY_INVALID") from exc
    return validate_policy(payload)


def load_json(path: Path) -> Any:
    p = Path(path)
    if not p.is_file():
        raise AllocationError("input not found: %s" % p)
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AllocationError("input is not JSON: %s" % exc,
                              "INPUT_INVALID") from exc


__all__ = ["SCHEMA", "RESULT_SCHEMA", "INPUT_SCHEMA", "GENERATOR",
           "GENERATOR_VERSION", "STATES", "RESULT_FIELDS", "AllocationError",
           "content", "policy_hash", "policy_state", "validate_policy",
           "check_policy", "revise_policy", "approve_policy",
           "classify_changes", "diff_policies", "allocate", "render_summary",
           "write_result", "load_policy", "load_json"]
