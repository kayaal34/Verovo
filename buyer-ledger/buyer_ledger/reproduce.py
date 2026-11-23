"""第三方可复核的重放验收包（Verovo「可重现」落点）。

一个目录自包含：原始用量文件、规则映射、身份映射、重放结果、差异分类和
所有文件的 sha256。另一个独立审阅者只靠这个目录就能重算结论，不需要访问
供应商后台，也不依赖本机的临时文件。

纪律：
1. 输入先复制进 inputs/，重放只读包内副本；manifest 记录每个文件 sha256
   与字节数，任何一字节被改，verify 立即 FAIL；
2. 身份映射只从事件 properties 或外部映射文件的显式字段取（task_id /
   attempt_id / retry_of / cache_hit_of / vendor / vendor_record_id），
   缺项保持 null 并列入 unmatched——不拿 external_subject_id 冒充任务；
3. attempt ≠ operation ≠ delegation，映射表如实标出缺哪一层，缺项不能让
   结论升级为 operation 级；
4. 差异只做分类计数（排除原因、金额为空原因、重复/冲突），不写因果；
5. verify 会重跑 replay 并重算身份映射与差异，manifest 不完整或文件缺失
   返回 UNPROVEN，不返回 PASS。
"""
from __future__ import annotations

import hashlib
import html
import json
import shutil
import tempfile
import uuid
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import usage_replay

SCHEMA = "verovo.reproduce-bundle/1"
IDENTITY_SCHEMA = "verovo.identity-mapping/1"
GENERATOR = "buyer_ledger.reproduce"
GENERATOR_VERSION = "1"
MANIFEST_NAME = "manifest.json"
README_NAME = "README.md"
IDENTITY_NAME = "identity-mapping.json"
DISCREPANCIES_NAME = "discrepancies.json"
DATA_CLASSIFICATIONS = ("confidential", "synthetic", "public-redacted")

REQUIRED_MANIFEST_KEYS = (
    "schema", "generator", "generator_version", "generated_at", "period", "input_fingerprint",
    "revision_id", "rule_map", "files", "replay_counts", "replay_metrics",
    "replay_integrity", "charge_verdict", "identity", "discrepancies")

IDENTITY_FIELDS = ("task_id", "attempt_id", "retry_of", "cache_hit_of",
                   "vendor", "vendor_record_id")
_PROP_KEYS = {
    "task_id": ("task_id", "task", "external_task_id"),
    "attempt_id": ("attempt_id", "attempt"),
    "retry_of": ("retry_of", "retry_transaction_id"),
    "cache_hit_of": ("cache_hit_of", "cache_of", "cache_source_transaction_id"),
    "vendor": ("vendor", "provider"),
    "vendor_record_id": ("vendor_record_id", "vendor_id", "provider_record_id"),
}
_GRAIN_NOTE = (
    "attempt ≠ operation ≠ delegation。映射缺项保持 null，不推断；"
    "只有 task_id 与 attempt_id 都齐全的记录才可用于尝试级复核，"
    "operation 级结论需要额外的分组证据。")
_CAUSALITY_NOTE = (
    "以下只是按重放输出做的分类计数，不是原因判断，也不是因果归因；"
    "同一差异可能同时有多个原因，需人工对照原始账单与供应商记录。")


class ReproduceError(ValueError):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _now_iso(when: Optional[datetime] = None) -> str:
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


# ---------------------------------------------------------------------------
# identity mapping
# ---------------------------------------------------------------------------

def _load_identity_map(path: Path) -> Dict[str, Dict[str, str]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        if "mappings" in payload or "records" in payload:
            payload = payload.get("mappings") or payload.get("records") or {}
        return {str(key): dict(value or {}) for key, value in payload.items()}
    if isinstance(payload, list):
        return {_text(item.get("transaction_id")): dict(item)
                for item in payload if _text(item.get("transaction_id"))}
    raise ReproduceError("identity map 必须是 {transaction_id: {...}} 或记录列表")


def derive_identity_map(events: List[Dict[str, Any]],
                        extra: Optional[Dict[str, Dict[str, Any]]] = None
                        ) -> Dict[str, Any]:
    """从事件 properties 与可选外部映射构建身份表；缺项如实列出。"""
    extra = extra or {}
    records: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    dangling: List[Dict[str, Any]] = []
    txids = {_text(event.get("transaction_id")) for event in events}
    coverage = {field: 0 for field in IDENTITY_FIELDS}
    for event in events:
        txid = _text(event.get("transaction_id"))
        props = event.get("properties") or {}
        supplied = extra.get(txid) or {}
        record: Dict[str, Any] = {"transaction_id": txid,
                                  "external_subject_id": _text(event.get("external_subject_id"))}
        sources = {}
        for field in IDENTITY_FIELDS:
            value = ""
            source = ""
            for key in _PROP_KEYS[field]:
                if _text(props.get(key)):
                    value, source = _text(props.get(key)), "event_properties"
                    break
            if not value and _text(supplied.get(field)):
                value, source = _text(supplied.get(field)), "identity_map"
            record[field] = value or None
            sources[field] = source or None
            if value:
                coverage[field] += 1
        record["sources"] = sources
        complete = bool(record["task_id"] and record["attempt_id"])
        record["complete_for_attempt"] = complete
        records.append(record)
        missing = [field for field in ("task_id", "attempt_id", "vendor")
                   if not record[field]]
        if missing:
            unmatched.append({"transaction_id": txid, "missing": missing,
                              "reason": "NOT_IN_EVENT_PROPERTIES_OR_IDENTITY_MAP"})
        for field in ("retry_of", "cache_hit_of"):
            ref = record[field]
            if ref and ref not in txids:
                dangling.append({"transaction_id": txid, "field": field,
                                 "points_to": ref,
                                 "reason": "REFERENCED_TRANSACTION_NOT_IN_BUNDLE"})
    total = len(records)
    return {
        "schema": IDENTITY_SCHEMA,
        "grain_note": _GRAIN_NOTE,
        "records": records,
        "coverage": {"records": total,
                     **{field: coverage[field] for field in IDENTITY_FIELDS},
                     "complete_for_attempt": sum(
                         1 for r in records if r["complete_for_attempt"])},
        "unmatched": unmatched,
        "unmatched_count": len(unmatched),
        "dangling_refs": dangling,
        "dangling_count": len(dangling),
        "note": ("缺项保持 null：external_subject_id 是计费主体，不是任务 id。"
                 "需要任务/尝试级复核时，请补身份映射后重新打包。"),
    }


# ---------------------------------------------------------------------------
# discrepancy classification
# ---------------------------------------------------------------------------

def classify_discrepancies(payload: Dict[str, Any]) -> Dict[str, Any]:
    exclusion: Dict[str, int] = {}
    amount: Dict[str, int] = {}
    for line in payload.get("lines") or []:
        for reason in line.get("exclusion_reasons") or []:
            exclusion[reason] = exclusion.get(reason, 0) + 1
        if line.get("amount_reason"):
            amount[line["amount_reason"]] = amount.get(line["amount_reason"], 0) + 1
    counts = payload.get("counts") or {}
    return {
        "schema": "verovo.discrepancy-classification/1",
        "note": _CAUSALITY_NOTE,
        "counts": {
            "lines": len(payload.get("lines") or []),
            "included": counts.get("included", 0),
            "excluded": counts.get("excluded", 0),
            "duplicates": counts.get("duplicates", 0),
            "conflicts": counts.get("conflicts", 0),
            "late": counts.get("late", 0),
            "amount_null": sum(amount.values()),
        },
        "exclusion_reasons": dict(sorted(exclusion.items())),
        "amount_reasons": dict(sorted(amount.items())),
        "aggregation_state": payload.get("aggregation_state"),
        "delivery_state": payload.get("delivery_state"),
    }


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def build_bundle(event_paths: List[Path], *, period: str, rule_map_path: Path,
                 out_dir: Path, identity_map_path: Optional[Path] = None,
                 closed_at: str = "", previous_revision: str = "",
                 generated_at: Optional[datetime] = None,
                 overwrite: bool = False, data_classification: str = "confidential",
                 sharing_approved_by: str = "") -> Dict[str, Any]:
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()) and not overwrite:
        raise ReproduceError("输出目录非空：换个目录或显式 overwrite（避免覆盖旧证据）")
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=out.name + ".tmp-", dir=str(out.parent)))
    backup = out.parent / (out.name + ".backup-" + uuid.uuid4().hex)
    had_original = out.exists()
    try:
        result = _build_bundle_direct(
            event_paths, period=period, rule_map_path=rule_map_path, out_dir=staging,
            identity_map_path=identity_map_path, closed_at=closed_at,
            previous_revision=previous_revision, generated_at=generated_at,
            overwrite=False, data_classification=data_classification,
            sharing_approved_by=sharing_approved_by)
        result["manifest"]["verify_command"] = "python3 -m buyer_ledger reproduce verify %s" % out
        _write_json(staging / MANIFEST_NAME, result["manifest"])
        verification = verify_bundle(staging)
        if verification["verdict"] != "PASS":
            raise ReproduceError("新重放包未通过自验，正式输出保持不变")
        if had_original:
            out.rename(backup)
        try:
            staging.rename(out)
        except Exception:
            if had_original and backup.exists() and not out.exists():
                backup.rename(out)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        result["out"] = str(out)
        result["paths"] = {
            key: str(out / Path(value).relative_to(staging))
            for key, value in result["paths"].items()
        }
        return result
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if had_original and backup.exists() and not out.exists():
            backup.rename(out)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _build_bundle_direct(event_paths: List[Path], *, period: str, rule_map_path: Path,
                 out_dir: Path, identity_map_path: Optional[Path] = None,
                 closed_at: str = "", previous_revision: str = "",
                 generated_at: Optional[datetime] = None,
                 overwrite: bool = False, data_classification: str = "confidential",
                 sharing_approved_by: str = "") -> Dict[str, Any]:
    classification = _text(data_classification) or "confidential"
    sharing_approver = _text(sharing_approved_by)
    if classification not in DATA_CLASSIFICATIONS:
        raise ReproduceError("data_classification 必须是 %s" % ", ".join(DATA_CLASSIFICATIONS))
    if classification == "public-redacted" and not sharing_approver:
        raise ReproduceError("public-redacted 需要具名脱敏发布批准人")
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()) and not overwrite:
        raise ReproduceError("输出目录非空：换个目录或显式 overwrite（避免覆盖旧证据）")
    inputs = out / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)

    file_entries: List[Dict[str, Any]] = []

    def _copy(src: Path, role: str, index: int) -> Path:
        src = Path(src)
        if not src.is_file():
            raise ReproduceError("input not found: %s" % src)
        dest = inputs / ("%02d-%s" % (index, src.name))
        shutil.copyfile(src, dest)
        file_entries.append({"path": str(dest.relative_to(out)), "role": role,
                             "source": str(src), "sha256": _sha(dest),
                             "bytes": dest.stat().st_size})
        return dest

    events: List[Dict[str, Any]] = []
    for index, path in enumerate(event_paths, 1):
        dest = _copy(Path(path), "events", index)
        events.extend(usage_replay.load_events(dest))
    rule_copy = _copy(Path(rule_map_path), "rule-map", len(event_paths) + 1)
    rule_map = usage_replay.load_rule_map(rule_copy)

    extra = {}
    if identity_map_path:
        identity_copy = _copy(Path(identity_map_path), "identity-map",
                              len(event_paths) + 2)
        extra = _load_identity_map(identity_copy)

    payload = usage_replay.replay(
        events, period=period, rule_map=rule_map, closed_at=closed_at,
        previous_revision=previous_revision,
        generated_at=generated_at or datetime.now(timezone.utc),
        source_files=[Path(entry["path"]) for entry in file_entries
                      if entry["role"] == "events"])
    paths = usage_replay.write_replay(payload, out)
    for role, key in (("replay", "json"), ("replay-lines", "csv"),
                      ("replay-diff", "diff")):
        path = Path(paths[key])
        file_entries.append({"path": str(path.relative_to(out)), "role": role,
                             "source": "generated", "sha256": _sha(path),
                             "bytes": path.stat().st_size})

    identity = derive_identity_map(events, extra)
    identity_path = out / IDENTITY_NAME
    _write_json(identity_path, identity)
    file_entries.append({"path": IDENTITY_NAME, "role": "identity-mapping",
                         "source": "generated", "sha256": _sha(identity_path),
                         "bytes": identity_path.stat().st_size})

    discrepancies = classify_discrepancies(payload)
    discrepancy_path = out / DISCREPANCIES_NAME
    _write_json(discrepancy_path, discrepancies)
    file_entries.append({"path": DISCREPANCIES_NAME,
                         "role": "discrepancy-classification",
                         "source": "generated", "sha256": _sha(discrepancy_path),
                         "bytes": discrepancy_path.stat().st_size})

    replay_paths = [entry["path"] for entry in file_entries
                    if entry["role"] == "events"]
    reproduce_command = (
        "python3 -m buyer_ledger usage replay %s --period %s --rule-map %s"
        % (" ".join("--input " + path for path in replay_paths),
           payload["period"]["from"][:7], [e["path"] for e in file_entries
                                           if e["role"] == "rule-map"][0]))
    manifest = {
        "schema": SCHEMA,
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "generated_at": _now_iso(generated_at),
        "sharing": {
            "data_classification": classification,
            "approved_by": sharing_approver or None,
            "public_by_default": False,
        },
        "period": payload["period"],
        "closed_at": payload.get("closed_at"),
        "previous_revision": previous_revision or None,
        "input_fingerprint": payload["input_fingerprint"],
        "revision_id": payload["revision"]["revision_id"],
        "rule_map": {
            "file": [e["path"] for e in file_entries if e["role"] == "rule-map"][0],
            "hash": payload["rule_map"]["hash"],
            "approved": payload["rule_map"]["approved"],
            "approved_by": payload["rule_map"].get("approved_by"),
            "approved_at": payload["rule_map"].get("approved_at"),
            "rule_map_version": _text(rule_map.get("rule_map_version")) or None,
            "rule_versions": [
                {"rule_id": _text(rule.get("rule_id")),
                 "metric_code": _text(rule.get("metric_code")),
                 "price_version": _text(rule.get("price_version")) or None,
                 "valid_from": _text(rule.get("valid_from")) or None,
                 "valid_to": _text(rule.get("valid_to")) or None}
                for rule in rule_map.get("rules") or []],
        },
        "replay_counts": payload["counts"],
        "replay_metrics": payload["metrics"],
        "replay_integrity": "PASS",
        "charge_verdict": "PASS" if payload.get("aggregation_state") == "OK" else "INDETERMINATE",
        "identity": {"file": IDENTITY_NAME,
                     "unmatched_count": identity["unmatched_count"],
                     "dangling_count": identity["dangling_count"],
                     "complete_for_attempt": identity["coverage"]["complete_for_attempt"]},
        "discrepancies": {"file": DISCREPANCIES_NAME,
                          "counts": discrepancies["counts"]},
        "files": file_entries,
        "reproduce_command": reproduce_command,
        "verify_command": "python3 -m buyer_ledger reproduce verify %s" % out,
        "boundary": payload.get("boundary"),
        "limitations": [
            "重放只覆盖计费口径；交付状态保持 UNKNOWN，不因 replay PASS 变成已验收。",
            "身份映射缺项保持 null；不补齐就无法支撑任务/尝试级结论。",
            "差异分类不是因果归因。",
        ],
    }
    _write_json(out / MANIFEST_NAME, manifest)
    (out / README_NAME).write_text(_render_readme(manifest), encoding="utf-8")
    return {"ok": True, "out": str(out), "manifest": manifest,
            "paths": {**paths, "manifest": str(out / MANIFEST_NAME),
                      "identity": str(identity_path),
                      "discrepancies": str(discrepancy_path)}}


def _render_readme(manifest: Dict[str, Any]) -> str:
    rule_map = manifest["rule_map"]
    counts = manifest["replay_counts"]
    return "\n".join([
        "# 重放验收包（%s）" % manifest["period"]["from"][:7],
        "",
        "本目录用于第三方复核：不依赖供应商后台、不依赖打包机器上的临时文件。",
        "",
        "## 内容",
        "",
        "- `inputs/`：原始用量文件与规则映射副本（重放只读这里）。",
        "- `usage-replay.json` / `usage-replay-lines.csv`：重放结果。",
        "- `identity-mapping.json`：任务/尝试/重试/缓存/供应商记录映射，缺项为 null。",
        "- `discrepancies.json`：排除原因与金额为空原因的分类计数（非因果）。",
        "- `manifest.json`：所有文件的 sha256、输入指纹、revision、计费规则版本。",
        "",
        "## 自行复核",
        "",
        "1. 校验文件未被改动：`python3 -m buyer_ledger reproduce verify .`",
        "2. 手工重跑重放（输出到新目录，避免覆盖本包）：",
        "",
        "   ```bash",
        "   %s --out /tmp/replay-check" % manifest["reproduce_command"],
        "   ```",
        "",
        "3. 对照 `usage-replay.json` 中的 `input_fingerprint` 与 `revision.revision_id`：",
        "",
        "   - `%s`" % manifest["input_fingerprint"],
        "   - `%s`" % manifest["revision_id"],
        "",
        "4. 对照 `identity-mapping.json` 的 `unmatched` 与 `dangling_refs`，确认哪些",
        "   记录还不能支撑任务/尝试级结论。",
        "",
        "## 判定",
        "",
        "- 账期：%s（半开区间）" % manifest["period"]["from"][:7],
        "- 事件：收到 %s / 计入 %s / 重复 %s / 冲突 %s / 排除 %s / 迟到 %s"
        % (counts.get("received"), counts.get("included"), counts.get("duplicates"),
           counts.get("conflicts"), counts.get("excluded"), counts.get("late")),
        "- RuleMap：%s（%s，版本 %s）"
        % (rule_map.get("hash"),
           "已批准" if rule_map.get("approved") else "未批准，金额不可作为计费依据",
           rule_map.get("rule_map_version") or "未标注"),
        "- 身份映射：%d 条可支撑尝试级复核，%d 条缺项，%d 条悬空引用"
        % (manifest["identity"]["complete_for_attempt"],
           manifest["identity"]["unmatched_count"],
           manifest["identity"]["dangling_count"]),
        "- Delivery：UNKNOWN（重放只覆盖计费口径）",
        "",
        "## 边界",
        "",
        "- %s" % (manifest.get("boundary") or ""),
        "- 差异分类不是因果归因；不得把 PASS 写成交付验收通过。",
        "",
    ])


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def _semantic_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    value = dict(payload)
    value.pop("generated_at", None)
    return value


def _safe_bundle_file(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise ReproduceError("包内文件路径不能为空")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ReproduceError("包内文件路径越界：%s" % relative)
    target = (root / candidate).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise ReproduceError("包内文件路径越界：%s" % relative) from exc
    if (root / candidate).is_symlink():
        raise ReproduceError("包内不允许符号链接：%s" % relative)
    return target


def _check_exact_file_set(out: Path, manifest: Dict[str, Any],
                          checks: List[Dict[str, Any]]) -> bool:
    registered = set()
    safe = True
    for entry in manifest.get("files") or []:
        try:
            target = _safe_bundle_file(out, entry.get("path"))
            registered.add(str(target.relative_to(out.resolve())))
        except ReproduceError as exc:
            _check(checks, "files.safe_path", False, str(exc))
            safe = False
    auxiliary = {MANIFEST_NAME, README_NAME, "template.json", "index.html", "public-share.json"}
    expected = registered | auxiliary
    actual = set()
    symlinks = []
    for path in out.rglob("*"):
        if path.is_symlink():
            symlinks.append(str(path.relative_to(out)))
            continue
        if path.is_file():
            actual.add(str(path.relative_to(out)))
    extra = sorted(actual - expected)
    missing = sorted(registered - actual)
    ok = safe and not extra and not missing and not symlinks
    detail = []
    if extra:
        detail.append("未登记文件：" + ", ".join(extra))
    if missing:
        detail.append("登记但缺失：" + ", ".join(missing))
    if symlinks:
        detail.append("符号链接：" + ", ".join(symlinks))
    _check(checks, "files.exact_set", ok, "；".join(detail))
    return ok


def _check(checks: List[Dict[str, Any]], name: str, ok: bool, detail: str = "",
           kind: str = "fail") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail,
                   **({} if ok else {"kind": kind})})


def verify_bundle(path: Path) -> Dict[str, Any]:
    out = Path(path)
    checks: List[Dict[str, Any]] = []
    manifest_path = out / MANIFEST_NAME
    if not manifest_path.is_file():
        _check(checks, "manifest.present", False, "缺少 manifest.json", "incomplete")
        return {"schema": SCHEMA, "verdict": "UNPROVEN", "checks": checks,
                "bundle": str(out)}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _check(checks, "manifest.parse", False, str(exc), "incomplete")
        return {"schema": SCHEMA, "verdict": "UNPROVEN", "checks": checks,
                "bundle": str(out)}
    _check(checks, "manifest.parse", True)
    if manifest.get("schema") != SCHEMA:
        _check(checks, "manifest.schema", False,
               "schema 应为 %s（实际 %r）" % (SCHEMA, manifest.get("schema")),
               "incomplete")
    else:
        _check(checks, "manifest.schema", True)
    missing = [key for key in REQUIRED_MANIFEST_KEYS if key not in manifest]
    _check(checks, "manifest.fields", not missing,
           "缺少字段：%s" % ", ".join(missing) if missing else "", "incomplete")

    file_hashes_ok = _check_exact_file_set(out, manifest, checks)
    if manifest.get("generator_version") != GENERATOR_VERSION:
        _check(checks, "manifest.generator_version", False,
               "生成器/算法版本缺失或不受支持", "incomplete")
        file_hashes_ok = False
    for entry in manifest.get("files") or []:
        rel = entry.get("path") or ""
        try:
            target = _safe_bundle_file(out, rel)
        except ReproduceError as exc:
            _check(checks, "file:%s" % rel, False, str(exc), "incomplete")
            file_hashes_ok = False
            continue
        if not rel or not target.is_file():
            _check(checks, "file:%s" % rel, False, "文件缺失", "incomplete")
            file_hashes_ok = False
            continue
        actual = _sha(target)
        size_ok = target.stat().st_size == entry.get("bytes")
        ok = actual == entry.get("sha256") and size_ok
        _check(checks, "file:%s" % rel, ok,
               "" if ok else "sha256/字节数不一致（文件被改动或损坏）")
        file_hashes_ok = file_hashes_ok and ok
    _check(checks, "files.listed", bool(manifest.get("files")),
           "" if manifest.get("files") else "manifest 未列出任何文件", "incomplete")

    replay_ok = True
    try:
        events: List[Dict[str, Any]] = []
        for entry in manifest.get("files") or []:
            if entry.get("role") == "events":
                events.extend(usage_replay.load_events(out / entry["path"]))
        rule_entry = next((entry for entry in manifest.get("files") or []
                           if entry.get("role") == "rule-map"), None)
        if rule_entry is None or not events:
            _check(checks, "replay.inputs", False, "缺少 events 或 rule-map",
                   "incomplete")
            replay_ok = False
        else:
            rule_map = usage_replay.load_rule_map(out / rule_entry["path"])
            identity_path = next((out / entry["path"]
                                  for entry in manifest.get("files") or []
                                  if entry.get("role") == "identity-map"), None)
            payload = usage_replay.replay(
                events, period=manifest["period"]["from"][:7],
                rule_map=rule_map, closed_at=manifest.get("closed_at") or "",
                previous_revision=manifest.get("previous_revision") or "",
                generated_at=datetime.now(timezone.utc),
                source_files=[Path(entry["path"]) for entry in manifest.get("files") or []
                              if entry.get("role") == "events"])
            _check(checks, "replay.fingerprint",
                   payload["input_fingerprint"] == manifest.get("input_fingerprint"),
                   "重跑得到的 input_fingerprint 与 manifest 不一致")
            _check(checks, "replay.revision",
                   payload["revision"]["revision_id"] == manifest.get("revision_id"),
                   "重跑得到的 revision_id 与 manifest 不一致")
            _check(checks, "replay.counts",
                   payload["counts"] == manifest.get("replay_counts"),
                   "重跑得到的 counts 与 manifest 不一致")
            _check(checks, "replay.metrics",
                   payload["metrics"] == manifest.get("replay_metrics"),
                   "重跑得到的 metrics 与 manifest 不一致")
            semantic_path = out / "usage-replay.json"
            if semantic_path.is_file():
                stored_payload = json.loads(semantic_path.read_text(encoding="utf-8"))
                semantic_same = _canonical(_semantic_payload(stored_payload)) == _canonical(
                    _semantic_payload(payload))
                _check(checks, "replay.semantic_output", semantic_same,
                       "" if semantic_same else "完整重放语义输出与包内结果不一致")
            else:
                _check(checks, "replay.semantic_output", False,
                       "缺少 usage-replay.json", "incomplete")
                semantic_same = False
            replay_ok = (payload["input_fingerprint"] == manifest.get("input_fingerprint")
                         and payload["revision"]["revision_id"] == manifest.get("revision_id")
                         and payload["counts"] == manifest.get("replay_counts")
                         and payload["metrics"] == manifest.get("replay_metrics")
                         and semantic_same)
            identity = derive_identity_map(
                events, _load_identity_map(identity_path) if identity_path else {})
            stored_identity = out / IDENTITY_NAME
            if stored_identity.is_file():
                stored = json.loads(stored_identity.read_text(encoding="utf-8"))
                same = _canonical(stored) == _canonical(identity)
                _check(checks, "identity.match", same,
                       "" if same else "identity-mapping.json 与从输入重算的结果不一致")
                replay_ok = replay_ok and same
            else:
                _check(checks, "identity.match", False, "缺少 identity-mapping.json",
                       "incomplete")
                replay_ok = False
            stored_discrepancies = out / DISCREPANCIES_NAME
            if stored_discrepancies.is_file():
                stored = json.loads(stored_discrepancies.read_text(encoding="utf-8"))
                same = _canonical(stored) == _canonical(classify_discrepancies(payload))
                _check(checks, "discrepancies.match", same,
                       "" if same else "discrepancies.json 与从重放重算的结果不一致")
                replay_ok = replay_ok and same
            else:
                _check(checks, "discrepancies.match", False,
                       "缺少 discrepancies.json", "incomplete")
                replay_ok = False
    except (usage_replay.ReplayError, ValueError, OSError) as exc:
        _check(checks, "replay.inputs", False, str(exc), "incomplete")
        replay_ok = False
    _check(checks, "replay.reproduced", replay_ok,
           "" if replay_ok else "无法从包内输入复现同一重放结果", "incomplete")

    if any(not check["ok"] and check.get("kind") == "incomplete" for check in checks):
        verdict = "UNPROVEN"
    elif any(not check["ok"] for check in checks):
        verdict = "FAIL"
    else:
        verdict = "PASS"
    return {"schema": SCHEMA, "verdict": verdict, "checks": checks,
            "bundle": str(out)}


def _public_html(manifest: Dict[str, Any]) -> str:
    counts = manifest.get("replay_counts") or {}
    sharing = manifest.get("sharing") or {}
    replay_integrity = html.escape(str(manifest.get("replay_integrity") or "UNPROVEN"))
    charge_verdict = html.escape(str(manifest.get("charge_verdict") or "UNPROVEN"))
    rows = "".join([
        "<tr><td>Intake</td><td>包内输入与规则副本</td><td>PASS</td></tr>",
        "<tr><td>Recompute</td><td>重放完整性与输入指纹</td><td>%s</td></tr>" % replay_integrity,
        "<tr><td>Charge review</td><td>费用规则与记录结论（不是交付验收）</td><td>%s</td></tr>" % charge_verdict,
        "<tr><td>Review</td><td>身份缺项与差异分类保持可见</td><td>REVIEW</td></tr>",
        "<tr><td>Decide</td><td>买方验收、争议或续约由人决定</td><td>UNPROVEN</td></tr>",
    ])
    values = {
        "period": html.escape(str((manifest.get("period") or {}).get("from", ""))[:7]),
        "classification": html.escape(str(sharing.get("data_classification") or "")),
        "received": html.escape(str(counts.get("received", 0))),
        "included": html.escape(str(counts.get("included", 0))),
        "unmatched": html.escape(str((manifest.get("identity") or {}).get("unmatched_count", 0))),
        "rows": rows,
    }
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Verovo 重放账期 %(period)s</title>
<style>body{max-width:900px;margin:40px auto;padding:0 20px;font:16px/1.6 system-ui,sans-serif;color:#17231f}table{border-collapse:collapse;width:100%%}th,td{text-align:left;padding:10px;border-bottom:1px solid #ccd5d1}pre{padding:16px;background:#101816;color:#eef7f3;overflow:auto}.note{padding:14px;border-left:4px solid #8b6b23;background:#faf6e9}</style></head>
<body><main><h1>重放账期 %(period)s</h1><p>%(classification)s · 合成或经授权脱敏的公开副本</p>
<p class="note">重放 PASS 只说明包内计费重算一致。Delivery 仍为 UNKNOWN，买方决定不能由工具代签。</p>
<table><thead><tr><th>阶段</th><th>检查</th><th>状态</th></tr></thead><tbody>%(rows)s</tbody></table>
<p>收到 %(received)s 条事件，计入 %(included)s 条；身份映射缺项 %(unmatched)s 条。</p>
<h2>查看与复用</h2><ul><li><a href="manifest.json">重放清单</a></li><li><a href="template.json">新任务模板</a></li><li><a href="public-share.json">发布记录</a></li></ul>
<h2>本地验证</h2><pre>python3 -m buyer_ledger reproduce verify .</pre>
</main></body></html>""" % values


def publish_bundle(path: Path, out_dir: Path, *, approved_by: str) -> Dict[str, Any]:
    publisher = _text(approved_by)
    if not publisher:
        raise ReproduceError("公开重放包需要具名发布人")
    source = Path(path)
    verification = verify_bundle(source)
    if verification["verdict"] != "PASS":
        raise ReproduceError("只有重放 PASS 的包才能公开")
    manifest = json.loads((source / MANIFEST_NAME).read_text(encoding="utf-8"))
    sharing = manifest.get("sharing") or {}
    classification = _text(sharing.get("data_classification")) or "confidential"
    if classification not in ("synthetic", "public-redacted"):
        raise ReproduceError("默认私有：请使用 synthetic 或具名批准的 public-redacted 输入重新打包")
    if classification == "public-redacted" \
            and publisher != _text(sharing.get("approved_by")):
        raise ReproduceError("public-redacted 发布人必须与打包时的脱敏批准人一致")
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise ReproduceError("公开输出目录必须为空")
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=out.name + ".tmp-", dir=str(out.parent)))
    try:
        for entry in manifest.get("files") or []:
            relative = str(entry["path"])
            source_file = _safe_bundle_file(source, relative)
            target = _safe_bundle_file(temp, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, target)
        public_manifest = json.loads(json.dumps(manifest))
        for entry in public_manifest.get("files") or []:
            entry.pop("source", None)
        public_manifest["verify_command"] = "python3 -m buyer_ledger reproduce verify ."
        public_manifest["sharing"] = {
            **(public_manifest.get("sharing") or {}),
            "public_by_default": False,
            "published_by": publisher,
        }
        _write_json(temp / MANIFEST_NAME, public_manifest)
        if (source / README_NAME).is_file():
            shutil.copyfile(source / README_NAME, temp / README_NAME)
        template = {
            "schema": "verovo.reproduce-template/1",
            "period": public_manifest["period"]["from"][:7],
            "build_command": public_manifest["reproduce_command"]
                             + " --out new-period-bundle --data-classification confidential",
            "note": "新任务默认私有。模板复用不会继承本次重放结论或买方验收。",
        }
        _write_json(temp / "template.json", template)
        (temp / "index.html").write_text(_public_html(public_manifest), encoding="utf-8")
        share = {
            "schema": "verovo.public-replay-share/1",
            "published_by": publisher,
            "published_at": _now_iso(),
            "data_classification": classification,
            "verification": "PENDING",
            "source_bytes_included": True,
            "boundary": "包内含已声明为 synthetic 或 public-redacted 的输入；不包含原始路径。公开重放不等于交付验收。",
        }
        _write_json(temp / "public-share.json", share)
        public_verification = verify_bundle(temp)
        if public_verification["verdict"] != "PASS":
            raise ReproduceError("公开副本未能通过重放校验")
        share["verification"] = public_verification["verdict"]
        _write_json(temp / "public-share.json", share)
        if out.exists():
            out.rmdir()
        temp.rename(out)
        return {"out": str(out), "manifest": public_manifest, "template": template,
                "share": share, "verification": public_verification}
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


__all__ = ["SCHEMA", "IDENTITY_SCHEMA", "GENERATOR", "GENERATOR_VERSION",
           "DATA_CLASSIFICATIONS", "ReproduceError", "derive_identity_map",
           "classify_discrepancies", "build_bundle", "verify_bundle", "publish_bundle"]
