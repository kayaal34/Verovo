"""V-08 OCR/paste bill candidate intake: the golden chain's front door.

    bill page / screenshot / pasted text
      -> OCR candidate draft (never booked automatically)
      -> field-level evidence: vendor / invoice_no / period / currency /
         amount / line items, each pointing back at a region of the
         screenshot (bbox) or a span of the text (line + char range)
      -> human field review: key fields and flagged fields queue up; nothing
         enters canonical while the queue is non-empty; high confidence still
         only means draft
      -> booking is an explicit human act: the reviewed invoice is exported
         as a bills CSV + verbatim document + verovo.period-job/1 for the
         existing close chain (ingest -> mapping confirm -> calculate ->
         report -> review -> signoff, where signoff still != payment)

Discipline (后续优化评审-20261117 §4.3, §3.1, §5, §7):

- an OCR draft never books itself (AC V11 stays intact one level earlier);
- key fields without an evidence region queue for review and are never
  guessed;
- bad OCR (garbled, nothing extractable) ends BLOCKED_USER_INPUT with one
  concrete user action; the garbled draft is preserved, no fake precision;
- a duplicate invoice_no never double-books;
- PASS/FAIL/UNPROVABLE stay verification conclusions inside the close
  report; they are never delivery terminal states;
- a paid OCR engine is a ``requires_procurement`` branch (structured JSON,
  ``transaction_executed=false``), never a silent failure or a purchase.

No network access, no provider calls, no booking of anything.
"""
from __future__ import annotations

import csv
import json
import platform
import re
import shutil
import subprocess
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .close import CloseError, _validate_job

CANDIDATE_SCHEMA = "verovo.bill-candidate/1"
OCR_LINES_SCHEMA = "verovo.ocr-lines/1"

#: §3.1 delivery terminal states. UNPROVABLE is a verification conclusion of
#: the close report and deliberately absent from this vocabulary.
TERMINAL_STATES = ("DELIVERED", "DELIVERED_WITH_GAP", "BLOCKED_USER_INPUT",
                   "UNSUPPORTED", "FAILED_UNRESOLVED")
VERIFICATION_CONCLUSIONS = ("PASS", "FAIL", "UNPROVABLE")

#: Key fields always queue for human review, whatever the OCR confidence.
KEY_FIELDS = ("vendor", "invoice_no", "period", "currency", "amount")
#: Below this confidence a non-key field (line items) queues as well.
REVIEW_THRESHOLD = 0.90

ENGINES = ("text", "vision", "paid-cloud-ocr")

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}

_INVOICE_NO_RE = re.compile(
    r"\b(?:invoice|inv)\s*(?:no\.?|number|num|id|#)?\s*[:#]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9\-/]{2,})", re.IGNORECASE)
_VENDOR_RE = re.compile(
    r"\b(vendor|supplier|provider|seller|issued\s+by)\s*[:：]\s*(\S.*?)\s*$",
    re.IGNORECASE)
_TOTAL_RE = re.compile(
    r"\b(?:grand\s+total|total(?:\s+(?:amount|due|payable))?|amount\s+(?:due|payable)|"
    r"balance\s+due)\s*[:：]?\s*([$¥€£]?)\s*([0-9][\d,]*(?:\.\d+)?)", re.IGNORECASE)
_CURRENCY_RE = re.compile(r"\b([A-Za-z]{3})\b")
#: currency symbols that map to exactly one currency; $ and ¥ stay ambiguous.
_SYMBOL_CURRENCIES = {"€": "EUR", "£": "GBP"}
_PERIOD_RANGE_RE = re.compile(
    r"\b(?:billing\s+period|service\s+period|invoice\s+period|period)\s*[:：]?\s*"
    r"(\d{4}-\d{2}-\d{2})\s*(?:-|–|—|~|\bto\b)\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
_PERIOD_MONTH_RE = re.compile(
    r"\b(?:billing\s+period|service\s+period|invoice\s+month|period|month)\s*[:：]?\s*"
    r"(\d{4}-\d{2})\b", re.IGNORECASE)
_ITEM_LINE_RE = re.compile(
    r"^(?P<desc>\S.*?)\s{2,}(?P<qty>\d[\d,]*)\s+(?P<unit_price>\d+(?:\.\d+)?)\s+"
    r"(?P<amount>\d[\d,]*\.\d{2})\s*$")
_ITEM_HEADER_RE = re.compile(r"quantity|unit\s*price|description", re.IGNORECASE)

#: chars that still carry information when judging OCR quality
_INFORMATIVE_RE = re.compile(r"[A-Za-z0-9\u3400-\u9fff]")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _next_month(month_first: date) -> date:
    from datetime import datetime as _dt
    return _dt(month_first.year + (month_first.month // 12),
               (month_first.month % 12) + 1, 1).date()


def _normalize_period(value: Any) -> Dict[str, Any]:
    """Normalize a review input into the half-open convention.

    Accepted: ``YYYY-MM`` (whole month), ``from..to`` / ``from - to`` where
    ``to`` is already the exclusive end (that is what ``show`` displays), or
    a dict with from/to. Extraction from invoice text converts the inclusive
    display range itself (``_period_from_text``).
    """
    if isinstance(value, dict):
        start = _text(value.get("from"))
        end = _text(value.get("to"))
        if not (start and end):
            raise ValueError("period dict needs from/to")
    else:
        raw = _text(value)
        if not raw:
            raise ValueError("period value is empty")
        month = re.fullmatch(r"(\d{4}-\d{2})", raw)
        if month:
            first = date.fromisoformat(month.group(1) + "-01")
            start, end = first.isoformat(), _next_month(first).isoformat()
        else:
            match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*(?:\.\.|-|–|—|~|to)?\s*"
                                 r"(\d{4}-\d{2}-\d{2})", raw, re.IGNORECASE)
            if not match:
                raise ValueError(
                    "period must be YYYY-MM or from..to (ISO dates, to exclusive)")
            start, end = match.group(1), match.group(2)
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError as exc:
        raise ValueError("period dates must be ISO dates") from exc
    if start_date >= end_date:
        raise ValueError("period.from must be earlier than period.to")
    return {"from": start_date.isoformat(), "to": end_date.isoformat(),
            "to_exclusive": True}


def _period_from_text(start_raw: str, end_raw: str) -> Dict[str, Any]:
    """Invoice text shows an inclusive display range; convert to half-open."""
    from datetime import timedelta
    start = date.fromisoformat(start_raw)
    end = date.fromisoformat(end_raw)
    if start > end:
        raise ValueError("period start is after its end")
    return {"from": start.isoformat(),
            "to": (end + timedelta(days=1)).isoformat(),
            "to_exclusive": True}


def _normalize_amount(value: Any) -> str:
    raw = _text(value).replace(",", "").replace("$", "").replace("¥", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?", raw):
        raise ValueError("amount must be a plain decimal number")
    try:
        Decimal(raw)
    except InvalidOperation as exc:  # pragma: no cover - regex already guards
        raise ValueError("amount is not a decimal") from exc
    return raw


def _normalize_currency(value: Any) -> str:
    raw = _text(value).upper()
    if not re.fullmatch(r"[A-Z]{3}", raw):
        raise ValueError("currency must be a three-letter code")
    return raw


# ---------------------------------------------------------------------------
# OCR quality and field extraction
# ---------------------------------------------------------------------------

def _looks_garbled(lines: List[Dict[str, Any]]) -> Optional[str]:
    """Return a reason string when the OCR output is unusable, else None."""
    joined = "\n".join(_text(line.get("text")) for line in lines)
    non_space = [ch for ch in joined if not ch.isspace()]
    informative = len(_INFORMATIVE_RE.findall(joined))
    if non_space and informative / len(non_space) < 0.6:
        return ("the recognized text is mostly non-informative characters "
                "(informative ratio below 0.6)")
    if informative < 12:
        return ("the recognized text carries fewer than 12 informative "
                "characters; there is nothing to extract")
    return None


def _evidence(line_record: Dict[str, Any], index: int, start: int, end: int,
              from_image: bool) -> Optional[Dict[str, Any]]:
    text = _text(line_record.get("text"))
    bbox = line_record.get("bbox")
    evidence: Dict[str, Any] = {
        "kind": "text_span", "line": index + 1,
        "char_start": start, "char_end": end,
        "excerpt": text[max(0, start - 24):min(len(text), end + 24)],
    }
    if from_image:
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4 \
                and all(isinstance(v, (int, float)) for v in bbox):
            evidence["kind"] = "image_region"
            evidence["bbox"] = [float(v) for v in bbox]
        # no bbox on an image input: the span points into the OCR payload
        # only, which is not a region of the screenshot -> flagged by caller.
    return evidence


def _has_region(evidence: Optional[Dict[str, Any]], from_image: bool) -> bool:
    if not evidence:
        return False
    if not from_image:
        return evidence.get("kind") == "text_span" and \
            evidence.get("char_end", 0) > evidence.get("char_start", -1)
    return evidence.get("kind") == "image_region" and "bbox" in evidence


def _field(name: str, value: Any, line_record: Dict[str, Any], index: int,
           start: int, end: int, from_image: bool, key: bool = False,
           extra_flags: Optional[List[str]] = None) -> Dict[str, Any]:
    evidence = _evidence(line_record, index, start, end, from_image)
    flags = list(extra_flags or [])
    if not _has_region(evidence, from_image):
        flags.append("missing_evidence")
    confidence = float(line_record.get("confidence", 1.0) or 0.0)
    if confidence < REVIEW_THRESHOLD:
        flags.append("low_confidence")
    return {
        "name": name, "value": value, "confidence": round(confidence, 3),
        "key": key, "flags": sorted(set(flags)), "evidence": evidence,
        "review": None, "status": "pending_review",
    }


_KNOWN_CURRENCIES = {"USD", "EUR", "CNY", "GBP", "JPY", "CAD", "AUD", "CHF",
                     "HKD", "SGD", "SEK", "NOK", "KRW", "INR"}


def _extract_fields(lines: List[Dict[str, Any]],
                    from_image: bool) -> List[Dict[str, Any]]:
    """Deterministic extraction; nothing is inferred without a matched span."""
    fields: List[Dict[str, Any]] = []
    found = set()

    for index, line_record in enumerate(lines):
        text = _text(line_record.get("text"))
        if not text:
            continue
        if "invoice_no" not in found:
            match = _INVOICE_NO_RE.search(text)
            if match:
                fields.append(_field("invoice_no", match.group(1).upper(),
                                     line_record, index, match.start(1),
                                     match.end(1), from_image, key=True))
                found.add("invoice_no")
                continue
        if "vendor" not in found:
            match = _VENDOR_RE.search(text)
            if match:
                fields.append(_field("vendor", match.group(2), line_record,
                                     index, match.start(2), match.end(2),
                                     from_image, key=True))
                found.add("vendor")
                continue
        if "period" not in found:
            match = _PERIOD_RANGE_RE.search(text)
            if match:
                period = _period_from_text(match.group(1), match.group(2))
                fields.append(_field("period", period, line_record, index,
                                     match.start(1), match.end(2),
                                     from_image, key=True))
                found.add("period")
                continue
            match = _PERIOD_MONTH_RE.search(text)
            if match:
                month = date.fromisoformat(match.group(1) + "-01")
                period = {"from": month.isoformat(),
                          "to": _next_month(month).isoformat(),
                          "to_exclusive": True}
                fields.append(_field("period", period, line_record, index,
                                     match.start(1), match.end(1),
                                     from_image, key=True))
                found.add("period")
                continue
        if "amount" not in found:
            match = _TOTAL_RE.search(text)
            if match:
                symbol = match.group(1) or ""
                fields.append(_field("amount", _normalize_amount(match.group(2)),
                                     line_record, index, match.start(2),
                                     match.end(2), from_image, key=True))
                found.add("amount")
                # currency lives on the total line: a known three-letter code
                # adjacent to the number, or an unambiguous symbol. Anything
                # else stays null for a human; "$"/"¥" are ambiguous.
                currency = None
                span = (match.start(2), match.end(2))
                flags: List[str] = []
                code = _CURRENCY_RE.search(text, match.end()) or \
                    _CURRENCY_RE.search(text, max(match.start() - 8, 0),
                                        match.start())
                if code and code.group(1).upper() in _KNOWN_CURRENCIES:
                    currency = code.group(1).upper()
                    span = (code.start(1), code.end(1))
                elif symbol in _SYMBOL_CURRENCIES:
                    currency = _SYMBOL_CURRENCIES[symbol]
                    span = (match.start(1), match.end(1))
                elif symbol:
                    flags = ["ambiguous_symbol"]
                else:
                    flags = ["not_found_on_total_line"]
                fields.append(_field("currency", currency, line_record, index,
                                     span[0], span[1], from_image, key=True,
                                     extra_flags=flags))
                found.add("currency")
                continue

    # line items are optional; they are evidence material, not key fields.
    items: List[Dict[str, Any]] = []
    for index, line_record in enumerate(lines):
        text = _text(line_record.get("text"))
        if not text or _ITEM_HEADER_RE.search(text):
            continue
        match = _ITEM_LINE_RE.match(text)
        if not match:
            continue
        items.append({
            "description": match.group("desc").strip(),
            "quantity": match.group("qty").replace(",", ""),
            "unit_price": match.group("unit_price"),
            "amount": match.group("amount").replace(",", ""),
            "confidence": round(float(line_record.get("confidence", 1.0) or 0.0), 3),
            "evidence": _evidence(line_record, index, match.start(), match.end(),
                                  from_image),
        })
    if items:
        item_confidence = min(item["confidence"] for item in items)
        sample_line = items[0]["evidence"]["line"] - 1
        field = _field("line_items", items, lines[sample_line], sample_line,
                       0, len(_text(lines[sample_line].get("text"))) - 1,
                       from_image)
        field["confidence"] = item_confidence
        fields.append(field)

    # every key field must exist in the queue, even when nothing matched:
    # an absent field stays absent for a human to resolve, never a guess.
    for name in KEY_FIELDS:
        if name not in found:
            fields.append({
                "name": name, "value": None,
                "confidence": 0.0, "key": True, "flags": ["not_extracted"],
                "evidence": None, "review": None, "status": "pending_review",
            })
    fields.sort(key=lambda f: (KEY_FIELDS.index(f["name"])
                               if f["name"] in KEY_FIELDS else len(KEY_FIELDS)))
    return fields


def _apply_line_sum_flag(fields: List[Dict[str, Any]]) -> None:
    by_name = {f["name"]: f for f in fields}
    amount = by_name.get("amount")
    items = by_name.get("line_items")
    if not amount or not items or not amount["value"] or not items["value"]:
        return
    try:
        total = Decimal(_normalize_amount(amount["value"]))
        item_sum = sum((Decimal(item["amount"]) for item in items["value"]),
                       Decimal("0"))
    except (InvalidOperation, ValueError):
        return
    if total != item_sum:
        amount["flags"] = sorted(set(amount["flags"]) | {"line_sum_mismatch"})


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def _candidate_dir(store: Path, candidate_id: str) -> Path:
    if not re.fullmatch(r"cand-\d{4}", candidate_id):
        raise CloseError("INPUT_REQUIRED", "unknown candidate id: %r" % candidate_id)
    return Path(store) / candidate_id


def _next_id(store: Path) -> str:
    store = Path(store)
    highest = 0
    if store.is_dir():
        for child in store.iterdir():
            match = re.fullmatch(r"cand-(\d{4})", child.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return "cand-%04d" % (highest + 1)


def _write_candidate(store: Path, candidate: Dict[str, Any]) -> None:
    path = _candidate_dir(store, candidate["candidate_id"])
    path.mkdir(parents=True, exist_ok=True)
    (path / "candidate.json").write_text(
        json.dumps(candidate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_candidate(store: Path, candidate_id: str) -> Dict[str, Any]:
    path = _candidate_dir(store, candidate_id) / "candidate.json"
    if not path.is_file():
        raise CloseError("INPUT_REQUIRED",
                         "candidate not found: %s (in %s)" % (candidate_id, store))
    candidate = json.loads(path.read_text(encoding="utf-8"))
    if candidate.get("schema") != CANDIDATE_SCHEMA:
        raise CloseError("STALE_SOURCE",
                         "%s is not a %s candidate" % (candidate_id, CANDIDATE_SCHEMA))
    return candidate


def list_candidates(store: Path) -> List[Dict[str, Any]]:
    store = Path(store)
    if not store.is_dir():
        return []
    out = []
    for child in sorted(store.iterdir()):
        if (child / "candidate.json").is_file():
            out.append(json.loads(
                (child / "candidate.json").read_text(encoding="utf-8")))
    return out


# ---------------------------------------------------------------------------
# procurement branch (§5)
# ---------------------------------------------------------------------------

def requires_procurement_payload(item: str, why: str, user_action: str,
                                 alternatives: List[str]) -> Dict[str, Any]:
    """The frozen §5 shape; never executes a transaction."""
    return {
        "terminal_state": "BLOCKED_USER_INPUT",
        "reason": "requires_procurement",
        "item": item,
        "why": why,
        "user_action": user_action,
        "alternatives": list(alternatives),
        "transaction_executed": False,
    }


def _paid_engine_payload() -> Dict[str, Any]:
    return requires_procurement_payload(
        item=("a paid cloud OCR engine is required by --engine paid-cloud-ocr "
              "and is not procured"),
        why=("the local chain only uses free local inputs: pasted text, a "
             "text file, or macOS Vision on a screenshot; Verovo never buys "
             "or subscribes on the user's behalf"),
        user_action=("procure the OCR API and provide its output yourself via "
                     "--ocr-json, or use the free paths: --text/--paste or "
                     "--image with the local Vision runner"),
        alternatives=["paste the bill text", "screenshot + local Vision OCR",
                      "precompute the OCR result and pass --ocr-json",
                      "narrow the period and type the key fields manually"])


# ---------------------------------------------------------------------------
# intake
# ---------------------------------------------------------------------------

def _normalized_lines(raw_lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(raw_lines, list) or not raw_lines:
        raise CloseError("INPUT_REQUIRED",
                         "OCR lines must be a non-empty array of {text, confidence, bbox}")
    clean = []
    for index, line in enumerate(raw_lines):
        if not isinstance(line, dict) or not _text(line.get("text")):
            raise CloseError("INPUT_REQUIRED",
                             "ocr lines[%d] must be an object with non-empty text" % index)
        confidence = line.get("confidence", 1.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            raise CloseError("INPUT_REQUIRED",
                             "ocr lines[%d] confidence must be a number" % index) from None
        clean.append({"text": _text(line["text"]),
                      "confidence": min(max(confidence, 0.0), 1.0),
                      "bbox": line.get("bbox")})
    return clean


def run_ocr_script(script: Path, image: Path) -> List[Dict[str, Any]]:
    """Run the local Vision OCR runner (candidate generator, never a booker).

    Vision is a macOS-only adapter. Injected ``verovo.ocr-lines/1`` fixtures
    remain portable, but a real OCR run on another platform is an explicit
    unsupported-scope refusal rather than an incidental ``swift`` failure.
    """
    if platform.system() != "Darwin":
        raise CloseError(
            "UNSUPPORTED_SCOPE",
            "local Vision OCR is unavailable on non-darwin (%s); use --text/"
            "--paste or inject an offline OCR result with --ocr-json"
            % platform.system())
    script = Path(script)
    if not script.is_file():
        raise CloseError(
            "INPUT_REQUIRED",
            "local Vision OCR runner not found: %s; paste the bill text "
            "(--text/--paste) or precompute the OCR output with --ocr-json" % script)
    try:
        proc = subprocess.run(["swift", str(script), str(image)],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CloseError("INPUT_REQUIRED",
                         "local Vision OCR failed to run: %s" % exc) from exc
    if proc.returncode != 0:
        raise CloseError("INPUT_REQUIRED",
                         "local Vision OCR exited %d: %s"
                         % (proc.returncode, proc.stderr.strip()[:200]))
    lines = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise CloseError("INPUT_REQUIRED",
                             "OCR runner emitted non-JSON output: %s" % exc) from exc
    return _normalized_lines(lines)


def add_candidate(store: Path, *, engine: str = "text", pasted_text: str = None,
                  text_path: Path = None, image_path: Path = None,
                  image_bytes: bytes = None, image_name: str = "source.png",
                  ocr_lines: List[Dict[str, Any]] = None,
                  ocr_script: Path = None) -> Dict[str, Any]:
    """Create an unreviewed candidate; never books, never calls a paid API.

    Returns ``{"candidate": <dict or None>, "procurement": <§5 payload or None>}``.
    """
    store = Path(store)
    if engine not in ENGINES:
        raise CloseError("UNSUPPORTED_SCOPE",
                         "engine must be one of %s (got %r)" % (list(ENGINES), engine))
    if engine == "paid-cloud-ocr":
        return {"candidate": None, "procurement": _paid_engine_payload()}

    source_bytes: bytes
    from_image = engine == "vision"
    lines_payload: Optional[List[Dict[str, Any]]] = None

    if from_image:
        if image_bytes is not None:
            source_bytes = image_bytes
        elif image_path is not None:
            image_path = Path(image_path)
            if not image_path.is_file():
                raise CloseError("INPUT_REQUIRED",
                                 "image not found: %s" % image_path)
            if image_path.suffix.lower() not in _IMAGE_SUFFIXES:
                raise CloseError("UNSUPPORTED_SCOPE",
                                 "image must be png/jpg/jpeg (got %s)" % image_path.suffix)
            source_bytes = image_path.read_bytes()
            image_name = image_path.name
        else:
            raise CloseError("INPUT_REQUIRED",
                             "vision engine needs --image (or injected bytes)")
        if ocr_lines is not None:
            lines_payload = _normalized_lines(ocr_lines)
        elif ocr_script is not None:
            temp_image = store.parent / ("ocr-input-" + image_name)
            temp_image.parent.mkdir(parents=True, exist_ok=True)
            temp_image.write_bytes(source_bytes)
            try:
                lines_payload = run_ocr_script(ocr_script, temp_image)
            finally:
                temp_image.unlink(missing_ok=True)
        else:
            raise CloseError(
                "INPUT_REQUIRED",
                "vision engine needs OCR output: pass --ocr-json, or make the "
                "local Vision runner available via --ocr-script")
    else:
        if pasted_text is not None:
            source_bytes = pasted_text.encode("utf-8")
        elif text_path is not None:
            text_path = Path(text_path)
            if not text_path.is_file():
                raise CloseError("INPUT_REQUIRED", "text file not found: %s" % text_path)
            source_bytes = text_path.read_bytes()
        else:
            raise CloseError("INPUT_REQUIRED",
                             "text engine needs --text FILE or --paste")
        try:
            text = source_bytes.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise CloseError("INPUT_REQUIRED",
                             "bill text is not UTF-8: %s" % exc) from exc
        if not text.strip():
            raise CloseError("INPUT_REQUIRED", "the pasted bill text is empty")
        lines_payload = [{"text": line, "confidence": 1.0, "bbox": None}
                         for line in text.splitlines() if line.strip()]

    candidate_id = _next_id(store)
    directory = _candidate_dir(store, candidate_id)

    garbled_reason = _looks_garbled(lines_payload)
    if garbled_reason:
        # nothing is extracted from an unusable OCR pass; the key fields stay
        # present-but-empty placeholders so the draft shows exactly what is
        # missing without inventing a single value.
        fields = [{
            "name": name, "value": None, "confidence": 0.0, "key": True,
            "flags": ["not_extracted"], "evidence": None, "review": None,
            "status": "pending_review",
        } for name in KEY_FIELDS]
        status = "blocked_user_input"
        blocked = {"reason": "bad_ocr", "detail": garbled_reason}
    else:
        fields = _extract_fields(lines_payload, from_image)
        _apply_line_sum_flag(fields)
        status = "unreviewed"
        blocked = None

    suffix = Path(image_name).suffix.lower() if from_image else ".txt"
    source_name = "source" + (suffix or ".bin")

    candidate = {
        "schema": CANDIDATE_SCHEMA,
        "candidate_id": candidate_id,
        "status": status,
        "engine": engine,
        "created_at": _now_iso(),
        "source": {
            "kind": "image" if from_image else "text",
            "file": source_name,
            "sha256": None,  # filled after write
            "raw_text": "ocr.txt" if from_image else source_name,
            "raw_text_sha256": None,  # filled after write
            "ocr_payload": "ocr.json" if from_image else None,
        },
        "fields": fields,
        "blocked": blocked,
        "duplicate_of": [],
        "booking": None,
        "boundary": ("candidates are drafts with field-level evidence; nothing "
                     "is booked and no verdict here authorizes payment"),
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / source_name).write_bytes(source_bytes)
    if from_image:
        (directory / "ocr.json").write_text(
            json.dumps({"schema": OCR_LINES_SCHEMA, "lines": lines_payload},
                       indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        # Preserve the exact normalized text used by field extraction as well
        # as the source image and OCR coordinates/confidence payload.
        (directory / "ocr.txt").write_text(
            "".join(_text(line["text"]) + "\n" for line in lines_payload),
            encoding="utf-8")
    candidate["source"]["sha256"] = _sha256_file(directory / source_name)
    candidate["source"]["raw_text_sha256"] = _sha256_file(
        directory / candidate["source"]["raw_text"])

    if status != "blocked_user_input":
        identity = _identity_of(candidate)
        duplicates = []
        for other in list_candidates(store):
            if other["candidate_id"] == candidate_id:
                continue
            if _same_invoice(other, identity):
                duplicates.append(other["candidate_id"])
        candidate["duplicate_of"] = duplicates

    _write_candidate(store, candidate)
    return {"candidate": candidate, "procurement": None}


def _field_value(candidate: Dict[str, Any], name: str) -> Any:
    for field in candidate.get("fields", []):
        if field["name"] == name:
            return field.get("value")
    return None


def _identity_of(candidate: Dict[str, Any]) -> Tuple[str, str]:
    vendor = _text(_field_value(candidate, "vendor")).casefold()
    invoice_no = _text(_field_value(candidate, "invoice_no")).casefold()
    return vendor, invoice_no


def _same_invoice(other: Dict[str, Any], identity: Tuple[str, str]) -> bool:
    other_vendor, other_no = _identity_of(other)
    vendor, invoice_no = identity
    return bool(vendor and invoice_no
                and other_vendor == vendor and other_no == invoice_no)


# ---------------------------------------------------------------------------
# review queue and field review
# ---------------------------------------------------------------------------

def review_queue(candidate: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Key fields, flagged fields and low-confidence fields queue for a human."""
    queue = []
    for field in candidate.get("fields", []):
        if field.get("review"):
            continue
        reasons = []
        if field.get("key"):
            reasons.append("key_field_review")
        if "not_extracted" in (field.get("flags") or []):
            reasons.append("not_extracted")
        elif field.get("flags"):
            reasons.extend(field["flags"])
        if field.get("confidence", 0.0) < REVIEW_THRESHOLD and not field.get("key"):
            reasons.append("low_confidence")
        if reasons:
            queue.append({
                "field": field["name"],
                "value": field.get("value"),
                "flags": field.get("flags") or [],
                "confidence": field.get("confidence"),
                "reasons": reasons,
                "evidence": field.get("evidence"),
            })
    return queue


_VALIDATORS = {
    "amount": _normalize_amount,
    "currency": _normalize_currency,
    "period": _normalize_period,
}


def review_field(store: Path, candidate_id: str, *, field: str, value: Any,
                 reviewer: str, note: str = "") -> Dict[str, Any]:
    """Record a human field review; may correct the OCR value, never hides it."""
    store = Path(store)
    if not _text(reviewer):
        raise CloseError("INPUT_REQUIRED",
                         "field review needs a named reviewer; no signature is inferred")
    candidate = load_candidate(store, candidate_id)
    if candidate["status"] == "booked":
        raise CloseError("REVIEW_REQUIRED",
                         "%s is already booked; supersede it with a new candidate "
                         "and a close revision, not an in-place edit" % candidate_id)
    if candidate["status"] == "blocked_user_input":
        raise CloseError("INPUT_REQUIRED",
                         "%s is blocked (%s); fix the source before reviewing fields"
                         % (candidate_id, (candidate.get("blocked") or {}).get("reason")))
    target = None
    for entry in candidate["fields"]:
        if entry["name"] == field:
            target = entry
            break
    if target is None:
        raise CloseError("INPUT_REQUIRED",
                         "unknown field %r; the candidate carries: %s"
                         % (field, ", ".join(f["name"] for f in candidate["fields"])))
    raw = value if isinstance(value, (dict, list)) else _text(value)
    if field in _VALIDATORS:
        try:
            normalized = _VALIDATORS[field](raw)
        except ValueError as exc:
            raise CloseError("INPUT_REQUIRED",
                             "field %s: %s" % (field, exc)) from exc
    else:
        normalized = raw
    if field in ("invoice_no", "amount", "currency") and not _text(normalized):
        raise CloseError("INPUT_REQUIRED",
                         "field %s cannot be confirmed empty; supply the value "
                         "from the bill or leave it pending" % field)
    original = target.get("value")
    target["review"] = {
        "reviewer": _text(reviewer),
        "value": normalized,
        "original_value": original,
        "note": _text(note),
        "at": _now_iso(),
        "attestation": "attested_by_declaration",
    }
    target["value"] = normalized
    target["status"] = "confirmed"
    target["flags"] = []
    _write_candidate(store, candidate)
    return candidate


# ---------------------------------------------------------------------------
# booking into the existing close chain
# ---------------------------------------------------------------------------

def _sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", _text(value)).strip("-")
    return cleaned[:60] or "invoice"


def book(store: Path, candidate_id: str, *, reviewer: str, into: Path,
         account: str, job_id: str = "", buyer: str = "",
         timezone_: str = "UTC", usage: str = None,
         rule_map: str = None) -> Dict[str, Any]:
    """Export the reviewed invoice as a bills source + job for close init.

    This is the only bridge from an OCR candidate into the ledger, it is a
    human act (``--by``), and it refuses to run while any field review is
    pending or the invoice_no is already booked.
    """
    store = Path(store)
    if not _text(reviewer):
        raise CloseError("INPUT_REQUIRED",
                         "booking needs a named reviewer; an OCR draft never "
                         "books itself")
    candidate = load_candidate(store, candidate_id)
    if candidate["status"] == "booked":
        raise CloseError("REVIEW_REQUIRED",
                         "%s is already booked at %s; bring changes in as a new "
                         "candidate and a close revision"
                         % (candidate_id, (candidate.get("booking") or {}).get("at")))
    if candidate["status"] == "blocked_user_input":
        raise CloseError("INPUT_REQUIRED",
                         "%s is blocked (%s): %s"
                         % (candidate_id,
                            (candidate.get("blocked") or {}).get("reason"),
                            (candidate.get("blocked") or {}).get("detail")))
    queue = review_queue(candidate)
    if queue:
        raise CloseError(
            "REVIEW_REQUIRED",
            "%d field(s) still pending review (%s); an unreviewed draft never "
            "enters canonical" % (len(queue), ", ".join(i["field"] for i in queue)))
    if not _text(account):
        raise CloseError("INPUT_REQUIRED",
                         "booking needs --account; the job account is never guessed")

    identity = _identity_of(candidate)
    for other in list_candidates(store):
        if other["candidate_id"] == candidate_id:
            continue
        if other.get("status") == "booked" and _same_invoice(other, identity):
            raise CloseError(
                "REVIEW_REQUIRED",
                "invoice %s of %s is already booked as %s; a duplicate invoice "
                "never double-books — dispute it through the close report"
                % (identity[1], identity[0], other["candidate_id"]))

    vendor = _text(_field_value(candidate, "vendor"))
    invoice_no = _text(_field_value(candidate, "invoice_no"))
    currency = _text(_field_value(candidate, "currency"))
    amount = _text(_field_value(candidate, "amount"))
    period = _field_value(candidate, "period")
    for name, value in (("vendor", vendor), ("invoice_no", invoice_no),
                        ("currency", currency), ("amount", amount)):
        if not value:
            raise CloseError("INPUT_REQUIRED",
                             "field %s is reviewed but empty; booking needs a value "
                             "from the bill, not a guess" % name)
    if not isinstance(period, dict) or not period.get("from") or not period.get("to"):
        raise CloseError("INPUT_REQUIRED",
                         "field period must be reviewed to a half-open range before "
                         "booking")

    bundle = Path(into)
    bundle.mkdir(parents=True, exist_ok=True)
    job_path = bundle / "period-job.json"
    if job_path.exists():
        raise CloseError("REVIEW_REQUIRED",
                         "%s already exists; a booking never overwrites an operator "
                         "job — use a fresh bundle or a close revision" % job_path)

    items = _field_value(candidate, "line_items") or []
    if items:
        try:
            line_items_sum = sum((Decimal(item["amount"]) for item in items),
                                 Decimal("0"))
        except (InvalidOperation, KeyError, TypeError) as exc:
            raise CloseError(
                "REVIEW_REQUIRED",
                "reviewed line items are malformed; correct them before booking"
            ) from exc
        if line_items_sum != Decimal(amount):
            raise CloseError(
                "REVIEW_REQUIRED",
                "reviewed line items sum to %s but the reviewed invoice total is "
                "%s; correct and re-confirm amount/line_items instead of booking "
                "an OCR mismatch" % (line_items_sum, amount))
    safe_no = _sanitize_name(invoice_no)
    export_name = "%s-invoice.csv" % safe_no
    doc_name = "%s%s" % (safe_no, Path(candidate["source"]["file"]).suffix or ".txt")

    fieldnames = ["Charge Reference", "Total Amount", "Currency", "Quantity",
                  "Unit Price", "Description", "source_party", "collector_party",
                  "control_domain", "collection_method", "confirmation_process",
                  "source_path", "evidence_ref"]
    source_ref = "bill-candidates/%s/%s" % (candidate_id, candidate["source"]["file"])

    def evidence_ref(field_name: str, item_index: int = 0) -> str:
        evidence = None
        for field in candidate["fields"]:
            if field["name"] == field_name:
                if field_name == "line_items" and items:
                    evidence = items[item_index].get("evidence")
                else:
                    evidence = field.get("evidence")
        if not evidence:
            return "bill-candidate:%s:field:%s" % (candidate_id, field_name)
        return "bill-candidate:%s:field:%s:line:%s" % (
            candidate_id, field_name, evidence.get("line", "?"))

    rows = []
    if items:
        for index, item in enumerate(items, start=1):
            rows.append({
                "Charge Reference": "%s-%d" % (invoice_no, index),
                "Total Amount": item["amount"],
                "Currency": currency,
                "Quantity": item["quantity"],
                "Unit Price": item["unit_price"],
                "Description": item["description"],
                "source_party": "vendor", "collector_party": "buyer",
                "control_domain": "vendor",
                "collection_method": "bill_screenshot_or_text_ocr",
                "confirmation_process": "field_review_attested",
                "source_path": source_ref,
                "evidence_ref": evidence_ref("line_items", index - 1),
            })
    else:
        rows.append({
            "Charge Reference": invoice_no,
            "Total Amount": amount,
            "Currency": currency,
            "Quantity": "", "Unit Price": "", "Description": "",
            "source_party": "vendor", "collector_party": "buyer",
            "control_domain": "vendor",
            "collection_method": "bill_screenshot_or_text_ocr",
            "confirmation_process": "field_review_attested",
            "source_path": source_ref,
            "evidence_ref": evidence_ref("amount"),
        })
    export_path = bundle / export_name
    with export_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    source_file = _candidate_dir(store, candidate_id) / candidate["source"]["file"]
    doc_path = bundle / doc_name
    shutil.copyfile(source_file, doc_path)

    sources = [
        {"role": "bills", "path": export_name, "control_domain": "vendor"},
        {"role": "invoice", "path": doc_name, "control_domain": "vendor"},
    ]
    if usage:
        if not (bundle / usage).is_file():
            raise CloseError("INPUT_REQUIRED",
                             "--usage %s not found inside %s" % (usage, bundle))
        sources.append({"role": "usage", "path": usage,
                        "control_domain": "vendor"})
    month = period["from"][:7]
    job = {
        "schema": "verovo.period-job/1",
        "job_id": _text(job_id) or "close-%s-%s" % (month, safe_no),
        "buyer": _text(buyer),
        "vendor": vendor,
        "account": _text(account),
        "period": {"from": period["from"], "to": period["to"],
                   "to_exclusive": True, "timezone": timezone_ or "UTC"},
        "currency": currency,
        "sources": sources,
        "rule_map": rule_map,
        "task_economics_requested": False,
        "approved_by": None,
    }
    if rule_map and not (bundle / rule_map).is_file():
        raise CloseError("INPUT_REQUIRED",
                         "--rule-map %s not found inside %s" % (rule_map, bundle))
    _validate_job(job)  # the export must pass close init's own contract
    job_path.write_text(json.dumps(job, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")

    booking = {
        "booked_by": _text(reviewer),
        "booked_at": _now_iso(),
        "attestation": "attested_by_declaration",
        "bundle": str(bundle),
        "export_path": str(export_path),
        "export_sha256": _sha256_file(export_path),
        "document_path": str(doc_path),
        "document_sha256": _sha256_file(doc_path),
        "job_path": str(job_path),
        "invoice_total": amount,
        "line_items_sum": (str(sum((Decimal(i["amount"]) for i in items),
                                   Decimal("0"))) if items else None),
        "payment_authorization": False,
    }
    candidate["status"] = "booked"
    candidate["booking"] = booking
    _write_candidate(store, candidate)
    return booking


# ---------------------------------------------------------------------------
# projection: review queue + §3.1 terminal state
# ---------------------------------------------------------------------------

def projection(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only projection with the review queue and a delivery terminal state."""
    queue = review_queue(candidate)
    out = dict(candidate)
    out["review_queue"] = queue
    if candidate["status"] == "booked":
        booking = candidate.get("booking") or {}
        terminal = {
            "terminal_state": "DELIVERED",
            "reason": "booked_into_close_chain",
            "receipt": {
                "export_path": booking.get("export_path"),
                "export_sha256": booking.get("export_sha256"),
                "job_path": booking.get("job_path"),
                "document_sha256": booking.get("document_sha256"),
                "booked_by": booking.get("booked_by"),
                "booked_at": booking.get("booked_at"),
            },
            "user_action": None,
            "note": ("booked means the reviewed invoice entered the close chain; "
                     "signoff there still does not authorize payment"),
        }
    elif candidate["status"] == "blocked_user_input":
        terminal = {
            "terminal_state": "BLOCKED_USER_INPUT",
            "reason": "bad_ocr",
            "user_action": ("retake the screenshot at a higher resolution or "
                            "paste the bill text; the garbled draft is preserved "
                            "and no field was guessed"),
            "alternatives": ["paste the bill text", "precompute --ocr-json",
                             "type the key fields during review"],
        }
    else:
        terminal = {
            "terminal_state": "BLOCKED_USER_INPUT",
            "reason": "review_pending",
            "user_action": ("review the queued fields (bill-candidate review ... "
                            "--field <name> --value <value> --by <reviewer>), then "
                            "book explicitly with bill-candidate book --by"),
            "queue_size": len(queue),
        }
    out["terminal"] = terminal
    return out
