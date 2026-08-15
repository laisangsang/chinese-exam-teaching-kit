"""Attach later reference answers without rerunning optional media work."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from .intake import archive_inputs
from .models import PipelineTask, StageRecord
from .runner import (
    DEPENDENT_ON_ANSWERS,
    _atomic_json,
    safe_task_path,
    task_lock,
)
from .state import load_task, save_task, sha256_file


@dataclass(frozen=True)
class AnswerAttachment:
    changed: bool
    added_sha256: tuple[str, ...]
    version_count: int


def attach_reference_answers(
    task_path: Path, answer_paths: Sequence[Path]
) -> AnswerAttachment:
    """Copy and deduplicate new answers, retaining a path-safe revision ledger."""
    if not answer_paths:
        raise ValueError("at least one reference answer is required")
    raw = Path(task_path)
    absolute = raw if raw.is_absolute() else Path.cwd() / raw
    if len(absolute.parts) < 5:
        raise ValueError("task path must match .local/tasks/SLUG/task.json")
    project_root = absolute.parent.parent.parent.parent
    safe_path = safe_task_path(project_root, absolute)
    with task_lock(safe_path.parent):
        task = load_task(safe_path)
        known = {record.sha256 for record in task.materials}
        archived = archive_inputs(task, tuple(Path(path) for path in answer_paths))
        added = tuple(record.sha256 for record in archived.materials if record.sha256 not in known)
        if not added:
            ledger = _load_ledger(task.workspace / "answers" / "differences.json")
            return AnswerAttachment(False, (), len(ledger["versions"]))

        added_set = set(added)
        materials = tuple(
            replace(record, material_type="answer_candidate")
            if record.sha256 in added_set
            else record
            for record in archived.materials
        )
        updated = replace(archived, materials=materials)
        stages = dict(updated.stages)
        for name in DEPENDENT_ON_ANSWERS:
            record = stages[name]
            stages[name] = StageRecord(
                status="pending",
                attempts=record.attempts,
                events=record.events
                + (
                    {
                        "event": "invalidated",
                        "reason": "new reference answer attached",
                    },
                ),
            )
        updated = replace(
            updated,
            stages=stages,
            events=updated.events
            + (
                {
                    "event": "reference_answers_attached",
                    "sha256": list(added),
                },
            ),
        )

        ledger_path = task.workspace / "answers" / "differences.json"
        ledger = _load_ledger(ledger_path)
        previous = tuple(
            version["sha256"] for version in ledger["versions"] if "sha256" in version
        )
        for digest in added:
            snapshot_path = _snapshot_previous_outputs(task, digest)
            if digest in previous:
                continue
            record = next(item for item in materials if item.sha256 == digest)
            ledger["versions"].append(
                {
                    "version": len(ledger["versions"]) + 1,
                    "sha256": digest,
                    "archived_path": record.archived_path,
                    "snapshot_manifest": snapshot_path,
                    "comparison": {
                        "previous_sha256": previous[-1] if previous else None,
                        "current_sha256": digest,
                        "status": "content_changed" if previous else "initial_reference_answer",
                    },
                }
            )
            previous += (digest,)
        _atomic_json(ledger_path, ledger)
        save_task(updated)
        return AnswerAttachment(True, added, len(ledger["versions"]))


def _load_ledger(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"schema_version": 1, "versions": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("answer difference ledger is unreadable") from None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("versions"), list)
    ):
        raise ValueError("answer difference ledger is invalid")
    return payload


def _snapshot_previous_outputs(task: PipelineTask, answer_digest: str) -> str:
    revision = task.workspace / "answers" / "revisions" / answer_digest[:16]
    if revision.is_symlink():
        raise ValueError("answer revision directory cannot be a symlink")
    candidates: list[tuple[Path, Path]] = []
    for root_name in ("content", "output"):
        root = task.workspace / root_name
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise ValueError("previous artifact directory is unsafe")
        for source in sorted(root.rglob("*"), key=lambda path: path.as_posix()):
            if source.is_symlink():
                raise ValueError("previous artifact tree cannot contain symlinks")
            if not source.is_file():
                continue
            relative = source.relative_to(task.workspace)
            destination = revision / relative
            candidates.append((source, destination))
    artifacts = []
    for source, destination in candidates:
        _atomic_bytes(destination, source.read_bytes())
        artifacts.append(
            {
                "path": destination.relative_to(revision).as_posix(),
                "sha256": sha256_file(destination),
            }
        )
    manifest = revision / "snapshot.json"
    _atomic_json(
        manifest,
        {
            "schema_version": 1,
            "answer_sha256": answer_digest,
            "artifacts": artifacts,
        },
    )
    return manifest.relative_to(task.workspace).as_posix()


def _atomic_bytes(destination: Path, contents: bytes) -> Path:
    path = Path(destination)
    if path.is_symlink():
        raise ValueError("snapshot destination cannot be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("snapshot directory cannot be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
