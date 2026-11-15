"""V-02 close pipeline: import one real billing period with a human-confirmed mapping.

The ``close`` flow is an orchestrator over files, never a second financial
engine (PRD 03 §6):

    verovo.period-job/1  ->  file scan  ->  csv/jsonl parse
        ->  column-recognition proposal  ->  account/period/currency/unit and
            duplicate-id checks  ->  mapping-draft.json
        ->  HUMAN confirmation (rules, control domains, column ownership)
        ->  canonical/ bundle in the format the existing Period Intake
            (healthcheck ``period_intake`` / ``canonical_finance``) consumes.

Discipline (AC V07-V15, V30, V34):

- original files are never modified; re-ingest is idempotent and keeps the
  original SHA-256 on record even when the file later changes (STALE_SOURCE);
- nothing enters canonical before a human confirms the mapping draft; the
  reviewer is recorded as ``attested_by_declaration`` (PRD §12.3), never as
  strong identity, and ``approved_by`` in the job JSON authorizes nothing;
- mixed currencies never merge into one case (split first); duplicate
  charge/usage ids are flagged and only the first row can be booked;
- OCR/invoice2data documents stay unreviewed drafts and never reach ledger
  rows; a price table whose validity window does not cover the period
  refuses recalculation instead of guessing;
- vendor exports keep the vendor control domain even when the buyer downloaded
  them; two vendor exports never qualify as Buyer Verified;
- with a single invoice the case still returns a partial source check and
  ``Invoice Reconciled`` stays UNPROVABLE; the missing-input list names gaps,
  never amounts;
- errors use the frozen C-01 code registry; ``calculate``/``report`` and
  ``revise``/``signoff`` are V-03/V-04 stubs that fail loudly.

No network access, no provider calls, no booking of anything.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PERIOD_JOB_SCHEMA = "verovo.period-job/1"
MAPPING_DRAFT_SCHEMA = "verovo.close-mapping/1"
MAPPING_CONFIRMED_SCHEMA = "verovo.close-mapping-confirmed/1"
SCAN_SCHEMA = "verovo.close-scan/1"
BUYER_CLOSE_CASE_SCHEMA = "verovo.buyer-close-case/1"

# C-01 §3 frozen error-code registry. Correct refusal is a first-class result.
ERROR_CODES = (
    "INPUT_REQUIRED", "AUTH_REQUIRED", "REVIEW_REQUIRED", "UNSUPPORTED_SCOPE",
    "POLICY_DENIED", "RATE_LIMITED", "STALE_SOURCE",
    "INDETERMINATE_EXTERNAL_ACTION", "ARTIFACT_PARTIAL", "INTERNAL_ERROR",
)

CONTROL_DOMAINS = ("vendor", "buyer", "buyer_or_vendor_pending_review")
RESOLVED_CONTROL_DOMAINS = ("vendor", "buyer")
INVOICE_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".txt"}
TABLE_SUFFIXES = {".csv", ".tsv", ".jsonl"}
MAX_SOURCE_BYTES = 20 * 1024 * 1024

# Commands that exist only as loud stubs; V-03 owns calculate/report, V-04
# owns revisions and signoff.
LATER_TICKET_COMMANDS = {"revise": "V-04", "signoff": "V-04"}

# role -> canonical Period Intake description. ``required`` columns are the
# minimum a reviewer must see mapped (or explicitly approve as a gap); the
# pipeline never invents a value for them.
ROLES = {
    "invoice": {"target": "invoices", "format": None, "required": (),
                "optional": (), "document": True},
    "bills": {"target": "bills", "format": "bills-csv",
              "required": ("charge_id", "amount", "currency"),
              "optional": ("quantity", "unit_price", "rate_rule", "task_id",
                           "allocation_rule", "tax", "description", "date",
                           "account_ref", "vendor", "adjustment_type",
                           "adjustment_state"), "document": False},
    "usage": {"target": "usage", "format": "usage-jsonl", "required": ("usage_id",),
              "optional": ("run_id", "charge_id", "cost", "currency", "quantity",
                           "unit", "rate_rule", "task_id", "financialized",
                           "date", "source_ref", "account_ref", "vendor"),
              "document": False},
    "tasks": {"target": "tasks", "format": "tasks-csv", "required": ("task_id",),
              "optional": ("type",), "document": False},
    "runs": {"target": "runs", "format": "runs-jsonl", "required": ("run_id",),
             "optional": ("task_id",), "document": False},
    "outcomes": {"target": "outcomes", "format": "outcomes-csv",
                 "required": ("task_id",),
                 "optional": ("outcome_id", "status", "verified", "value",
                              "currency", "accepted_by", "accepted_at",
                              "source_ref"), "document": False},
}
LEDGER_CHECK_ROLES = ("bills", "usage", "runs")

# Evidence-provenance fields the vendor export may already carry row-by-row;
# they pass through unchanged. The human-confirmed control domain fills the
# manifest-level provenance otherwise.
EVIDENCE_FIELDS = ("source_party", "source_affiliation", "control_domain",
                   "collector_party", "collection_method", "independence",
                   "confirmation_process", "source_path")

DOMAIN_EVIDENCE = {
    "vendor": {
        "source_party": "vendor", "source_affiliation": "vendor",
        "control_domain": "vendor", "collector_party": "vendor",
        "collection_method": "vendor_export",
        "independence": "same_vendor_control_domain",
        "confirmation_process": "vendor_export",
    },
    "buyer": {
        "source_party": "buyer", "source_affiliation": "buyer",
        "control_domain": "buyer", "collector_party": "buyer",
        "collection_method": "buyer_local_export",
        # Procedural qualification of a human-confirmed buyer control domain;
        # still an attested declaration, not cryptographic identity proof.
        "independence": "buyer_independent",
        "confirmation_process": "mapping_review_attested",
    },
}

# Column-recognition vocabulary. Normalized names: casefolded, BOM-stripped,
# underscores/hyphens collapsed to single spaces. The first matching column
# wins per role field order; conflicts are surfaced for review, never guessed.
_FIELD_SYNONYMS = {
    "charge_id": ("charge id", "charge reference", "charge no", "charge",
                  "reference", "ref", "line id", "invoice line id", "fee id",
                  "id", "entry id"),
    "amount": ("total amount", "amount", "total", "cost", "fee",
               "charge amount", "net amount", "gross amount", "billed amount",
               "amount due"),
    "currency": ("currency", "ccy", "curr", "currency code", "币种"),
    "quantity": ("quantity", "qty", "tokens", "units", "count", "usage", "volume"),
    "unit_price": ("unit price", "price", "rate", "unit rate", "unit cost"),
    "rate_rule": ("rate plan", "rate rule", "pricing rule", "price rule",
                  "rule id", "rule", "plan"),
    "task_id": ("task id", "task", "job id", "job"),
    "allocation_rule": ("alloc rule", "allocation rule", "allocation"),
    "usage_id": ("record id", "usage id", "usage record id", "event id", "record"),
    "run_id": ("run id", "run", "trace id"),
    "cost": ("total cost", "cost", "amount", "total", "spend"),
    "unit": ("billable unit", "unit type", "unit", "units", "metric"),
    "financialized": ("financialized", "financial", "billable"),
    "date": ("service date", "usage date", "charged at", "occurred at",
             "created at", "timestamp", "date", "day"),
    "type": ("task type", "type"),
    "status": ("status", "outcome", "result"),
    "verified": ("buyer verified", "verified"),
    "value": ("business value", "value"),
    "accepted_by": ("accepted by",),
    "accepted_at": ("accepted at",),
    "account_ref": ("account ref", "account id", "account", "acct"),
    "vendor": ("provider", "supplier", "vendor"),
    "source_ref": ("source ref",),
    "tax": ("tax amount", "tax", "vat"),
    "adjustment_type": ("adjustment type", "adjustment kind", "adjustment",
                        "credit type", "refund type"),
    "adjustment_state": ("adjustment state", "adjustment status", "credit state",
                         "application state", "applied state"),
    "description": ("description", "item", "detail", "service", "memo"),
    "outcome_id": ("outcome id",),
}
_SYNONYM_LOOKUP: Dict[str, str] = {}
for _canonical, _synonyms in _FIELD_SYNONYMS.items():
    for _synonym in _synonyms:
        _SYNONYM_LOOKUP.setdefault(_synonym, _canonical)


class CloseError(ValueError):
    """Refusal with a frozen C-01 machine code; never retried by the caller."""

    def __init__(self, code: str, message: str):
        if code not in ERROR_CODES:
            raise ValueError("unregistered close error code: %r" % code)
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _norm(value: Any) -> str:
    text = _text(value).lstrip("\ufeff").casefold()
    text = re.sub(r"[_\-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _iso_date(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(_text(value)[:10])
    except ValueError as exc:
        raise CloseError("INPUT_REQUIRED",
                         "%s must be an ISO date: %r" % (label, value)) from exc


# ---------------------------------------------------------------------------
# verovo.period-job/1 contract
# ---------------------------------------------------------------------------

def _validate_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an in-memory period job against the frozen contract."""
    if job.get("schema") != PERIOD_JOB_SCHEMA:
        raise CloseError("UNSUPPORTED_SCOPE",
                         "period job schema must be %s (got %r)"
                         % (PERIOD_JOB_SCHEMA, job.get("schema")))
    for field in ("job_id", "vendor", "account"):
        if not _text(job.get(field)):
            raise CloseError("INPUT_REQUIRED", "period job needs a non-empty %s" % field)
    currency = _text(job.get("currency")).upper()
    if not re.fullmatch(r"[A-Za-z]{3}", currency):
        raise CloseError("INPUT_REQUIRED",
                         "period job currency must be a three-letter code (got %r)"
                         % job.get("currency"))
    job["currency"] = currency
    period = job.get("period")
    if not isinstance(period, dict):
        raise CloseError("INPUT_REQUIRED", "period job needs a period object")
    if period.get("to_exclusive") is not True:
        raise CloseError(
            "UNSUPPORTED_SCOPE",
            "period must use the half-open convention [from, to); set to_exclusive=true")
    start = _iso_date(period.get("from"), "period.from")
    end = _iso_date(period.get("to"), "period.to")
    if start >= end:
        raise CloseError("INPUT_REQUIRED", "period.from must be earlier than period.to")
    tz = _text(period.get("timezone")) or "UTC"
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(tz)
    except Exception as exc:  # unknown zone or no tz database available
        raise CloseError("INPUT_REQUIRED",
                         "period timezone is not a valid IANA zone: %r" % tz) from exc
    period["timezone"] = tz
    sources = job.get("sources")
    if not isinstance(sources, list):
        raise CloseError("INPUT_REQUIRED", "period job sources must be an array")
    seen_paths = set()
    for index, entry in enumerate(sources):
        if not isinstance(entry, dict):
            raise CloseError("INPUT_REQUIRED", "sources[%d] must be an object" % index)
        role = _text(entry.get("role"))
        if role not in ROLES:
            raise CloseError("UNSUPPORTED_SCOPE",
                             "sources[%d] role %r is outside the supported "
                             "single-period slice %s" % (index, role, sorted(ROLES)))
        source_path = _text(entry.get("path"))
        if not source_path:
            raise CloseError("INPUT_REQUIRED", "sources[%d] needs a path" % index)
        # Path containment is enforced at ingest against the resolved bundle
        # (``_resolve_inside``), where symlinks and ``..`` are both observable.
        # Validating here would reject a job before the bundle is known and
        # would not catch a symlink created inside the bundle.
        normalized = Path(source_path).as_posix()
        if normalized in seen_paths:
            raise CloseError("INPUT_REQUIRED", "source path declared twice: %s" % normalized)
        seen_paths.add(normalized)
        entry["path"] = normalized
        domain = _text(entry.get("control_domain"))
        if domain not in CONTROL_DOMAINS:
            raise CloseError("UNSUPPORTED_SCOPE",
                             "sources[%d] control_domain must be one of %s (got %r)"
                             % (index, list(CONTROL_DOMAINS), domain))
        entry["control_domain"] = domain
        declared = _text(entry.get("sha256"))
        if declared and not re.fullmatch(r"[0-9a-fA-F]{64}", declared):
            raise CloseError("INPUT_REQUIRED",
                             "sources[%d] sha256 must be 64 hex characters" % index)
    rule_map = job.get("rule_map")
    if rule_map is not None:
        if not isinstance(rule_map, str) or not _text(rule_map) \
                or Path(_text(rule_map)).is_absolute():
            raise CloseError("INPUT_REQUIRED",
                             "rule_map must be a relative path inside the bundle")
        job["rule_map"] = Path(_text(rule_map)).as_posix()
    job["task_economics_requested"] = job.get("task_economics_requested") is True
    approved_by = job.get("approved_by")
    if approved_by is not None and not isinstance(approved_by, str):
        raise CloseError("INPUT_REQUIRED", "approved_by must be null or a string")
    # approved_by is a recorded declaration only; it never authorizes anything
    # inside this pipeline (C-01 red line).
    return job


def load_period_job(path: Path) -> Dict[str, Any]:
    """Load and validate a verovo.period-job/1 file."""
    path = Path(path)
    if not path.is_file():
        raise CloseError("INPUT_REQUIRED", "period job not found: %s" % path)
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CloseError("INPUT_REQUIRED", "period job is not valid JSON: %s" % exc) from exc
    if not isinstance(job, dict):
        raise CloseError("INPUT_REQUIRED", "period job must be a JSON object")
    return _validate_job(job)


def init_case(case_dir: Path, *, job_path: Path = None, job_id: str = "",
              buyer: str = "", vendor: str = "", account: str = "",
              currency: str = "", period: str = "", timezone_: str = "UTC",
              task_economics_requested: bool = False,
              rule_map: str = None) -> Dict[str, Any]:
    """Create a close case directory with a verbatim or scaffolded period job."""
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    target = case_dir / "period-job.json"
    if target.exists():
        raise CloseError("REVIEW_REQUIRED",
                         "%s already exists; revisions arrive in V-04 and never "
                         "silently overwrite the job" % target.name)
    if job_path is not None:
        job = load_period_job(Path(job_path))
        raw = Path(job_path).read_bytes()
    else:
        if not (job_id and vendor and account and currency and period):
            raise CloseError("INPUT_REQUIRED",
                             "close init needs --from-job, or job-id/vendor/account/"
                             "currency/period together")
        if not re.fullmatch(r"\d{4}-\d{2}", period):
            raise CloseError("INPUT_REQUIRED", "period must be YYYY-MM (got %r)" % period)
        start = date.fromisoformat(period + "-01")
        next_month = datetime(start.year + (start.month // 12), (start.month % 12) + 1, 1)
        job = {
            "schema": PERIOD_JOB_SCHEMA,
            "job_id": job_id, "buyer": buyer, "vendor": vendor, "account": account,
            "period": {"from": start.isoformat(), "to": next_month.date().isoformat(),
                       "to_exclusive": True, "timezone": timezone_},
            "currency": currency.upper(),
            "sources": [],
            "rule_map": rule_map,
            "task_economics_requested": task_economics_requested,
            "approved_by": None,
        }
        _validate_job(job)
        raw = (json.dumps(job, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    target.write_bytes(raw)
    return {"case_dir": str(case_dir), "period_job": job, "state": "created",
            "next": "close ingest %s --bundle <dir>" % case_dir}


# ---------------------------------------------------------------------------
# file scan and parsing
# ---------------------------------------------------------------------------

def _resolve_inside(bundle: Path, relative: str, what: str) -> Path:
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise CloseError("POLICY_DENIED", "%s escapes the bundle: %s" % (what, relative))
    resolved = (bundle / relative).resolve()
    try:
        resolved.relative_to(bundle.resolve())
    except ValueError as exc:
        raise CloseError("POLICY_DENIED", "%s escapes the bundle: %s" % (what, relative)) from exc
    return resolved


def _read_source(path: Path) -> str:
    raw = path.read_bytes()
    if not raw.strip():
        raise CloseError("INPUT_REQUIRED", "%s is an empty file" % path.name)
    if len(raw) > MAX_SOURCE_BYTES:
        raise CloseError("INPUT_REQUIRED",
                         "%s exceeds the %d-byte import limit" % (path.name, MAX_SOURCE_BYTES))
    try:
        return raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise CloseError("INPUT_REQUIRED",
                         "%s is not UTF-8 CSV/JSONL (convert the encoding first): %s"
                         % (path.name, exc)) from exc


def _split_csv(text: str, name: str) -> Tuple[List[str], List[Dict[str, str]]]:
    delimiter = "\t" if name.lower().endswith(".tsv") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        raise CloseError("INPUT_REQUIRED", "%s has no header row" % name)
    header = [str(cell or "").strip() for cell in rows[0]]
    if not any(header):
        raise CloseError("INPUT_REQUIRED", "%s has an empty header" % name)
    width = len(header)
    out: List[Dict[str, str]] = []
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(str(cell or "").strip() for cell in row):
            continue
        if len(row) != width:
            raise CloseError("INPUT_REQUIRED",
                             "%s line %d is a corrupt row: %d cells, the header has %d"
                             % (name, line_no, len(row), width))
        out.append({header[i]: str(row[i]).strip() for i in range(width)})
    return header, out


def _split_jsonl(text: str, name: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    keys: List[str] = []
    seen: set = set()
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CloseError("INPUT_REQUIRED",
                             "%s line %d is not JSON: %s" % (name, line_no, exc)) from exc
        if not isinstance(item, dict):
            raise CloseError("INPUT_REQUIRED",
                             "%s line %d must be a JSON object" % (name, line_no))
        rows.append(item)
        for key in item:
            if key not in seen:
                seen.add(key)
                keys.append(str(key))
    return keys, rows


def _parse_table(path: Path,
                 allow_empty: bool = False) -> Tuple[List[str], List[Dict[str, Any]], str]:
    suffix = path.suffix.lower()
    if allow_empty and not path.read_bytes().strip():
        # An outcomes source may be empty: it means "no outcomes rows", which
        # is a partial axis, not a malformed ledger file.  Keep a contract
        # header so the canonical bundle stays readable.
        columns = list(ROLES["outcomes"]["required"]) + list(
            ROLES["outcomes"]["optional"])
        return columns, [], "csv"
    text = _read_source(path)
    if suffix == ".jsonl":
        columns, rows = _split_jsonl(text, path.name)
        return columns, rows, "jsonl"
    columns, rows = _split_csv(text, path.name)
    return columns, rows, "csv"


def _propose_columns(role: str, columns: List[str]) -> Dict[str, Any]:
    """Deterministic column-recognition proposal; conflicts stay visible."""
    spec = ROLES[role]
    order = list(spec["required"]) + [field for field in spec["optional"]]
    mapping: Dict[str, str] = {}
    conflicts: List[Dict[str, str]] = []
    for column in columns:
        canonical = _SYNONYM_LOOKUP.get(_norm(column))
        if canonical is None or canonical not in order:
            continue
        if canonical in mapping:
            conflicts.append({"field": canonical, "column": column,
                              "kept": mapping[canonical]})
            continue
        mapping[canonical] = column
    return {"mapping": mapping, "conflicts": conflicts,
            "required_mapped": [f for f in spec["required"] if f in mapping],
            "pending_fields": [f for f in spec["required"] if f not in mapping]}


def _cell(row: Dict[str, Any], column: Optional[str]) -> str:
    if not column:
        return ""
    value = row.get(column)
    if isinstance(value, bool):
        return "true" if value else "false"
    return _text(value)


def _parse_date_loose(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(_text(value)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# rule map
# ---------------------------------------------------------------------------

def _load_rule_map(bundle: Path, job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    relative = job.get("rule_map")
    if not relative:
        return None
    path = _resolve_inside(bundle, relative, "rule_map")
    if not path.is_file():
        raise CloseError("INPUT_REQUIRED", "rule_map file not found: %s" % relative)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CloseError("INPUT_REQUIRED", "rule_map is not valid JSON: %s" % exc) from exc
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise CloseError("INPUT_REQUIRED", "rule_map needs a rules array")
    return data


def _check_rule_map(job: Dict[str, Any], rule_map: Optional[Dict[str, Any]],
                    ledger_roles: List[str]) -> Dict[str, Any]:
    """Currency/validity/version checks; an expired price table refuses repricing."""
    violations: List[Dict[str, Any]] = []
    open_items: List[Dict[str, Any]] = []
    pricing: List[Dict[str, Any]] = []
    allocation_ids: set = set()
    intake_formats: Dict[str, str] = {}
    absent = rule_map is None
    if not absent:
        start = _iso_date(job["period"]["from"], "period.from")
        end = _iso_date(job["period"]["to"], "period.to") - timedelta(days=1)
        for index, rule in enumerate(rule_map["rules"]):
            if not isinstance(rule, dict):
                raise CloseError("INPUT_REQUIRED",
                                 "rule_map rules[%d] must be an object" % index)
            rule_id = _text(rule.get("rule_id"))
            kind = _text(rule.get("kind"))
            if kind == "intake":
                fmt = _text(rule.get("format"))
                if fmt:
                    intake_formats.setdefault(fmt, rule_id)
            elif kind == "pricing":
                pricing.append(rule)
                rule_currency = _text(rule.get("currency")).upper()
                if rule_currency and rule_currency != job["currency"]:
                    violations.append({
                        "kind": "pricing_currency", "rule_id": rule_id,
                        "detail": "pricing rule %s is %s while the period is %s"
                                  % (rule_id, rule_currency, job["currency"])})
                valid_from = _text(rule.get("valid_from"))
                valid_to = _text(rule.get("valid_to"))
                if valid_from or valid_to:
                    window_start = _iso_date(valid_from or "0001-01-01",
                                             "%s.valid_from" % rule_id)
                    window_end = _iso_date(valid_to or "9999-12-31",
                                           "%s.valid_to" % rule_id)
                    if not (window_start <= start and window_end >= end):
                        violations.append({
                            "kind": "pricing_validity", "rule_id": rule_id,
                            "valid_from": valid_from or None,
                            "valid_to": valid_to or None,
                            "detail": ("pricing rule %s validity [%s, %s] does not "
                                       "cover the half-open period [%s, %s); "
                                       "recalculation is refused"
                                       % (rule_id, valid_from or "?", valid_to or "?",
                                          job["period"]["from"], job["period"]["to"]))})
                else:
                    open_items.append({
                        "kind": "rule_validity_unstated", "rule_id": rule_id,
                        "detail": "pricing rule %s states no validity window; it must "
                                  "be re-reviewed before recalculation counts" % rule_id})
            elif kind == "allocation":
                if rule_id:
                    allocation_ids.add(rule_id)
    for role in ledger_roles:
        fmt = ROLES[role]["format"]
        if fmt and fmt not in intake_formats:
            violations.append({
                "kind": "missing_intake_rule", "rule_id": None,
                "detail": "rule map has no approved intake rule for format %s (role %s)"
                          % (fmt, role)})
    return {"violations": violations, "open_items": open_items, "pricing": pricing,
            "allocation_ids": sorted(allocation_ids),
            "intake_formats": intake_formats, "absent": absent}


def _blocked_code(violations: List[Dict[str, Any]]) -> Optional[str]:
    for violation in violations:
        if violation.get("kind") in ("currency_mix", "pricing_currency",
                                     "pricing_validity", "account_mismatch"):
            return "UNSUPPORTED_SCOPE"
    for violation in violations:
        if violation.get("kind") == "missing_intake_rule":
            return "INPUT_REQUIRED"
    return None


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

def ingest(case_dir: Path, bundle: Path = None) -> Dict[str, Any]:
    """Scan, parse, propose the mapping and validate; writes only on full success."""
    case_dir = Path(case_dir)
    job = load_period_job(case_dir / "period-job.json")
    bundle = Path(bundle) if bundle is not None else case_dir
    if not bundle.is_dir():
        raise CloseError("INPUT_REQUIRED", "bundle directory not found: %s" % bundle)
    bundle_root = bundle.resolve()
    intake_dir = case_dir / "intake"
    scan_path = intake_dir / "scan.json"
    draft_path = intake_dir / "mapping-draft.json"
    previous_scan = None
    if scan_path.is_file():
        try:
            previous_scan = json.loads(scan_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous_scan = None

    registry: Dict[str, List[Dict[str, Any]]] = {}
    source_hashes: Dict[str, str] = {}
    tables: Dict[str, List[Dict[str, Any]]] = {}
    documents: List[Dict[str, Any]] = []
    open_items: List[Dict[str, Any]] = []

    for entry in job["sources"]:
        role = entry["role"]
        relative = Path(entry["path"]).as_posix()
        path = _resolve_inside(bundle_root, relative, "source %s" % role)
        if not path.is_file():
            raise CloseError("INPUT_REQUIRED", "source file not found: %s" % relative)
        raw = path.read_bytes()
        if len(raw) > MAX_SOURCE_BYTES:
            raise CloseError("INPUT_REQUIRED",
                             "%s exceeds the %d-byte import limit" % (relative, MAX_SOURCE_BYTES))
        sha = _sha256_bytes(raw)
        declared = _text(entry.get("sha256"))
        if declared and declared.lower() != sha:
            raise CloseError("STALE_SOURCE",
                             "%s no longer matches its declared sha256 (declared %s, "
                             "found %s)" % (relative, declared.lower(), sha))
        if previous_scan:
            for known in previous_scan.get("sources", {}).get(role, []):
                if known.get("path") == relative and known.get("sha256") not in (None, sha):
                    raise CloseError(
                        "STALE_SOURCE",
                        "%s changed since the last ingest (was %s, found %s); the "
                        "original hash stays on record — bring the change in as a "
                        "V-04 revision, never as a silent re-ingest"
                        % (relative, known["sha256"], sha))
        source_hashes[relative] = sha
        spec = ROLES[role]
        record = {"path": relative, "sha256": sha, "bytes": len(raw),
                  "format": None, "row_count": 0, "status": "registered"}
        registry.setdefault(role, []).append(record)
        if spec["document"]:
            record["format"] = "document"
            documents.append({
                "document_ref": "invoice:%s" % sha[:12],
                "path": relative, "sha256": sha,
                "review_status": "unreviewed",
                "booking_status": "unbooked",
                "extraction": "not_attempted_in_v02",
                "detail": ("OCR/invoice2data may only produce an unreviewed draft; the "
                           "document is registered for human review and never booked"),
            })
            continue
        if path.suffix.lower() not in TABLE_SUFFIXES:
            raise CloseError("UNSUPPORTED_SCOPE",
                             "%s has unsupported table format %r (use csv/tsv/jsonl)"
                             % (relative, path.suffix))
        columns, rows, fmt = _parse_table(path, allow_empty=(role == "outcomes"))
        record["format"] = fmt
        record["row_count"] = len(rows)
        proposal = _propose_columns(role, columns)
        tables.setdefault(role, []).append({
            "entry": entry, "path": path, "relative": relative,
            "columns": columns, "rows": rows, "proposal": proposal, "format": fmt,
        })
        for conflict in proposal["conflicts"]:
            open_items.append({
                "kind": "column_conflict", "source": relative,
                "detail": "column %r also suggests field %r; %r stays mapped and %r "
                          "is preserved unmapped for review"
                          % (conflict["column"], conflict["field"],
                             conflict["kept"], conflict["column"])})
        for field in proposal["pending_fields"]:
            open_items.append({
                "kind": "mapping_pending", "source": relative, "field": field,
                "detail": "%s has no proposed column for required field %r; it stays "
                          "pending for human mapping and is never guessed"
                          % (relative, field)})

    ledger_roles = sorted(tables)
    rule_map = _load_rule_map(bundle_root, job)
    rule_check = _check_rule_map(job, rule_map, ledger_roles)
    violations: List[Dict[str, Any]] = list(rule_check["violations"])
    open_items.extend(rule_check["open_items"])

    # Cross-source validation: currency / account / half-open period / units /
    # duplicate ids. Nothing here is dropped silently; everything is listed.
    # A charge that appears in both the bills and usage export is one monetary
    # representation, so it is counted once in the currency mix; duplicated
    # representations never inflate the diagnostic.
    currencies: Dict[Optional[str], int] = {}
    charge_currencies: Dict[str, Optional[str]] = {}
    account_mismatches: List[Dict[str, Any]] = []
    period_outliers: List[Dict[str, Any]] = []
    unit_notes: List[Dict[str, Any]] = []
    seen_charges: Dict[str, str] = {}
    duplicate_charge_ids: List[Dict[str, Any]] = []
    seen_usage: Dict[str, str] = {}
    duplicate_usage_ids: List[Dict[str, Any]] = []
    start = _iso_date(job["period"]["from"], "period.from")
    end = _iso_date(job["period"]["to"], "period.to")
    pricing_by_id = {_text(rule.get("rule_id")): rule for rule in rule_check["pricing"]}
    task_accounts: Dict[str, set] = {}
    missing_task_rows: List[str] = []
    unapproved_allocation_count = 0
    unapproved_rule_refs: set = set()
    rows_without_amount = 0
    total_ledger_rows = 0
    id_fields = {"bills": "charge_id", "usage": "usage_id",
                 "tasks": "task_id", "runs": "run_id"}

    for role in ledger_roles:
        for table in tables[role]:
            mapping = table["proposal"]["mapping"]
            id_field = id_fields.get(role, "")
            for index, row in enumerate(table["rows"], start=1):
                row_id = _cell(row, mapping.get(id_field)) if id_field else ""
                total_ledger_rows += 1
                currency = _cell(row, mapping.get("currency")).upper()
                charge_ref = _cell(row, mapping.get("charge_id"))
                representation = charge_ref or (row_id if role == "bills" else "")
                if representation and representation in charge_currencies:
                    seen_currency = charge_currencies[representation]
                    if currency and seen_currency and currency != seen_currency:
                        violations.append({
                            "kind": "currency_mix",
                            "detail": "charge %s is represented as both %s and %s; "
                                      "one charge cannot carry two currencies"
                                      % (representation, seen_currency, currency)})
                    elif currency and not seen_currency:
                        # The first representation did not state a currency; the
                        # later one is the only observed value, not an extra row.
                        currencies[None] = currencies.get(None, 0) - 1
                        if currencies[None] <= 0:
                            del currencies[None]
                        currencies[currency] = currencies.get(currency, 0) + 1
                        charge_currencies[representation] = currency
                else:
                    if representation:
                        charge_currencies[representation] = currency or None
                    currencies[currency] = currencies.get(currency, 0) + 1
                if currency and currency != job["currency"]:
                    violations.append({
                        "kind": "currency_mix",
                        "detail": "%s row %s is %s while the period is %s; split "
                                  "the bundle by currency before merging"
                                  % (table["relative"], row_id or index,
                                     currency, job["currency"])})
                account = _cell(row, mapping.get("account_ref"))
                if account and account != job["account"]:
                    account_mismatches.append({
                        "id": row_id, "source": table["relative"], "account": account,
                        "detail": "%s row %s declares account %s while the job is %s"
                                  % (table["relative"], row_id or index,
                                     account, job["account"])})
                when = _parse_date_loose(_cell(row, mapping.get("date")))
                if when is not None and not (start <= when < end):
                    period_outliers.append({
                        "id": row_id or str(index), "source": table["relative"],
                        "date": when.isoformat(),
                        "reason": "%s is outside the half-open period [%s, %s)"
                                  % (when.isoformat(), job["period"]["from"],
                                     job["period"]["to"])})
                task_id = _cell(row, mapping.get("task_id"))
                allocation_rule = _cell(row, mapping.get("allocation_rule"))
                rate_rule = _cell(row, mapping.get("rate_rule"))
                if role in ("bills", "usage"):
                    allocation_approved = allocation_rule in rule_check["allocation_ids"]
                    if not task_id and not allocation_approved:
                        missing_task_rows.append(
                            row_id or "%s#%d" % (table["relative"], index))
                    # A Rate Plan / allocation reference is only approved when
                    # the rule map actually contains it. Unknown references are
                    # surfaced; they are never treated as an allocation driver.
                    unapproved_ref = ""
                    if allocation_rule and not allocation_approved:
                        unapproved_ref = allocation_rule
                    elif rate_rule and rate_rule not in pricing_by_id \
                            and rate_rule not in rule_check["allocation_ids"]:
                        unapproved_ref = rate_rule
                    if unapproved_ref:
                        unapproved_allocation_count += 1
                        unapproved_rule_refs.add(unapproved_ref)
                    amount_value = _cell(row, mapping.get(
                        "amount" if role == "bills" else "cost"))
                    if not amount_value:
                        rows_without_amount += 1
                if account and task_id:
                    task_accounts.setdefault(account, set()).add(task_id)
                unit = _cell(row, mapping.get("unit"))
                if unit and rate_rule in pricing_by_id:
                    rule_unit = _text(pricing_by_id[rate_rule].get("unit"))
                    if rule_unit and rule_unit != unit:
                        unit_notes.append({
                            "id": row_id, "rule_id": rate_rule,
                            "detail": "row unit %r differs from pricing rule %s unit %r"
                                      % (unit, rate_rule, rule_unit)})
                if role == "bills" and row_id:
                    if row_id in seen_charges:
                        duplicate_charge_ids.append({
                            "id": row_id, "source": table["relative"],
                            "first": seen_charges[row_id],
                            "detail": "charge %s repeats; only the first row can be "
                                      "booked" % row_id})
                    else:
                        seen_charges[row_id] = table["relative"]
                if role == "usage" and row_id:
                    if row_id in seen_usage:
                        duplicate_usage_ids.append({
                            "id": row_id, "source": table["relative"],
                            "first": seen_usage[row_id],
                            "detail": "usage %s repeats; only the first row can be "
                                      "counted" % row_id})
                    else:
                        seen_usage[row_id] = table["relative"]
    for mismatch in account_mismatches:
        violations.append(dict(mismatch, kind="account_mismatch"))

    # Coverage diagnostics, minimal version: four honest categories; each one
    # names what is missing and never invents an amount.
    shared_accounts = {account: sorted(tasks) for account, tasks in task_accounts.items()
                       if len(tasks) > 1}
    has_usage = bool(tables.get("usage"))
    has_buyer_ledger = any(
        _text(table["entry"].get("control_domain")) == "buyer"
        for table in tables.get("bills", []) + tables.get("usage", []))
    if not has_usage and not has_buyer_ledger:
        missing_real_cost = {"count": total_ledger_rows,
                             "detail": "no independent cost or usage record is ingested"}
    else:
        missing_real_cost = {
            "count": rows_without_amount,
            "detail": "ledger rows without a real amount are listed, never filled "
                      "with zero"}
    coverage_diagnostics = {
        "missing_task_id": {
            "count": len(missing_task_rows),
            "examples": missing_task_rows[:5],
            "detail": "ledger rows without a task link or approved allocation rule "
                      "stay unallocated",
        },
        "shared_account": {
            "count": len(shared_accounts),
            "examples": sorted(shared_accounts)[:5],
            "detail": "accounts carrying more than one task need an approved "
                      "shared-cost driver",
        },
        "unapproved_allocation": {
            "count": unapproved_allocation_count,
            "referenced_rules": sorted(unapproved_rule_refs),
            "detail": "rows reference rate-plan or allocation rules absent from the "
                      "approved rule map",
        },
        "missing_real_cost": missing_real_cost,
    }

    distinct_ledger_sources = sum(1 for role in LEDGER_CHECK_ROLES if tables.get(role))
    if distinct_ledger_sources == 0 and not documents:
        source_check_status = "none"
    elif distinct_ledger_sources < 2:
        source_check_status = "partial"
    else:
        source_check_status = "two_sided"

    blocked_code = _blocked_code(violations)
    currency_rows = [{"currency": currency or None, "rows": rows}
                     for currency, rows in sorted(currencies.items(),
                                                  key=lambda kv: kv[0] or "")]

    draft = {
        "schema": MAPPING_DRAFT_SCHEMA,
        "state": "draft",
        "job_id": job["job_id"],
        "source_hashes": dict(sorted(source_hashes.items())),
        "sources": {},
        "validation": {
            "blocked": blocked_code is not None,
            "blocked_code": blocked_code,
            "blocked_reason": (next((v["detail"] for v in violations), None)
                               if blocked_code else None),
            "currencies": currency_rows,
            "account_mismatches": account_mismatches,
            "duplicate_charge_ids": duplicate_charge_ids,
            "duplicate_usage_ids": duplicate_usage_ids,
            "period_outliers": period_outliers,
            "unit_notes": unit_notes,
            "rule_map": {
                "absent": rule_check["absent"],
                "violations": rule_check["violations"],
                "open_items": rule_check["open_items"],
            },
            "ledger_row_count": total_ledger_rows,
        },
        "coverage_diagnostics": coverage_diagnostics,
        "source_check_status": source_check_status,
        "open_items": open_items,
        "boundary": ("proposal only: nothing enters canonical until a human confirms "
                     "the mapping draft; pending fields and conflicts are never guessed"),
    }
    for role in ledger_roles:
        entries = []
        for table in tables[role]:
            proposal = table["proposal"]
            entries.append({
                "path": table["relative"], "sha256": _sha256_bytes(
                    table["path"].read_bytes()),
                "format": table["format"],
                "control_domain": _text(table["entry"].get("control_domain")),
                "row_count": len(table["rows"]),
                "columns": proposal["mapping"],
                "required_mapped": proposal["required_mapped"],
                "pending_fields": proposal["pending_fields"],
                "conflicts": proposal["conflicts"],
                "sample": dict(list(table["rows"][:1])[0]) if table["rows"] else {},
            })
        draft["sources"][role] = entries[0] if len(entries) == 1 else entries

    scan = {
        "schema": SCAN_SCHEMA,
        "job_sha256": _sha256_bytes((case_dir / "period-job.json").read_bytes()),
        "bundle_root": str(bundle_root),
        "sources": registry,
        "documents": documents,
    }

    draft_bytes = (json.dumps(draft, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    idempotent = draft_path.is_file() and draft_path.read_bytes() == draft_bytes
    intake_dir.mkdir(parents=True, exist_ok=True)
    scan_path.write_text(json.dumps(scan, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    draft_path.write_bytes(draft_bytes)
    return {"scan": scan, "mapping_draft": draft, "documents": documents,
            "idempotent": idempotent}


# ---------------------------------------------------------------------------
# human confirmation + canonical generation
# ---------------------------------------------------------------------------

def _read_draft(case_dir: Path) -> Tuple[bytes, Dict[str, Any]]:
    path = case_dir / "intake" / "mapping-draft.json"
    if not path.is_file():
        raise CloseError("INPUT_REQUIRED",
                         "no mapping draft found; run close ingest first (%s)" % path)
    raw = path.read_bytes()
    return raw, json.loads(raw.decode("utf-8"))


def _bundle_root(case_dir: Path) -> Path:
    scan_path = case_dir / "intake" / "scan.json"
    if not scan_path.is_file():
        raise CloseError("INPUT_REQUIRED",
                         "no ingest scan found; run close ingest first (%s)" % scan_path)
    scan = json.loads(scan_path.read_text(encoding="utf-8"))
    root = Path(_text(scan.get("bundle_root")))
    if not root.is_dir():
        raise CloseError("STALE_SOURCE",
                         "the ingested bundle %s is no longer available; re-ingest "
                         "from its current location" % root)
    return root


def confirm_mapping(case_dir: Path, *, reviewer: str,
                    control_domains: Dict[str, str] = None,
                    note: str = "") -> Dict[str, Any]:
    """Record the human mapping confirmation, then generate the canonical bundle."""
    case_dir = Path(case_dir)
    if not _text(reviewer):
        raise CloseError("INPUT_REQUIRED", "confirming the mapping needs a reviewer name")
    confirmed_path = case_dir / "intake" / "mapping-confirmed.json"
    if confirmed_path.exists():
        raise CloseError("REVIEW_REQUIRED",
                         "mapping is already confirmed; revisions arrive in V-04 and "
                         "never silently overwrite the confirmed state")
    job = load_period_job(case_dir / "period-job.json")
    draft_raw, draft = _read_draft(case_dir)
    blocked = draft.get("validation", {}).get("blocked")
    if blocked:
        raise CloseError(draft["validation"].get("blocked_code") or "REVIEW_REQUIRED",
                         "the mapping draft is blocked: %s"
                         % draft["validation"].get("blocked_reason"))

    overrides = {role: _text(domain) for role, domain in (control_domains or {}).items()}
    for role, domain in overrides.items():
        if domain not in RESOLVED_CONTROL_DOMAINS:
            raise CloseError("INPUT_REQUIRED",
                             "control domain for %s must be vendor or buyer (got %r)"
                             % (role, domain))
    resolved_domains: Dict[str, str] = {}
    pending_roles: List[str] = []
    for entry in job["sources"]:
        role = entry["role"]
        domain = overrides.get(role) or _text(entry.get("control_domain"))
        if domain not in RESOLVED_CONTROL_DOMAINS:
            pending_roles.append(role)
        else:
            resolved_domains.setdefault(role, domain)
    if pending_roles:
        raise CloseError("REVIEW_REQUIRED",
                         "control domain still pending for %s; resolve each source to "
                         "vendor or buyer with --control-domain role=vendor|buyer"
                         % ", ".join(sorted(set(pending_roles))))

    confirmation = {
        "schema": MAPPING_CONFIRMED_SCHEMA,
        "state": "confirmed",
        "job_id": job["job_id"],
        "based_on_draft_sha256": _sha256_bytes(draft_raw),
        "confirmed_by": _text(reviewer),
        "attestation": "attested_by_declaration",
        "confirmed_at": _now_iso(),
        "note": _text(note),
        "sources": {},
    }
    for role, entries in (draft.get("sources") or {}).items():
        if isinstance(entries, dict):
            entries = [entries]
        confirmation["sources"][role] = {
            "control_domain": resolved_domains.get(role, "vendor"),
            "columns": [entry.get("columns") or {} for entry in entries],
            "pending_fields_approved_as_gaps": [
                entry.get("pending_fields") or [] for entry in entries],
        }
    if job.get("rule_map"):
        bundle_root = _bundle_root(case_dir)
        rule_source = _resolve_inside(bundle_root, job["rule_map"], "rule_map")
        if rule_source.is_file():
            confirmation["rule_map"] = {
                "path": job["rule_map"],
                "sha256": _sha256_bytes(rule_source.read_bytes())}
    confirmed_path.parent.mkdir(parents=True, exist_ok=True)
    confirmed_path.write_text(
        json.dumps(confirmation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        generate_canonical(case_dir, confirmation)
    except Exception:
        # Do not leave a confirmed mapping without a canonical bundle: the
        # retry path must stay open. A partial canonical directory is removed
        # and rebuilt by the next generate_canonical call.
        confirmed_path.unlink()
        raise
    return confirmation


def _canonical_table(table: Dict[str, Any]) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Apply the confirmed column mapping; unmapped columns pass through.

    A rename that would collide with a *different* original column is skipped —
    the original column stays authoritative and the conflict is already on
    record in the mapping draft.  Renaming the mapped column itself (for example
    ``Adjustment Type`` -> ``adjustment_type``) is safe even when the normalized
    names match.
    """
    mapping = table["proposal"]["mapping"]
    columns = table["columns"]
    normalized_columns = {_norm(column) for column in columns}
    rename: Dict[str, str] = {}
    for field, column in mapping.items():
        if field == column:
            continue
        if _norm(field) in normalized_columns and _norm(column) != _norm(field):
            continue
        rename[column] = field
    header = [rename.get(column, column) for column in columns]
    rows: List[Dict[str, Any]] = []
    for row in table["rows"]:
        out: Dict[str, Any] = {}
        for column in columns:
            key = rename.get(column, column)
            value = row.get(column)
            out[key] = value if isinstance(value, bool) else _text(value)
        rows.append(out)
    return header, rows


def generate_canonical(case_dir: Path,
                       confirmation: Dict[str, Any] = None) -> Path:
    """Write the Period-Intake-compatible bundle; only runs after confirmation."""
    case_dir = Path(case_dir)
    job = load_period_job(case_dir / "period-job.json")
    if confirmation is None:
        path = case_dir / "intake" / "mapping-confirmed.json"
        if not path.is_file():
            raise CloseError("REVIEW_REQUIRED",
                             "canonical generation requires a confirmed mapping draft")
        confirmation = json.loads(path.read_text(encoding="utf-8"))
    bundle_root = _bundle_root(case_dir)
    scan = json.loads((case_dir / "intake" / "scan.json").read_text(encoding="utf-8"))
    rule_map = _load_rule_map(bundle_root, job)
    rule_check = _check_rule_map(job, rule_map, sorted(scan["sources"]))
    blocking = _blocked_code(rule_check["violations"])
    if blocking:
        raise CloseError(blocking, rule_check["violations"][0]["detail"])

    tables: Dict[str, List[Dict[str, Any]]] = {}
    for entry in job["sources"]:
        role = entry["role"]
        if ROLES[role]["document"]:
            continue
        relative = Path(entry["path"]).as_posix()
        path = _resolve_inside(bundle_root, relative, "source %s" % role)
        columns, rows, fmt = _parse_table(path, allow_empty=(role == "outcomes"))
        tables.setdefault(role, []).append({
            "entry": entry, "path": path, "relative": relative,
            "columns": columns, "rows": rows,
            "proposal": _propose_columns(role, columns), "format": fmt,
        })

    canonical = case_dir / "canonical"
    if canonical.exists():
        shutil.rmtree(canonical)
    canonical.mkdir(parents=True)

    manifest: Dict[str, Any] = {
        "case_id": job["job_id"],
        "vendor": job["vendor"],
        "account_ref": job["account"],
        "currency": job["currency"],
        "period": {"start": job["period"]["from"], "end": job["period"]["to"]},
        "period_convention": "half-open [from, to); timezone %s"
                             % job["period"]["timezone"],
        "rule_map": "rule-map.json",
        "sources": {},
    }
    rule_version_by_id = {
        _text(rule.get("rule_id")): _text(rule.get("version"))
        for rule in (rule_map or {}).get("rules", [])}

    for role in sorted(tables):
        spec = ROLES[role]
        target = spec["target"]
        suffix = ".jsonl" if spec["format"].endswith("jsonl") else ".csv"
        target_path = canonical / (target + suffix)
        entries = []
        header_out: List[str] = []
        all_rows: List[Dict[str, Any]] = []
        for table in tables[role]:
            header, rows = _canonical_table(table)
            for key in header:
                if key not in header_out:
                    header_out.append(key)
            all_rows.extend(rows)
            domain = _text((confirmation.get("sources", {}).get(role) or {})
                           .get("control_domain", "vendor"))
            entry = {"path": target_path.name, "format": spec["format"]}
            entry.update(DOMAIN_EVIDENCE[domain])
            rule_id = rule_check["intake_formats"].get(spec["format"])
            if rule_id:
                entry["rule_id"] = rule_id
                entry["rule_version"] = rule_version_by_id.get(rule_id, "")
            entries.append(entry)
        if suffix == ".jsonl":
            target_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in all_rows),
                encoding="utf-8")
        else:
            fieldnames = header_out or ["charge_id"]
            with target_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames,
                                        extrasaction="ignore")
                writer.writeheader()
                writer.writerows(all_rows)
        manifest["sources"][target] = entries

    invoice_entries = []
    for document in scan.get("documents", []):
        source = _resolve_inside(bundle_root, document["path"], "invoice document")
        if not source.is_file():
            raise CloseError("INPUT_REQUIRED",
                             "invoice document missing: %s" % document["path"])
        shutil.copyfile(source, canonical / source.name)
        invoice_entries.append({
            "path": source.name, "sha256": document.get("sha256"),
            "document_ref": document.get("document_ref"),
            "review_status": "unreviewed", "booking_status": "unbooked",
            "detail": ("OCR/invoice2data draft only; not a ledger source and not "
                       "booked as cost"),
        })
    # Invoice documents are review artifacts, not ledger sources: they stay
    # outside manifest.sources so Period Intake can never read them as booked
    # cost rows. The canonical generator's job is to preserve the original
    # bytes for a human, not to turn extracted text into an amount.
    if invoice_entries:
        manifest["documents"] = invoice_entries

    if job.get("rule_map"):
        rule_source = _resolve_inside(bundle_root, job["rule_map"], "rule_map")
        shutil.copyfile(rule_source, canonical / "rule-map.json")
    else:
        (canonical / "rule-map.json").write_text(
            json.dumps({"schema_version": "1.0", "rules": []}, indent=2) + "\n",
            encoding="utf-8")
    (canonical / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return canonical


# ---------------------------------------------------------------------------
# BuyerCloseCase read-only projection (PRD §12.2)
# ---------------------------------------------------------------------------

def _latest_mtime(paths: List[Path]) -> Optional[str]:
    times = [p.stat().st_mtime for p in paths if p.is_file()]
    if not times:
        return None
    return datetime.fromtimestamp(max(times), timezone.utc).isoformat(timespec="seconds")


def build_case(case_dir: Path) -> Dict[str, Any]:
    """Project the close case for reviewers; reads state files, writes nothing."""
    case_dir = Path(case_dir)
    job = load_period_job(case_dir / "period-job.json")
    scan_path = case_dir / "intake" / "scan.json"
    draft_path = case_dir / "intake" / "mapping-draft.json"
    confirmed_path = case_dir / "intake" / "mapping-confirmed.json"
    canonical_manifest = case_dir / "canonical" / "manifest.json"
    calculation_path = case_dir / "calculation.json"
    report_path = case_dir / "report" / "report.json"
    review_path = case_dir / "review.json"
    has_scan = scan_path.is_file()
    draft = json.loads(draft_path.read_text(encoding="utf-8")) if draft_path.is_file() else None
    confirmation = (json.loads(confirmed_path.read_text(encoding="utf-8"))
                    if confirmed_path.is_file() else None)
    scan = json.loads(scan_path.read_text(encoding="utf-8")) if has_scan else None
    calculation = (json.loads(calculation_path.read_text(encoding="utf-8"))
                   if calculation_path.is_file() else None)
    report = (json.loads(report_path.read_text(encoding="utf-8"))
              if report_path.is_file() else None)
    review = (json.loads(review_path.read_text(encoding="utf-8"))
              if review_path.is_file() else None)
    confirmed = confirmation is not None

    snapshot_refs: List[Dict[str, Any]] = []
    domains: Dict[str, List[str]] = {"vendor": [], "buyer": [], "pending": []}
    resolved = (confirmation or {}).get("sources", {})
    for entry in job["sources"]:
        role = entry["role"]
        domain = _text((resolved.get(role) or {}).get("control_domain")) \
            or _text(entry.get("control_domain"))
        bucket = domain if domain in ("vendor", "buyer") else "pending"
        if not ROLES[role]["document"]:
            domains[bucket].append(role)
        ref = {"role": role, "path": Path(entry["path"]).as_posix(),
               "control_domain": domain,
               "declared_sha256": _text(entry.get("sha256")) or None}
        snapshot_refs.append(ref)
    if scan:
        known = {row["path"]: row["sha256"]
                 for rows in scan.get("sources", {}).values() for row in rows}
        for ref in snapshot_refs:
            if ref["path"] in known:
                ref["sha256"] = known[ref["path"]]

    distinct_ledger = sum(1 for role in LEDGER_CHECK_ROLES if role in domains["vendor"]
                          or role in domains["buyer"] or role in domains["pending"])
    buyer_ledger = any(role in domains["buyer"] for role in ("bills", "usage"))
    vendor_ledger_count = sum(1 for role in ("bills", "usage") if role in domains["vendor"])

    # Source-check conclusions decidable at import stage. They are never PASS:
    # PASS/FAIL verdicts belong to the canonical run in V-03.
    source_check: Dict[str, Dict[str, str]] = {}
    if distinct_ledger < 2:
        source_check["Invoice Reconciled"] = {
            "status": "UNPROVABLE",
            "reason": ("fewer than two distinct ledger sources are ingested; "
                       "reconciliation needs a second source and is never PASS at "
                       "import stage")}
    else:
        source_check["Invoice Reconciled"] = {
            "status": "PENDING_V03",
            "reason": "two ledger sources are ingested; canonical reconciliation runs in V-03"}
    if not buyer_ledger:
        source_check["Buyer Verified"] = {
            "status": "UNPROVABLE",
            "reason": ("every ledger source is vendor-controlled (even when downloaded "
                       "by the buyer); repeated vendor exports never qualify as buyer "
                       "verification")}
    else:
        source_check["Buyer Verified"] = {
            "status": "PENDING_V03",
            "reason": "a buyer-controlled ledger source is declared; qualification runs in V-03"}
    source_check["Vendor Reconciled"] = (
        {"status": "PENDING_V03",
         "reason": "two vendor-controlled exports can be compared in V-03"}
        if vendor_ledger_count >= 2 else
        {"status": "UNPROVABLE",
         "reason": "two vendor-controlled exports are required for Vendor Reconciled"})
    if report is not None:
        # The published report owns the canonical verdicts now; keep the
        # import-stage source check from looking like it is still pending.
        for name in ("Invoice Reconciled", "Vendor Reconciled", "Buyer Verified"):
            verdict = (report.get("verdicts") or {}).get(name)
            if not verdict:
                continue
            rows = verdict.get("rows") or []
            source_check[name] = {
                "status": verdict.get("status") or "UNPROVABLE",
                "reason": (rows[0].get("reason") if rows else
                           "; ".join(verdict.get("limitations") or []) or
                           "see report verdict"),
            }

    axes: Dict[str, str] = {}
    axes["finance"] = "pending_calculation" if confirmed and distinct_ledger else "blocked"
    outcome_requested = job.get("task_economics_requested") or any(
        entry["role"] == "outcomes" for entry in job["sources"])
    for axis in ("verification", "delivery", "economics"):
        if not outcome_requested:
            axes[axis] = "not_applicable"
        elif confirmed:
            axes[axis] = "pending_calculation"
        else:
            axes[axis] = "blocked"
    completion_semantics = {
        "finance": ("blocked until a human confirms the mapping and a ledger pair "
                    "exists; pending_calculation means ready for the V-03 canonical "
                    "run"),
    }
    if report is not None:
        # A published report is the completed canonical run: show its four
        # independent axes instead of the pre-calculation placeholders.
        for axis in ("finance", "verification", "delivery", "economics"):
            axes[axis] = _text(report.get(axis + "_status")) or axes[axis]
            meaning = (report.get("axes_semantics") or {}).get(axis + "_status")
            if meaning:
                completion_semantics[axis] = meaning

    missing_inputs: List[Dict[str, str]] = []
    if distinct_ledger == 0:
        missing_inputs.append({
            "item": "an independent cost or usage record",
            "why": "an invoice alone cannot be reconciled; a second ledger source "
                   "is required"})
    if distinct_ledger and not buyer_ledger:
        missing_inputs.append({
            "item": "a buyer-controlled ledger source",
            "why": "Buyer Verified stays UNPROVABLE while all ledger sources are "
                   "vendor exports"})
    if not job.get("rule_map"):
        missing_inputs.append({
            "item": "an approved rule map (price table with currency, unit, "
                    "validity and version)",
            "why": "Rule Repriced needs approved per-unit pricing that covers the period"})
    if not confirmed:
        missing_inputs.append({
            "item": "field mapping confirmation by a human reviewer",
            "why": "nothing enters canonical before the mapping draft is confirmed"})
    pending_domains = sorted({entry["role"] for entry in job["sources"]
                              if _text(entry.get("control_domain"))
                              not in RESOLVED_CONTROL_DOMAINS})
    if pending_domains and not confirmed:
        missing_inputs.append({
            "item": "control-domain resolution for %s" % ", ".join(pending_domains),
            "why": "each source must be confirmed as vendor- or buyer-controlled"})
    if draft is not None and not confirmed:
        for item in draft.get("open_items", []):
            if item.get("kind") == "mapping_pending":
                missing_inputs.append({
                    "item": "a column for %s.%s" % (item.get("source"), item.get("field")),
                    "why": "the required field has no proposed column; it is never guessed"})
                break

    review_queue: List[Dict[str, Any]] = []
    if draft is not None:
        for item in draft.get("open_items", []):
            review_queue.append({
                "ref": "%s:%s" % (item.get("source", "rule-map"),
                                  item.get("kind", "open")),
                "kind": item.get("kind"), "detail": item.get("detail")})
    for document in (scan or {}).get("documents", []):
        review_queue.append({"ref": document["document_ref"],
                             "kind": "invoice_document",
                             "detail": document.get("detail")})
    if report is not None:
        for item in report.get("open_items", []):
            review_queue.append({
                "ref": item.get("id"), "kind": item.get("kind"),
                "detail": item.get("detail"), "state": item.get("state"),
                "owner": item.get("owner")})

    # Cross-period reuse: only rules whose stated validity covers this period
    # may be proposed at all, and every one of them still requires fresh review
    # next period (a PASS last month never proves this month's price).
    reusable: List[Dict[str, Any]] = []
    rule_versions: List[Dict[str, Any]] = []
    start = _iso_date(job["period"]["from"], "period.from")
    end = _iso_date(job["period"]["to"], "period.to")
    last_day = end - timedelta(days=1)
    if scan is not None and job.get("rule_map"):
        try:
            bundle_root = _bundle_root(case_dir)
            rule_map = _load_rule_map(bundle_root, job)
        except CloseError:
            rule_map = None
        for rule in (rule_map or {}).get("rules", []):
            if _text(rule.get("kind")) == "pricing":
                rule_versions.append({"rule_id": rule.get("rule_id"),
                                      "version": rule.get("version")})
                valid_from = _text(rule.get("valid_from"))
                valid_to = _text(rule.get("valid_to"))
                if not valid_from or not valid_to:
                    continue
                if _iso_date(valid_from, "valid_from") <= start \
                        and _iso_date(valid_to, "valid_to") >= last_day:
                    reusable.append({
                        "rule_id": _text(rule.get("rule_id")),
                        "version": rule.get("version"),
                        "valid_from": valid_from, "valid_to": valid_to,
                        "requires_re_review_next_period": True})

    if report is not None:
        if review is not None:
            task_state, blocked_code = "delivered", None
            next_actor = "human_reviewer"
            next_required = {
                "action": "sign off the period or open a revision (V-04)",
                "ticket": "V-04"}
        else:
            task_state, blocked_code = "needs_review", "REVIEW_REQUIRED"
            next_actor = "human_reviewer"
            next_required = {
                "action": "review the month-end report and record a named reviewer",
                "report": "close review %s --by <reviewer> --scope <scope>"
                          % case_dir,
                "ticket": "V-03"}
    elif calculation is not None:
        task_state, blocked_code = "ready", None
        next_actor = "verovo_cli"
        next_required = {"action": "build the month-end report (close report)",
                         "ticket": "V-03"}
    elif confirmed and distinct_ledger:
        task_state, blocked_code = "ready", None
        next_actor = "verovo_cli"
        next_required = {"action": "run the canonical calculation (close calculate)",
                         "ticket": "V-03"}
    elif draft is not None and draft.get("validation", {}).get("blocked"):
        task_state = "needs_input"
        blocked_code = draft["validation"].get("blocked_code") or "INPUT_REQUIRED"
        next_actor = "buyer_operator"
        next_required = {"action": "resolve the blocked validation (for example split "
                                   "the bundle by currency)", "ticket": "V-02"}
    elif has_scan and distinct_ledger == 0:
        task_state, blocked_code = "needs_input", "INPUT_REQUIRED"
        next_actor = "buyer_operator"
        next_required = {"action": "supply the missing inputs listed in missing_inputs",
                         "ticket": "V-02"}
    elif pending_domains and not confirmed:
        task_state, blocked_code = "needs_review", "REVIEW_REQUIRED"
        next_actor = "human_reviewer"
        next_required = {
            "action": "resolve source control domains, then confirm the mapping",
            "control_domains": "close mapping --control-domain role=vendor|buyer",
            "mapping": "close mapping --confirm --review-by <reviewer>",
            "ticket": "V-02"}
    elif not confirmed and has_scan:
        task_state, blocked_code = "needs_review", "REVIEW_REQUIRED"
        next_actor = "human_reviewer"
        next_required = {
            "action": "confirm the field mapping draft",
            "mapping": "close mapping --confirm --review-by <reviewer>",
            "ticket": "V-02"}
    elif has_scan:
        task_state, blocked_code = "needs_input", "INPUT_REQUIRED"
        next_actor = "buyer_operator"
        next_required = {"action": "supply the missing inputs listed in missing_inputs",
                         "ticket": "V-02"}
    else:
        task_state, blocked_code = "created", "INPUT_REQUIRED"
        next_actor = "buyer_operator"
        next_required = {"action": "ingest the period bundle (close ingest)",
                         "ticket": "V-02"}

    state_files = [case_dir / "period-job.json", scan_path, draft_path,
                   confirmed_path, canonical_manifest, calculation_path,
                   report_path, review_path]
    return {
        "schema": BUYER_CLOSE_CASE_SCHEMA,
        "read_only": True,
        "close_case_id": job["job_id"],
        "identity": {
            "buyer_account_ref": {"buyer": job.get("buyer", ""),
                                  "account": job["account"]},
            "vendor_ref": job["vendor"],
            "period_range": dict(job["period"]),
            "currency": job["currency"],
        },
        "sources": {
            "source_snapshot_refs": snapshot_refs,
            "collection_control_domains": domains,
            "documents": (scan or {}).get("documents", []),
            "approved_rule_version": rule_versions,
            "mapping_revision": ("confirmed:%s"
                                 % confirmation.get("based_on_draft_sha256", "")[:12]
                                 if confirmed else "unconfirmed"),
        },
        "computed_facts": {
            "amount_basis": {
                "ledger_sources": distinct_ledger,
                "note": ("observed parsed rows only; canonical recalculation, "
                         "allocation and the five-amount reconciliation arrive in V-03"),
            },
            "difference_items": [],
            "explanation_basis_refs": [],
            "completion_axes": axes,
            "completion_axes_semantics": completion_semantics,
            "source_check": source_check,
        },
        "review_flow": {
            "review_queue": review_queue,
            "assigned_reviewer": ((review or {}).get("reviewer")
                                  or (confirmation or {}).get("confirmed_by")),
            "approved_scope": ((review or {}).get("approved_scope")
                               or (confirmation or {}).get("sources")),
            "outstanding_requests": missing_inputs,
            "signoff_revision": ((review or {}).get("signoff") or {}).get("state"),
        },
        "history": {
            "previous_period_ref": None,
            "supersedes_revision": None,
            "credits_pending": [],
            "credits_applied": [],
            "next_period_reusable_rules": reusable,
        },
        "missing_inputs": missing_inputs,
        "c01_status": {
            "task_state": task_state,
            "next_actor": next_actor,
            "next_required_action": next_required,
            "blocked_reason_code": blocked_code,
            "last_updated_at": _latest_mtime(state_files),
            "has_pending_external_effect": False,
            "artifact_refs": [path.relative_to(case_dir).as_posix()
                              for path in state_files if path.is_file()],
        },
        "boundary": ("read-only projection of the close case; it never writes back into "
                     "canonical structures and no verdict here authorizes payment"),
    }


def stub_command(command: str) -> None:
    """V-04 commands refuse loudly instead of being half-implemented."""
    ticket = LATER_TICKET_COMMANDS[command]
    raise CloseError(
        "UNSUPPORTED_SCOPE",
        "'close %s' is implemented in ticket %s; V-03 delivers init/ingest/"
        "mapping/calculate/report/review/status and refuses to fake revisions "
        "or signoff" % (command, ticket))
