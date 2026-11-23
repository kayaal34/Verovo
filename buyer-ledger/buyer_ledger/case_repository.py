from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .close import CloseError

STATE_PATHS = {
    "job": Path("period-job.json"),
    "scan": Path("intake/scan.json"),
    "draft": Path("intake/mapping-draft.json"),
    "confirmation": Path("intake/mapping-confirmed.json"),
    "canonical_manifest": Path("canonical/manifest.json"),
    "calculation": Path("calculation.json"),
    "report": Path("report/report.json"),
    "review": Path("review.json"),
    "signoff": Path("signoff.json"),
    "revision": Path("revision.json"),
}


class CaseRepository:
    def __init__(self, case_dir: Any):
        self.case_dir = Path(case_dir)
        self._snapshot = None

    def path(self, name: str) -> Path:
        try:
            relative = STATE_PATHS[name]
        except KeyError as exc:
            raise ValueError("unknown close state artifact: %r" % name) from exc
        return self.case_dir / relative

    def exists(self, name: str) -> bool:
        return self.path(name).is_file()

    def _read_snapshot(self) -> Dict[str, bytes]:
        return {name: self.path(name).read_bytes() for name in STATE_PATHS
                if self.path(name).is_file()}

    def snapshot(self) -> None:
        self._snapshot = self._read_snapshot()

    def assert_snapshot_current(self) -> None:
        if self._snapshot is not None and self._snapshot != self._read_snapshot():
            raise CloseError(
                "STALE_SOURCE",
                "close state changed while the case was being projected; retry the projection",
            )

    def read_json(self, name: str, *, missing_ok: bool = True) -> Optional[Dict[str, Any]]:
        path = self.path(name)
        raw = self._snapshot.get(name) if self._snapshot is not None else None
        if raw is None and path.is_file():
            raw = path.read_bytes()
        if raw is None:
            if missing_ok:
                return None
            raise CloseError("INPUT_REQUIRED", "%s not found: %s" % (name, path))
        try:
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CloseError(
                "ARTIFACT_PARTIAL",
                "%s exists but cannot be read as JSON: %s" % (path, exc),
            ) from exc
        if not isinstance(value, dict):
            raise CloseError("ARTIFACT_PARTIAL", "%s must contain a JSON object" % path)
        return value

    def state_files(self) -> Tuple[Path, ...]:
        return tuple(self.path(name) for name in STATE_PATHS)

    def atomic_write_bytes(self, target: Any, data: bytes) -> Path:
        path = Path(target)
        if not path.is_absolute():
            path = self.case_dir / path
        resolved = path.resolve()
        try:
            resolved.relative_to(self.case_dir.resolve())
        except ValueError as exc:
            raise CloseError("INPUT_REQUIRED", "write path escapes the close case: %s" % path) from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = bytes(data)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="wb", prefix=path.name + ".tmp-", dir=str(path.parent), delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(path))
            temporary = None
        except OSError as exc:
            raise CloseError("ARTIFACT_PARTIAL", "atomic write failed for %s: %s" % (path, exc)) from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
        return path
