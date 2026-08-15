"""Atomic persistence and legal transitions for resumable tasks."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from .models import PipelineTask, STAGES, StageRecord


ALLOWED_STAGE_TRANSITIONS = {
    "pending": frozenset({"running", "waiting"}),
    "running": frozenset({"waiting", "completed", "degraded", "failed"}),
    "waiting": frozenset({"running"}),
    "failed": frozenset({"running"}),
    "degraded": frozenset({"running", "completed"}),
    "completed": frozenset({"running"}),
}


def sha256_file(path: Path) -> str:
    """Hash a file in bounded chunks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_task(task: PipelineTask) -> Path:
    """Atomically persist ``task.json`` without serializing its host path."""
    _ensure_task_workspace(task.workspace)
    destination = task.workspace / "task.json"
    task.workspace.mkdir(parents=True, exist_ok=True)
    _ensure_task_workspace(task.workspace)
    descriptor, partial_name = tempfile.mkstemp(
        prefix=".task-",
        suffix=".partial",
        dir=task.workspace,
    )
    partial = Path(partial_name)
    try:
        payload = json.dumps(task.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with os.fdopen(descriptor, "w", encoding="utf-8") as partial_file:
            partial_file.write(payload)
            partial_file.flush()
            os.fsync(partial_file.fileno())
        os.replace(partial, destination)
    except Exception:
        if partial.exists():
            partial.unlink()
        raise
    return destination


def load_task(path: Path) -> PipelineTask:
    """Load task state and derive the non-serialized workspace from its file."""
    task_path = Path(path)
    if task_path.name != "task.json":
        raise ValueError("task state path must end with task.json")
    data = json.loads(task_path.read_text(encoding="utf-8"))
    return PipelineTask.from_dict(data, workspace=task_path.parent)


def transition_stage(task: PipelineTask, stage: str, status: str) -> PipelineTask:
    """Return a new task with one legal stage transition recorded."""
    record = _stage(task, stage)
    if status not in ALLOWED_STAGE_TRANSITIONS.get(record.status, frozenset()):
        raise ValueError(f"Illegal stage transition: {record.status} -> {status}")

    attempts = record.attempts + (1 if status == "running" else 0)
    updated = replace(
        record,
        status=status,
        attempts=attempts,
        events=record.events + ({"event": "transition", "from": record.status, "to": status},),
    )
    stages = dict(task.stages)
    stages[stage] = updated
    changed = replace(task, stages=stages)
    if status == "failed":
        return _invalidate_downstream(changed, stage)
    return changed


def _invalidate_downstream(task: PipelineTask, stage: str) -> PipelineTask:
    start = STAGES.index(stage) + 1
    stages = dict(task.stages)
    for name in STAGES[start:]:
        record = stages[name]
        stages[name] = replace(
            record,
            status="pending",
            events=record.events + ({"event": "invalidated", "reason": "upstream stage failed"},),
        )
    return replace(
        task,
        stages=stages,
        events=task.events
        + ({"event": "downstream_invalidated", "stage": stage, "reason": "upstream stage failed"},),
    )


def _stage(task: PipelineTask, stage: str) -> StageRecord:
    try:
        return task.stages[stage]
    except KeyError as error:
        raise ValueError(f"Unknown pipeline stage: {stage}") from error


def _ensure_task_workspace(workspace: Path) -> None:
    tasks_root = workspace.parent
    local_root = tasks_root.parent
    project_root = local_root.parent
    if any(path.is_symlink() for path in (local_root, tasks_root, workspace)):
        raise ValueError("task workspace symlink would escape the managed task area")
    if (
        not tasks_root.resolve().is_relative_to(project_root.resolve())
        or not workspace.resolve().is_relative_to(tasks_root.resolve())
    ):
        raise ValueError("task workspace escapes .local/tasks")
