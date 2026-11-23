"""Project an existing Audit Case into a buyer spend-action worklist."""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


CENT = Decimal("0.01")
ACTION_ORDER = (
    "evidence_required",
    "review_required",
    "unreviewed",
    "ready_for_payment",
    "settled",
)
EVIDENCE_CLASSIFICATIONS = {
    "missing_evidence",
    "currency_mismatch",
    "unverified_outcome",
}
OPEN_STATUSES = {"identified", "validated", "accepted"}
CLOSED_STATUSES = {"realized", "dismissed"}
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class SpendActionError(ValueError):
    pass


def _money(value: Any, field: str) -> str:
    if value is None or isinstance(value, bool):
        raise SpendActionError("%s: amount is UNKNOWN, not zero" % field)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise SpendActionError("%s: amount is not decimal" % field) from None
    if not number.is_finite():
        raise SpendActionError("%s: amount must be finite" % field)
    return str(number.quantize(CENT, rounding=ROUND_HALF_UP))


def _currency(value: Any, field: str) -> str:
    if not isinstance(value, str) or not CURRENCY_RE.fullmatch(value):
        raise SpendActionError("%s: currency must be a three-letter code" % field)
    return value


def _inside(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise SpendActionError("invoice path must be a non-empty string")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise SpendActionError("invoice path escapes the case root: %s" % relative) from None
    if not candidate.is_file():
        raise SpendActionError("invoice path does not exist: %s" % relative)
    return candidate


def _validate_case(case: Any) -> None:
    if not isinstance(case, dict):
        raise SpendActionError("audit case must be an object")
    for field in ("case_id", "buyer", "vendor", "period", "coverage", "findings", "status"):
        if field not in case:
            raise SpendActionError("audit case is missing %s" % field)
    if not isinstance(case["case_id"], str) or not case["case_id"].strip():
        raise SpendActionError("case_id must be a non-empty string")
    if not isinstance(case["findings"], list):
        raise SpendActionError("findings must be an array")
    coverage = case["coverage"]
    if not isinstance(coverage, dict):
        raise SpendActionError("coverage must be an object")
    _currency(coverage.get("currency"), "coverage.currency")


def _invoice_paths(case: Dict[str, Any], manifest: Optional[Dict[str, Any]]) -> List[str]:
    paths = set()
    if manifest:
        sources = manifest.get("sources", {})
        if isinstance(sources, dict):
            invoices = sources.get("invoices", [])
            if isinstance(invoices, str):
                invoices = [invoices]
            if not isinstance(invoices, list):
                raise SpendActionError("manifest sources.invoices must be an array")
            paths.update(invoices)
        clean = manifest.get("clean_charge_lines", [])
        if not isinstance(clean, list) or any(
            not isinstance(line_id, str) or not line_id for line_id in clean
        ):
            raise SpendActionError("manifest clean_charge_lines must be an array of strings")
    for finding in case["findings"]:
        if not isinstance(finding, dict):
            raise SpendActionError("each finding must be an object")
        affected = finding.get("affected_lines", [])
        if not isinstance(affected, list):
            raise SpendActionError("affected_lines must be an array")
        for line in affected:
            if isinstance(line, dict) and "path" in line:
                paths.add(line["path"])
        for ref in finding.get("evidence_refs", []):
            if isinstance(ref, dict) and str(ref.get("path", "")).endswith(".json"):
                paths.add(ref["path"])
    return sorted(paths)


def _load_invoice_lines(root: Path, paths: List[str]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    lines = {}
    for relative in paths:
        document_path = _inside(root, relative)
        try:
            document = json.loads(document_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SpendActionError("cannot read invoice %s: %s" % (relative, exc)) from exc
        if not isinstance(document, dict) or "lines" not in document:
            continue
        if not isinstance(document["lines"], list):
            raise SpendActionError("%s.lines must be an array" % relative)
        for line in document["lines"]:
            if not isinstance(line, dict) or not isinstance(line.get("line_id"), str):
                raise SpendActionError("%s contains a line without line_id" % relative)
            key = (relative, line["line_id"])
            if key in lines:
                raise SpendActionError("duplicate invoice line %s#%s" % key)
            amount = _money(line.get("amount"), "%s#%s.amount" % key)
            currency = _currency(line.get("currency"), "%s#%s.currency" % key)
            lines[key] = {
                "source": {
                    "kind": "invoice_line",
                    "path": relative,
                    "line_id": line["line_id"],
                },
                "charge_id": line.get("charge_id"),
                "amount": {"currency": currency, "value": amount},
            }
    return lines


def _finding_id(finding: Dict[str, Any]) -> str:
    finding_id = finding.get("finding_id")
    if not isinstance(finding_id, str) or not finding_id:
        raise SpendActionError("finding_id must be a non-empty string")
    return finding_id


def _finding_status(finding: Dict[str, Any]) -> str:
    status = finding.get("status")
    if status not in OPEN_STATUSES | CLOSED_STATUSES:
        raise SpendActionError("unsupported finding status: %r" % status)
    return status


def _classification(finding: Dict[str, Any]) -> str:
    classification = finding.get("classification")
    if not isinstance(classification, str) or not classification:
        raise SpendActionError("finding classification must be a non-empty string")
    return classification


def _reason(finding: Dict[str, Any]) -> str:
    reason = finding.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason
    amount = finding.get("amount", {})
    if isinstance(amount, dict) and isinstance(amount.get("basis"), str):
        return amount["basis"]
    return _classification(finding)


def _owner(action: str, classification: Optional[str]) -> str:
    if action == "ready_for_payment":
        return "accounts_payable"
    if action == "evidence_required":
        return "buyer_finance" if classification == "currency_mismatch" else "buyer_operations"
    if action == "review_required":
        return "buyer_finance" if classification == "shared_cost_allocation" else "procurement"
    return "buyer_finance"


def _action_for_finding(finding: Dict[str, Any]) -> str:
    if finding.get("provable") is False or _classification(finding) in EVIDENCE_CLASSIFICATIONS:
        return "evidence_required"
    return "review_required"


def _entry(entry_id: str, source: Dict[str, Any], action: str, reason: str,
           finding_ids: List[str], amount: Optional[Dict[str, str]],
           classification: Optional[str]) -> Dict[str, Any]:
    return {
        "entry_id": entry_id,
        "source": source,
        "charge_id": None,
        "action": action,
        "owner_role": _owner(action, classification),
        "reason": reason,
        "finding_ids": finding_ids,
        "amount": amount,
    }


def _summarize(actions: List[Dict[str, Any]], default_currency: str) -> Dict[str, Any]:
    counts = {action: 0 for action in ACTION_ORDER}
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in actions:
        action = row["action"]
        counts[action] += 1
        amount = row["amount"]
        currency = amount["currency"] if amount else default_currency
        key = (action, currency)
        bucket = grouped.setdefault(key, {
            "action": action,
            "currency": currency,
            "known_value": Decimal("0.00"),
            "known_count": 0,
            "unknown_count": 0,
        })
        if amount is None:
            bucket["unknown_count"] += 1
        else:
            bucket["known_value"] += Decimal(amount["value"])
            bucket["known_count"] += 1
    amounts = []
    for key in sorted(grouped, key=lambda item: (ACTION_ORDER.index(item[0]), item[1])):
        bucket = grouped[key]
        known_count = bucket.pop("known_count")
        bucket["known_value"] = str(bucket["known_value"]) if known_count else None
        amounts.append(bucket)
    return {
        "counts": {action: counts[action] for action in ACTION_ORDER if counts[action]},
        "amounts": amounts,
    }


def build_action_list(case: Dict[str, Any], case_root: Path,
                      manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return a read-only verovo.spend-action-list/0.1 projection."""
    _validate_case(case)
    root = Path(case_root).resolve()
    default_currency = _currency(case["coverage"]["currency"], "coverage.currency")
    lines = _load_invoice_lines(root, _invoice_paths(case, manifest))
    clean = set(manifest.get("clean_charge_lines", [])) if manifest else set()
    open_by_line: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    realized_by_line: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    case_actions = []

    for finding in case["findings"]:
        finding_id = _finding_id(finding)
        status = _finding_status(finding)
        classification = _classification(finding)
        affected = finding.get("affected_lines", [])
        if not affected and status in OPEN_STATUSES:
            action = _action_for_finding(finding)
            finding_amount = finding.get("amount", {})
            value = finding_amount.get("value") if isinstance(finding_amount, dict) else None
            amount = None
            if value is not None:
                amount = {
                    "currency": _currency(finding_amount.get("currency"), "%s.currency" % finding_id),
                    "value": _money(value, "%s.amount.value" % finding_id),
                }
            case_actions.append(_entry(
                "finding:%s" % finding_id,
                {"kind": "finding", "finding_id": finding_id},
                action,
                _reason(finding),
                [finding_id],
                amount,
                classification,
            ))
        for affected_line in affected:
            if not isinstance(affected_line, dict):
                raise SpendActionError("affected line must be an object")
            key = (affected_line.get("path"), affected_line.get("line_id"))
            if not all(isinstance(part, str) and part for part in key):
                raise SpendActionError("affected line needs path and line_id")
            if status in OPEN_STATUSES:
                open_by_line.setdefault(key, []).append(finding)
            elif status == "realized":
                realized_by_line.setdefault(key, []).append(finding)

    actions = list(case_actions)
    for key, line in lines.items():
        open_findings = open_by_line.get(key, [])
        realized_findings = realized_by_line.get(key, [])
        path, line_id = key
        if open_findings:
            primary = sorted(
                open_findings,
                key=lambda finding: (
                    _action_for_finding(finding) != "evidence_required",
                    _finding_id(finding),
                ),
            )[0]
            action = _action_for_finding(primary)
            reason = "; ".join(_reason(finding) for finding in open_findings)
            classification = _classification(primary)
        elif realized_findings:
            action = "settled"
            reason = "Settlement evidence is recorded; no duplicate payment action is proposed."
            classification = None
        elif line_id in clean:
            action = "ready_for_payment"
            reason = "The line is explicitly confirmed clean in the period manifest; the buyer still decides whether to pay."
            classification = None
        else:
            action = "unreviewed"
            reason = "The line is not explicitly confirmed clean and has no affected-line finding; absence of a finding is not payment clearance."
            classification = None
        finding_ids = sorted(
            _finding_id(finding) for finding in open_findings + realized_findings
        )
        row = _entry(
            "invoice:%s#%s" % key,
            line["source"],
            action,
            reason,
            finding_ids,
            line["amount"],
            classification,
        )
        row["charge_id"] = line["charge_id"]
        actions.append(row)

    actions.sort(key=lambda row: (
        ACTION_ORDER.index(row["action"]),
        row["source"]["kind"],
        row["source"].get("path", ""),
        row["source"].get("line_id", row["source"].get("finding_id", "")),
    ))
    return {
        "schema": "verovo.spend-action-list/0.1",
        "case_id": case["case_id"],
        "buyer": case["buyer"],
        "vendor": case["vendor"],
        "period": case["period"],
        "actions": actions,
        "summary": _summarize(actions, default_currency),
        "executes_payment": False,
        "buyer_decision_required": True,
    }


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def action_markdown(result: Dict[str, Any]) -> str:
    """Render the worklist without turning it into a payment instruction."""
    output = [
        "# Spend action list — %s / %s" % (result["vendor"], result["case_id"]),
        "",
        "Buyer review worklist only. This report does not execute or authorize payment.",
        "",
        "| action | owner | source | amount | reason |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in result["actions"]:
        source = row["source"]
        label = (
            "%s#%s" % (source["path"], source["line_id"])
            if source["kind"] == "invoice_line"
            else source["finding_id"]
        )
        amount = row["amount"]
        amount_text = "%s %s" % (amount["currency"], amount["value"]) if amount else "UNKNOWN"
        output.append("| %s | %s | %s | %s | %s |" % (
            _md(row["action"]),
            _md(row["owner_role"]),
            _md(label),
            _md(amount_text),
            _md(row["reason"]),
        ))
    output.extend(["", "## Counts", ""])
    for action, count in result["summary"]["counts"].items():
        output.append("- **%s**: %d" % (action, count))
    output.extend([
        "",
        "Amounts remain separated by action and currency. UNKNOWN counts are not zero.",
        "Record the buyer's actual payment, hold, evidence request or vendor review separately.",
    ])
    return "\n".join(output) + "\n"
