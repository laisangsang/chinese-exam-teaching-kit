"""Immutable, JSON-safe records for the public resumable pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping


SCHEMA_VERSION = 1
STAGES = (
    "intake",
    "extract",
    "media",
    "knowledge_pre",
    "analysis",
    "delivery",
    "knowledge_post",
    "verification",
)
STATUSES = frozenset({"pending", "running", "completed", "degraded", "failed"})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _task_relative_inputs_path(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{field_name} must be a task-relative inputs path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or not path.parts
        or path.parts[0] != "inputs"
        or len(path.parts) < 2
    ):
        raise ValueError(f"{field_name} must be a task-relative inputs path")
    return path.as_posix()


def _validate_workspace(workspace: Path) -> Path:
    path = Path(workspace)
    if ".." in path.parts or path.parent.name != "tasks" or path.parent.parent.name != ".local":
        raise ValueError("task workspace must be under .local/tasks")
    return path


@dataclass(frozen=True)
class MaterialRecord:
    """One archived input without any original filename or source path."""

    archived_path: str
    sha256: str
    material_type: str
    size_bytes: int | None = None
    processing_status: str = "copied"
    duplicate_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "archived_path",
            _task_relative_inputs_path(self.archived_path, field_name="archived_path"),
        )
        if not isinstance(self.sha256, str) or len(self.sha256) != 64:
            raise ValueError("sha256 must be a 64-character digest")
        try:
            int(self.sha256, 16)
        except ValueError as error:
            raise ValueError("sha256 must be a hexadecimal digest") from error
        if not isinstance(self.material_type, str) or not self.material_type:
            raise ValueError("material_type is required")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")
        duplicates = tuple(
            _task_relative_inputs_path(path, field_name="duplicate_sources")
            for path in self.duplicate_sources
        )
        object.__setattr__(self, "duplicate_sources", duplicates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "archived_path": self.archived_path,
            "sha256": self.sha256,
            "material_type": self.material_type,
            "size_bytes": self.size_bytes,
            "processing_status": self.processing_status,
            "duplicate_sources": list(self.duplicate_sources),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MaterialRecord":
        if not isinstance(data, Mapping):
            raise ValueError("material record must be an object")
        return cls(
            archived_path=str(data["archived_path"]),
            sha256=str(data["sha256"]),
            material_type=str(data["material_type"]),
            size_bytes=data.get("size_bytes"),
            processing_status=str(data.get("processing_status", "copied")),
            duplicate_sources=tuple(data.get("duplicate_sources", ())),
        )


@dataclass(frozen=True)
class StageRecord:
    """Current state and immutable transition history for one stage."""

    status: str = "pending"
    attempts: int = 0
    events: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown stage status: {self.status}")
        if self.attempts < 0:
            raise ValueError("stage attempts cannot be negative")
        object.__setattr__(self, "events", tuple(_freeze(event) for event in self.events))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "attempts": self.attempts,
            "events": _thaw(self.events),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StageRecord":
        if not isinstance(data, Mapping):
            raise ValueError("stage record must be an object")
        return cls(
            status=str(data["status"]),
            attempts=int(data.get("attempts", 0)),
            events=tuple(data.get("events", ())),
        )


@dataclass(frozen=True)
class PipelineTask:
    """Complete durable state for one local exam-processing task."""

    task_id: str
    title: str
    workspace: Path
    materials: tuple[MaterialRecord, ...] = ()
    stages: Mapping[str, StageRecord] = field(default_factory=dict)
    events: tuple[Mapping[str, Any], ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.task_id or not self.title:
            raise ValueError("task_id and title are required")
        object.__setattr__(self, "workspace", _validate_workspace(self.workspace))
        object.__setattr__(self, "materials", tuple(self.materials))
        object.__setattr__(self, "stages", MappingProxyType(dict(self.stages)))
        object.__setattr__(self, "events", tuple(_freeze(event) for event in self.events))

    @classmethod
    def create(cls, task_id: str, title: str, workspace: Path) -> "PipelineTask":
        return cls(
            task_id=task_id,
            title=title,
            workspace=workspace,
            stages={stage: StageRecord() for stage in STAGES},
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a payload containing only task-local paths."""
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "title": self.title,
            "materials": [material.to_dict() for material in self.materials],
            "stages": {name: record.to_dict() for name, record in self.stages.items()},
            "events": _thaw(self.events),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, workspace: Path) -> "PipelineTask":
        if not isinstance(data, Mapping):
            raise ValueError("task JSON must be an object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("task schema_version is unsupported")
        stages = data.get("stages")
        if not isinstance(stages, Mapping) or set(stages) != set(STAGES):
            raise ValueError("task stages do not match the public pipeline")
        return cls(
            task_id=str(data["task_id"]),
            title=str(data["title"]),
            workspace=workspace,
            materials=tuple(MaterialRecord.from_dict(item) for item in data.get("materials", ())),
            stages={stage: StageRecord.from_dict(stages[stage]) for stage in STAGES},
            events=tuple(data.get("events", ())),
            schema_version=SCHEMA_VERSION,
        )
