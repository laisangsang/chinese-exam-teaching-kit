"""Classify and privately archive user inputs for a local task."""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Literal, Sequence

from .models import MaterialRecord, PipelineTask
from .state import sha256_file


DOCUMENT_SUFFIXES = frozenset({".md", ".txt", ".pdf", ".docx"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"})
AUDIO_SUFFIXES = frozenset({".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"})


def classify_material(path: Path) -> str:
    """Classify a supported local input without contacting an external service."""
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"材料文件不存在: {source}")
    suffix = source.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix not in DOCUMENT_SUFFIXES:
        raise ValueError(f"不支持的材料类型: {suffix or source.name}")
    if suffix != ".pdf":
        return "document"

    text = _first_two_pdf_pages_text(source)
    if any(keyword in text for keyword in ("参考答案", "答案", "评分标准", "评分参考")):
        return "answer_candidate"
    if any(keyword in text for keyword in ("试卷", "试题", "考试", "题号", "选择题")):
        return "exam_candidate"
    return "document_unknown"


def archive_inputs(task: PipelineTask, paths: Sequence[Path]) -> PipelineTask:
    """Copy supported files into the task, deduplicating them by SHA-256."""
    archive_dir = task.workspace / "inputs"
    _ensure_archive_containment(task.workspace, archive_dir)
    prepared: list[tuple[Path, str]] = []
    for raw_path in paths:
        source = Path(raw_path)
        prepared.append((source, classify_material(source)))

    materials = list(task.materials)
    indexes = {record.sha256: index for index, record in enumerate(materials)}
    created_destinations: list[Path] = []
    with _archive_lock(archive_dir):
        try:
            for source, material_type in prepared:
                staged, digest, size_bytes = _stage_source(source, archive_dir)
                try:
                    existing_index = indexes.get(digest)
                    if existing_index is not None:
                        record = materials[existing_index]
                        destination = task.workspace / record.archived_path
                        _publish_staged_copy(staged, destination, digest)
                        duplicates = tuple(
                            dict.fromkeys(record.duplicate_sources + (record.archived_path,))
                        )
                        materials[existing_index] = replace(
                            record,
                            duplicate_sources=duplicates,
                        )
                        continue

                    relative_path = f"inputs/material-{digest}{source.suffix.lower()}"
                    destination = task.workspace / relative_path
                    publication = _publish_staged_copy(staged, destination, digest)
                    if publication == "created":
                        created_destinations.append(destination)
                    materials.append(
                        MaterialRecord(
                            archived_path=relative_path,
                            sha256=digest,
                            material_type=material_type,
                            size_bytes=size_bytes,
                        )
                    )
                    indexes[digest] = len(materials) - 1
                finally:
                    staged.unlink(missing_ok=True)
        except Exception:
            for destination in created_destinations:
                if destination.is_file():
                    destination.unlink()
            raise
    return replace(task, materials=tuple(materials))


def _stage_source(source: Path, archive_dir: Path) -> tuple[Path, str, int]:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".material-",
        suffix=".partial",
        dir=archive_dir,
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with os.fdopen(descriptor, "wb") as archived, source.open("rb") as original:
            for chunk in iter(lambda: original.read(1024 * 1024), b""):
                archived.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
            archived.flush()
            os.fsync(archived.fileno())
        hexdigest = digest.hexdigest()
        if sha256_file(temporary) != hexdigest:
            raise OSError("staged archive failed SHA-256 verification")
        return temporary, hexdigest, size_bytes
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _publish_staged_copy(
    staged: Path,
    destination: Path,
    digest: str,
) -> Literal["created", "repaired", "reused"]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError(f"archive destination cannot be a symlink: {destination}")
    if not destination.resolve().is_relative_to(destination.parent.resolve()):
        raise ValueError(f"archive destination escapes its parent: {destination}")

    replacing_corrupt = False
    if destination.exists():
        if not destination.is_file():
            raise FileExistsError(f"归档目标已存在且不是普通文件: {destination}")
        if sha256_file(destination) == digest:
            return "reused"
        destination.unlink()
        replacing_corrupt = True

    try:
        os.link(staged, destination)
    except FileExistsError:
        if destination.is_file() and not destination.is_symlink() and sha256_file(destination) == digest:
            return "reused"
        raise FileExistsError(f"归档目标被并发占用且内容不同: {destination}") from None
    return "repaired" if replacing_corrupt else "created"


def _ensure_archive_containment(workspace: Path, archive_dir: Path) -> None:
    tasks_root = workspace.parent
    local_root = tasks_root.parent
    project_root = local_root.parent
    managed_paths = (local_root, tasks_root, workspace, archive_dir)
    if any(path.is_symlink() for path in managed_paths):
        raise ValueError("archive path symlink would escape the managed task area")
    if (
        not tasks_root.resolve().is_relative_to(project_root.resolve())
        or not workspace.resolve().is_relative_to(tasks_root.resolve())
        or not archive_dir.resolve().is_relative_to(workspace.resolve())
    ):
        raise ValueError("archive path escapes .local/tasks")


@contextmanager
def _archive_lock(
    archive_dir: Path,
    *,
    platform_name: str | None = None,
    windows_api: Any | None = None,
) -> Iterator[None]:
    """Hold a process-level archive lock on POSIX and Windows."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    lock_path = archive_dir / ".archive-inputs.lock"
    if lock_path.is_symlink():
        raise ValueError("archive lock cannot be a symlink")
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("archive lock cannot be a symlink") from error
        raise
    with os.fdopen(descriptor, "a+b") as lock_file:
        if (platform_name or os.name) == "nt":
            if windows_api is None:
                import msvcrt

                windows_api = msvcrt
            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            windows_api.locking(lock_file.fileno(), windows_api.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                windows_api.locking(lock_file.fileno(), windows_api.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _first_two_pdf_pages_text(path: Path) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:2])
    except Exception:
        return ""
