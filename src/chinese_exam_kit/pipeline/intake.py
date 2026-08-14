"""Classify and privately archive user inputs for a local task."""

from __future__ import annotations

import errno
import os
import shutil
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator, Sequence

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
    copied: list[Path] = []
    with _archive_lock(archive_dir):
        try:
            for source, material_type in prepared:
                digest = sha256_file(source)
                existing_index = indexes.get(digest)
                if existing_index is not None:
                    record = materials[existing_index]
                    materials[existing_index] = replace(
                        record,
                        duplicate_sources=record.duplicate_sources + (record.archived_path,),
                    )
                    continue

                relative_path = f"inputs/material-{digest}{source.suffix.lower()}"
                destination = task.workspace / relative_path
                if _copy_without_overwrite(source, destination, digest):
                    copied.append(destination)
                materials.append(
                    MaterialRecord(
                        archived_path=relative_path,
                        sha256=digest,
                        material_type=material_type,
                        size_bytes=source.stat().st_size,
                    )
                )
                indexes[digest] = len(materials) - 1
        except Exception:
            for destination in copied:
                if destination.is_file():
                    destination.unlink()
            raise
    return replace(task, materials=tuple(materials))


def _copy_without_overwrite(source: Path, destination: Path, digest: str) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError(f"archive destination cannot be a symlink: {destination}")
    if not destination.resolve().is_relative_to(destination.parent.resolve()):
        raise ValueError(f"archive destination escapes its parent: {destination}")
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not destination.is_file() or sha256_file(destination) != digest:
            raise FileExistsError(f"归档目标已存在且内容不同，拒绝覆盖: {destination}")
        return False
    try:
        with source.open("rb") as original, os.fdopen(descriptor, "wb") as archived:
            shutil.copyfileobj(original, archived)
    except Exception:
        if destination.is_file():
            destination.unlink()
        raise
    return True


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
def _archive_lock(archive_dir: Path) -> Iterator[None]:
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
        if os.name == "nt":
            import msvcrt

            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
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
