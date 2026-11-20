"""V-NEXT-1 late/duplicate usage events and billing replay.

This module is a deterministic, file-in/file-out replay over an authorized
usage export.  It never creates an invoice, never charges a card, never
mutates a close case, and never treats an unapproved rule map as a pricing
authority:

    duplicate key + same properties hash  -> duplicate, counted once, no-op
    duplicate key + different hash        -> conflict, never silently merged
    occurred_at < period start             -> late, new revision, old signoff kept
    missing/invalid price                 -> amount null plus a stable reason

Aggregates (count/sum/max/unique_count/latest/weighted_sum) must come from an
approved rule map.  Anything else keeps quantity visible but leaves the money
side empty instead of guessing.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA = "verovo.usage-replay/1"
RULE_MAP_SCHEMA = "verovo.usage-rule-map/1"
GENERATOR = "buyer_ledger.usage_replay"
GENERATOR_VERSION = "1"

AGGREGATES = ("count", "sum", "max", "unique_count", "latest", "weighted_sum")
DEDUPE_STATES = ("unique", "duplicate", "conflict")

BOUNDARY = (
    "L0 只读重放：只读取授权用量文件并写本地投影；不生成 invoice、不扣款、"
    "不写回 canonical、不修改已签核 revision。重复键只计一次，冲突不静默合并；"
    "缺有效价格时金额为 null 并保留原因，不用 0 或估算补齐。")

REQUIRED_EVENT_FIELDS = (
    "transaction_id", "external_subject_id", "metric_code", "occurred_at",
    "ingested_at", "source_ref", "source_hash")


class ReplayError(ValueError):
    """A replay input or rule violated a stable, reportable rule."""

    def __init__(self, message: str, code: str = "INPUT_REQUIRED") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _now_iso(now: Optional[datetime] = None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _sha_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_payload(value: Any) -> str:
    return _sha_text(_canonical(value))


def _parse_dt(value: Any, field: str) -> datetime:
    raw = _text(value).replace("Z", "+00:00")
    if not raw:
        raise ReplayError("%s is required" % field)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ReplayError("%s must be ISO 8601: %s" % (field, exc)) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReplayError("%s must carry a timezone offset" % field)
    return parsed


def _parse_dec(value: Any, field: str) -> Decimal:
    if value is None or _text(value) == "":
        raise ReplayError("%s is required" % field)
    if isinstance(value, float):
        raise ReplayError("refuse float in %s: pass a decimal string" % field)
    try:
        return Decimal(_text(value))
    except InvalidOperation as exc:
        raise ReplayError("%s is not a decimal string: %r" % (field, value)) from exc


def _dec_or_none(value: Any) -> Optional[Decimal]:
    if value is None or _text(value) == "":
        return None
    try:
        return Decimal(_text(value))
    except InvalidOperation:
        return None


def _money(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.0001")), "f")


def _csv_safe(value: Any) -> str:
    """Prevent spreadsheet formula injection without changing meaning."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


# ---------------------------------------------------------------------------
# input loading
# ---------------------------------------------------------------------------

def _load_json_events(path: Path) -> List[Dict[str, Any]]:
    raw = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        payload = json.loads(raw)
        if isinstance(payload, dict):
            payload = payload.get("events")
        if not isinstance(payload, list):
            raise ReplayError("%s must contain an event list" % path.name)
        return [dict(item) for item in payload]
    events = []
    for lineno, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayError("%s:%d is not JSON: %s" % (path.name, lineno, exc)) from exc
        if not isinstance(item, dict):
            raise ReplayError("%s:%d must be a JSON object" % (path.name, lineno))
        events.append(item)
    return events


def _load_csv_events(path: Path) -> List[Dict[str, Any]]:
    raw = path.read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(raw)))
    events = []
    for row in rows:
        item: Dict[str, Any] = {}
        for key, value in row.items():
            if key is None:
                continue
            item[key.strip()] = value
        props = item.get("properties")
        if props is not None:
            if _text(props) == "":
                item["properties"] = {}
            else:
                try:
                    item["properties"] = json.loads(props)
                except json.JSONDecodeError as exc:
                    raise ReplayError(
                        "properties must be a JSON object in CSV: %s" % exc) from exc
        events.append(item)
    return events


def load_events(path: Path) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        raise ReplayError("event file not found: %s" % p)
    if p.suffix.lower() == ".csv":
        return _load_csv_events(p)
    return _load_json_events(p)


def load_rule_map(path: Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise ReplayError("rule map not found: %s" % p)
    try:
        payload = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ReplayError("rule map is not JSON: %s" % exc) from exc
    if not isinstance(payload, dict):
        raise ReplayError("rule map must be a JSON object")
    if payload.get("schema") not in (None, RULE_MAP_SCHEMA):
        raise ReplayError("unsupported rule map schema: %s" % payload.get("schema"),
                          "RULE_MAP_INVALID")
    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ReplayError("rule map has no rules", "RULE_MAP_INVALID")
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ReplayError("each rule must be an object", "RULE_MAP_INVALID")
        rule_id = _text(rule.get("rule_id"))
        metric = _text(rule.get("metric_code"))
        if not rule_id or not metric:
            raise ReplayError("each rule needs rule_id and metric_code",
                              "RULE_MAP_INVALID")
        if rule_id in seen:
            raise ReplayError("duplicate rule_id: %s" % rule_id, "RULE_MAP_INVALID")
        seen.add(rule_id)
        if _text(rule.get("aggregate")) not in AGGREGATES:
            raise ReplayError("rule %s has an unsupported aggregate: %r"
                              % (rule_id, rule.get("aggregate")), "RULE_MAP_INVALID")
        if not _text(rule.get("unit")):
            raise ReplayError("rule %s needs a unit" % rule_id, "RULE_MAP_INVALID")
        if _text(rule.get("currency")):
            currency = _text(rule.get("currency")).upper()
            if len(currency) != 3:
                raise ReplayError("rule %s currency must be ISO-4217" % rule_id,
                                  "RULE_MAP_INVALID")
        if rule.get("unit_price") not in (None, ""):
            _parse_dec(rule.get("unit_price"), "rule %s unit_price" % rule_id)
    return payload


def _rule_approved(rule_map: Dict[str, Any]) -> bool:
    return bool(_text(rule_map.get("approved_by")) and _text(rule_map.get("approved_at")))


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def _normalize_event(raw: Dict[str, Any], index: int) -> Dict[str, Any]:
    missing = [field for field in REQUIRED_EVENT_FIELDS if not _text(raw.get(field))]
    if missing:
        raise ReplayError("event #%d is missing %s" % (index, ", ".join(missing)))
    properties = raw.get("properties")
    if properties is None:
        properties = {}
    if not isinstance(properties, dict):
        raise ReplayError("event #%d properties must be an object" % index)
    occurred = _parse_dt(raw.get("occurred_at"), "occurred_at")
    ingested = _parse_dt(raw.get("ingested_at"), "ingested_at")
    transaction_id = _text(raw.get("transaction_id"))
    dedupe_key = _text(raw.get("dedupe_key")) or transaction_id
    return {
        "transaction_id": transaction_id,
        "dedupe_key": dedupe_key,
        "external_subject_id": _text(raw.get("external_subject_id")),
        "metric_code": _text(raw.get("metric_code")),
        "occurred_at": occurred.isoformat(),
        "occurred_date": occurred.date().isoformat(),
        "ingested_at": ingested.isoformat(),
        "properties": properties,
        "properties_hash": _hash_payload(properties),
        "source_ref": _text(raw.get("source_ref")),
        "source_hash": _text(raw.get("source_hash")),
        "revision_id": _text(raw.get("revision_id")),
        "parent_revision_id": _text(raw.get("parent_revision_id")),
        "order": index,
    }


def _period_window(period: str) -> Tuple[date, date]:
    text = _text(period)
    try:
        start = date.fromisoformat(text + "-01")
    except ValueError as exc:
        raise ReplayError("period must be YYYY-MM (got %r)" % period) from exc
    end = date(start.year + (start.month // 12), (start.month % 12) + 1, 1)
    return start, end


def _rule_for(rule_map: Dict[str, Any], metric: str) -> Optional[Dict[str, Any]]:
    for rule in rule_map.get("rules") or []:
        if _text(rule.get("metric_code")) == metric:
            return rule
    return None


def _price_for(rule: Dict[str, Any], occurred: date) -> Tuple[Optional[Decimal], str]:
    raw = rule.get("unit_price")
    if raw in (None, ""):
        return None, "PRICE_MISSING"
    price = _parse_dec(raw, "unit_price")
    valid_from = _text(rule.get("valid_from"))
    valid_to = _text(rule.get("valid_to"))
    if valid_from and occurred < date.fromisoformat(valid_from):
        return None, "PRICE_NOT_VALID_FOR_OCCURRED_AT"
    if valid_to and occurred > date.fromisoformat(valid_to):
        return None, "PRICE_NOT_VALID_FOR_OCCURRED_AT"
    if not _text(rule.get("price_version")):
        return None, "PRICE_VERSION_MISSING"
    return price, ""


def _line_for(event: Dict[str, Any], rule: Dict[str, Any], *,
              rule_approved: bool, start: date, end: date,
              closed_at: Optional[datetime]) -> Dict[str, Any]:
    occurred = date.fromisoformat(event["occurred_date"])
    reasons: List[str] = []
    if not (start <= occurred < end):
        reasons.append("PERIOD_OUT_OF_WINDOW")
    if not rule_approved:
        reasons.append("RULE_MAP_NOT_APPROVED")
    event_unit = _text(event["properties"].get("unit")) or _text(rule.get("unit"))
    if event_unit != _text(rule.get("unit")):
        reasons.append("UNIT_MISMATCH")
    value_field = _text(rule.get("value_field")) or "quantity"
    raw_qty = event["properties"].get(value_field, event["properties"].get("quantity"))
    quantity = _dec_or_none(raw_qty)
    aggregate = _text(rule.get("aggregate"))
    if quantity is None and aggregate not in ("count", "unique_count"):
        reasons.append("QUANTITY_MISSING")
    price, price_reason = _price_for(rule, occurred)
    ingested = datetime.fromisoformat(event["ingested_at"])
    late = occurred < start or (closed_at is not None and ingested > closed_at)
    return {
        "transaction_id": event["transaction_id"],
        "dedupe_key": event["dedupe_key"],
        "external_subject_id": event["external_subject_id"],
        "metric_code": event["metric_code"],
        "occurred_at": event["occurred_at"],
        "ingested_at": event["ingested_at"],
        "source_ref": event["source_ref"],
        "source_hash": event["source_hash"],
        "properties_hash": event["properties_hash"],
        "unit": event_unit,
        "quantity": _money(quantity) if quantity is not None else None,
        "price_version": _text(rule.get("price_version")) or None,
        "unit_price": _money(price) if price is not None else None,
        "amount": None,
        "amount_reason": price_reason or None,
        "late": late,
        "included": not reasons,
        "exclusion_reason": reasons[0] if reasons else "",
        "exclusion_reasons": reasons,
        "dedupe_state": "unique",
    }


def _aggregate(metric: str, rule: Dict[str, Any],
               lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    aggregate = _text(rule.get("aggregate"))
    values = [Decimal(str(line["quantity"])) for line in lines
              if line.get("quantity") is not None]
    currency = _text(rule.get("currency")).upper() or None
    quantity: Optional[Decimal] = None
    unique_values: List[str] = []
    if aggregate == "count":
        quantity = Decimal(len(lines))
    elif aggregate == "sum":
        quantity = sum(values, Decimal("0"))
    elif aggregate == "max":
        quantity = max(values) if values else None
    elif aggregate == "unique_count":
        field = _text(rule.get("unique_field")) or "id"
        unique_values = sorted({_text(line.get("properties", {}).get(field))
                                for line in lines
                                if _text(line.get("properties", {}).get(field))})
        quantity = Decimal(len(unique_values))
    elif aggregate == "latest":
        ordered = sorted(lines, key=lambda item: item["occurred_at"])
        quantity = Decimal(str(ordered[-1]["quantity"])) if ordered else None
    elif aggregate == "weighted_sum":
        total = Decimal("0")
        for line, value in zip([l for l in lines if l.get("quantity") is not None], values):
            field = _text(rule.get("weight_field")) or "weight"
            weight = _dec_or_none(line.get("properties", {}).get(field))
            if weight is None:
                return {"metric_code": metric, "aggregate": aggregate,
                        "quantity": None, "unit": _text(rule.get("unit")),
                        "currency": currency, "amount": None,
                        "amount_reason": "WEIGHT_MISSING", "event_count": len(lines),
                        "unique_values": []}
            total += value * weight
        quantity = total
    priced = aggregate in ("count", "sum", "weighted_sum", "unique_count")
    price, price_reason = _price_for(rule, date.fromisoformat(
        lines[0]["occurred_at"][:10])) if lines else (None, "PRICE_MISSING")
    amount = None
    amount_reason = price_reason or None
    if priced and quantity is not None and price is not None:
        amount = quantity * price
    elif not priced:
        amount_reason = "AGGREGATE_NOT_PRICED"
    return {
        "metric_code": metric,
        "aggregate": aggregate,
        "rule_id": _text(rule.get("rule_id")),
        "quantity": _money(quantity) if quantity is not None else None,
        "unit": _text(rule.get("unit")),
        "currency": currency,
        "amount": _money(amount) if amount is not None else None,
        "amount_reason": amount_reason,
        "event_count": len(lines),
        "unique_values": unique_values,
        "price_version": _text(rule.get("price_version")) or None,
        "source_ref": _text(rule.get("source_ref")) or None,
    }


def replay(events: Iterable[Dict[str, Any]], *, period: str,
           rule_map: Dict[str, Any], closed_at: Optional[str] = None,
           previous_revision: str = "", generated_at: Optional[datetime] = None,
           source_files: Optional[List[Path]] = None) -> Dict[str, Any]:
    """Replay events into deterministic lines, metrics and a revision descriptor."""
    start, end = _period_window(period)
    closed = _parse_dt(closed_at, "closed_at") if _text(closed_at) else None
    approved = _rule_approved(rule_map)
    normalized: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    duplicates: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    lines: List[Dict[str, Any]] = []

    for index, raw in enumerate(events):
        event = _normalize_event(raw, index)
        prior = seen.get(event["dedupe_key"])
        if prior is not None:
            same = (prior["properties_hash"] == event["properties_hash"]
                    and prior["source_hash"] == event["source_hash"])
            if same:
                duplicates.append({
                    "dedupe_key": event["dedupe_key"],
                    "transaction_id": event["transaction_id"],
                    "source_ref": event["source_ref"],
                    "state": "duplicate",
                    "note": "same key and same hash; counted once",
                })
                normalized.append(event)
                continue
            conflicts.append({
                "dedupe_key": event["dedupe_key"],
                "transaction_id": event["transaction_id"],
                "source_ref": event["source_ref"],
                "state": "conflict",
                "properties_hash": event["properties_hash"],
                "prior_properties_hash": prior["properties_hash"],
                "note": "same key with a different hash; not merged",
            })
            event["dedupe_state"] = "conflict"
            normalized.append(event)
            rule = _rule_for(rule_map, event["metric_code"])
            if rule:
                line = _line_for(event, rule, rule_approved=approved,
                                 start=start, end=end, closed_at=closed)
                line["included"] = False
                line["dedupe_state"] = "conflict"
                line["exclusion_reason"] = "DEDUPE_CONFLICT"
                line["exclusion_reasons"] = ["DEDUPE_CONFLICT"]
                lines.append(line)
            else:
                lines.append({
                    "transaction_id": event["transaction_id"],
                    "dedupe_key": event["dedupe_key"],
                    "metric_code": event["metric_code"],
                    "occurred_at": event["occurred_at"],
                    "ingested_at": event["ingested_at"],
                    "source_ref": event["source_ref"],
                    "source_hash": event["source_hash"],
                    "properties_hash": event["properties_hash"],
                    "quantity": None, "unit": None, "unit_price": None,
                    "price_version": None, "amount": None,
                    "amount_reason": None, "late": False, "included": False,
                    "dedupe_state": "conflict",
                    "exclusion_reason": "DEDUPE_CONFLICT",
                    "exclusion_reasons": ["DEDUPE_CONFLICT"],
                })
            continue
        seen[event["dedupe_key"]] = event
        normalized.append(event)
        rule = _rule_for(rule_map, event["metric_code"])
        if rule is None:
            lines.append({
                "transaction_id": event["transaction_id"],
                "dedupe_key": event["dedupe_key"],
                "metric_code": event["metric_code"],
                "occurred_at": event["occurred_at"],
                "ingested_at": event["ingested_at"],
                "source_ref": event["source_ref"],
                "source_hash": event["source_hash"],
                "properties_hash": event["properties_hash"],
                "quantity": None, "unit": None, "unit_price": None,
                "price_version": None, "amount": None,
                "amount_reason": None, "late": False, "included": False,
                "dedupe_state": "unique",
                "exclusion_reason": "METRIC_NOT_IN_RULE_MAP",
                "exclusion_reasons": ["METRIC_NOT_IN_RULE_MAP"],
            })
            continue
        lines.append(_line_for(event, rule, rule_approved=approved,
                               start=start, end=end, closed_at=closed))

    # Carry the properties through for unique/weighted aggregates without
    # exposing them in the line projection.
    by_key = {event["transaction_id"]: event for event in normalized}
    for line in lines:
        event = by_key.get(line.get("transaction_id"))
        if event:
            line["properties"] = event["properties"]

    included = [line for line in lines if line.get("included")]
    metrics: List[Dict[str, Any]] = []
    aggregation_state = "OK" if approved else "RULE_MAP_NOT_APPROVED"
    if approved:
        for rule in rule_map.get("rules") or []:
            metric_lines = [line for line in included
                            if line["metric_code"] == _text(rule.get("metric_code"))]
            if metric_lines:
                metrics.append(_aggregate(_text(rule.get("metric_code")), rule,
                                          metric_lines))
    for line in lines:
        line.pop("properties", None)

    late_lines = [line for line in lines if line.get("late")]
    input_fingerprint = _hash_payload({
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "closed_at": closed.isoformat() if closed else None,
        "rule_map": rule_map,
        "events": [{key: value for key, value in event.items()
                    if key not in ("order",)} for event in normalized],
    })
    revision_id = "ur-" + hashlib.sha256(input_fingerprint.encode("utf-8")).hexdigest()[:12]
    effect = "new_revision" if late_lines else "no_change"
    return {
        "schema": SCHEMA,
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "generated_at": _now_iso(generated_at),
        "input_fingerprint": input_fingerprint,
        "period": {"from": start.isoformat(), "to": end.isoformat(),
                   "to_exclusive": True},
        "closed_at": closed.isoformat() if closed else None,
        "rule_map": {"hash": _hash_payload(rule_map),
                     "approved": approved,
                     "approved_by": _text(rule_map.get("approved_by")) or None,
                     "approved_at": _text(rule_map.get("approved_at")) or None},
        "source_files": [str(path) for path in (source_files or [])],
        "counts": {
            "received": len(normalized),
            "included": len(included),
            "duplicates": len(duplicates),
            "conflicts": len(conflicts),
            "excluded": len([line for line in lines if not line.get("included")]),
            "late": len(late_lines),
        },
        "duplicates": duplicates,
        "conflicts": conflicts,
        "lines": lines,
        "metrics": metrics,
        "aggregation_state": aggregation_state,
        "revision": {
            "revision_id": revision_id,
            "parent_revision_id": previous_revision or None,
            "effect": effect,
            "late_event_count": len(late_lines),
            "previous_signoff_preserved": True,
            "note": ("late events create a new replay revision; the existing "
                     "close revision and its signoff are never rewritten")
            if late_lines else "same input, no new revision",
        },
        "delivery_state": "UNKNOWN",
        "delivery_note": ("用量重放只覆盖计费口径；交付与验收没有独立证据，"
                          "保持 UNKNOWN。"),
        "boundary": BOUNDARY,
    }


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def diff_replays(current: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Any]:
    def _by_key(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {line.get("dedupe_key") or line.get("transaction_id"): line
                for line in payload.get("lines") or []}

    old = _by_key(previous)
    new = _by_key(current)
    added = [key for key in new if key not in old]
    removed = [key for key in old if key not in new]
    changed = []
    for key in new:
        if key in old:
            before, after = old[key], new[key]
            fields = [field for field in ("quantity", "amount", "included",
                                          "exclusion_reason", "occurred_at")
                      if before.get(field) != after.get(field)]
            if fields:
                changed.append({"key": key, "fields": fields,
                                "before": {f: before.get(f) for f in fields},
                                "after": {f: after.get(f) for f in fields}})
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "counts_before": previous.get("counts"),
        "counts_after": current.get("counts"),
        "note": ("差异只描述重放结果变化，不归责、不推断原因；"
                 "以来源文件与规则版本复核。"),
    }


def render_diff(diff: Dict[str, Any]) -> str:
    lines = ["# 用量重放差异", "",
             diff.get("note", ""), "",
             "- 新增行：%d" % len(diff.get("added") or []),
             "- 移除行：%d" % len(diff.get("removed") or []),
             "- 变化行：%d" % len(diff.get("changed") or []), ""]
    if diff.get("added"):
        lines.append("## 新增")
        lines.extend("- `%s`" % key for key in diff["added"])
        lines.append("")
    if diff.get("removed"):
        lines.append("## 移除")
        lines.extend("- `%s`" % key for key in diff["removed"])
        lines.append("")
    if diff.get("changed"):
        lines.append("## 变化")
        for item in diff["changed"]:
            lines.append("- `%s`：%s" % (item["key"], ", ".join(item["fields"])))
            for field in item["fields"]:
                lines.append("  - %s: %r → %r" % (field, item["before"].get(field),
                                                  item["after"].get(field)))
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------

def _write_csv(payload: Dict[str, Any], path: Path) -> None:
    columns = ("transaction_id", "external_subject_id", "metric_code",
               "occurred_at", "ingested_at", "quantity", "unit", "unit_price",
               "price_version", "amount", "amount_reason", "included",
               "exclusion_reason", "dedupe_state", "source_ref", "source_hash")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for line in payload.get("lines") or []:
        row = {column: line.get(column) for column in columns}
        row["included"] = "true" if line.get("included") else "false"
        writer.writerow({key: _csv_safe(value) for key, value in row.items()})
    path.write_text(buffer.getvalue(), encoding="utf-8")


def write_replay(payload: Dict[str, Any], out_dir: Path, *,
                 diff: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "usage-replay.json"
    csv_path = out / "usage-replay-lines.csv"
    md_path = out / "replay-diff.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    _write_csv(payload, csv_path)
    md_path.write_text(render_diff(diff or {"added": [], "removed": [], "changed": [],
                                            "note": "首次重放：无上一版可比较。"}),
                       encoding="utf-8")
    return {"json": str(json_path), "csv": str(csv_path), "diff": str(md_path)}


__all__ = ["SCHEMA", "RULE_MAP_SCHEMA", "GENERATOR", "GENERATOR_VERSION",
           "AGGREGATES", "ReplayError", "load_events", "load_rule_map",
           "replay", "diff_replays", "render_diff", "write_replay"]
