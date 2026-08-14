"""Per-question records and durable, privacy-safe audit events."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


_FILE_URI = re.compile(r"(?i)file:[/\\]")
_WINDOWS_DRIVE_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]")
_UNC_PATH = re.compile(r"(?<!\\)\\\\[^\\\r\n]+\\")
_POSIX_PATH = re.compile(r"(?<![:/A-Za-z0-9])/(?:[^/\r\n]+/)+")


@dataclass(frozen=True)
class QuestionKnowledge:
    question_id: str
    module: str
    question_type: str
    abilities: tuple[str, ...]
    task_statement: str
    evidence_anchor: str
    answer_boundary: str
    retrieval_queries: tuple[str, ...]

    def __post_init__(self) -> None:
        text_fields = (
            self.question_id,
            self.module,
            self.question_type,
            self.task_statement,
            self.evidence_anchor,
            self.answer_boundary,
        )
        if any(not value.strip() for value in text_fields):
            raise ValueError("question knowledge fields must be nonempty")
        if not self.abilities or not self.retrieval_queries:
            raise ValueError("question abilities and retrieval_queries are required")


def _absolute_path_like(value: str) -> bool:
    stripped = value.strip()
    return bool(
        PurePosixPath(stripped).is_absolute()
        or PureWindowsPath(stripped).is_absolute()
        or _FILE_URI.search(value)
        or _WINDOWS_DRIVE_PATH.search(value)
        or _UNC_PATH.search(value)
        or _POSIX_PATH.search(value)
    )


def _validate_json_safe(value: Any) -> None:
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("audit values must be JSON-safe")
        return
    if isinstance(value, str):
        if _absolute_path_like(value):
            raise ValueError("audit values must not contain an absolute path")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_safe(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key, item in value.items():
            _validate_json_safe(key)
            _validate_json_safe(item)
        return
    raise ValueError("audit values must be JSON-safe")


def _audit_path(root: Path, task_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", task_id):
        raise ValueError("task_id must be a safe filename component")
    return Path(root) / "audit" / f"{task_id}.jsonl"


def _acquire_lock(path: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for audit lock")
            time.sleep(0.01)


def append_audit_event(
    root: Path,
    *,
    task_id: str,
    stage: str,
    event: str,
    card_id: str | None = None,
    applicability: str | None = None,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> Path:
    """Atomically append one JSONL event without serializing machine-local paths."""
    if stage not in {"pre", "during", "post"}:
        raise ValueError("audit stage must be pre, during or post")
    if event not in {"search", "use", "review", "candidate", "status_change"}:
        raise ValueError("unknown audit event")
    if applicability not in {None, "applicable", "not_applicable", "review_required"}:
        raise ValueError("unknown knowledge applicability")
    if details is not None and not isinstance(details, dict):
        raise ValueError("audit details must be an object")
    record = {
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "stage": stage,
        "event": event,
        "card_id": card_id,
        "applicability": applicability,
        "reason": reason,
        "details": details or {},
    }
    _validate_json_safe(record)
    serialized = json.dumps(
        record, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
    ).encode("utf-8") + b"\n"

    path = _audit_path(Path(root), task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_descriptor = _acquire_lock(lock_path)
    temporary: Path | None = None
    try:
        existing = path.read_bytes() if path.exists() else b""
        for line in existing.splitlines():
            json.loads(line)
        if existing and not existing.endswith(b"\n"):
            existing += b"\n"
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(existing)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        os.close(lock_descriptor)
        lock_path.unlink(missing_ok=True)
    return path
