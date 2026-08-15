"""Portable media chapter records with explicit local capability degradation."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Protocol, Sequence


HOST_PATH_RE = re.compile(r"(?:^|\s)(?:[A-Za-z]:[\\/]|/)\S+")


def _relative_path(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{field} must be a safe relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field} must be a safe relative path")
    if ":" in path.parts[0]:
        raise ValueError(f"{field} must be a safe relative path")
    return path.as_posix()


@dataclass(frozen=True)
class FrameReference:
    timestamp_ms: int
    path: str

    def __post_init__(self) -> None:
        if self.timestamp_ms < 0:
            raise ValueError("frame timestamp cannot be negative")
        object.__setattr__(self, "path", _relative_path(self.path, field="frame path"))

    def to_dict(self) -> dict[str, object]:
        return {"timestamp_ms": self.timestamp_ms, "path": self.path}


@dataclass(frozen=True)
class MediaChapter:
    chapter_id: str
    title: str
    start_ms: int
    end_ms: int
    section: str = "unclassified"
    question_numbers: tuple[int, ...] = ()
    transcript_path: str | None = None
    frames: tuple[FrameReference, ...] = ()

    def __post_init__(self) -> None:
        if not self.chapter_id or not self.title:
            raise ValueError("chapter id and title are required")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("chapter time range is invalid")
        if any(number < 1 for number in self.question_numbers):
            raise ValueError("question numbers must be positive")
        if self.transcript_path is not None:
            object.__setattr__(
                self,
                "transcript_path",
                _relative_path(self.transcript_path, field="transcript path"),
            )
        object.__setattr__(self, "question_numbers", tuple(self.question_numbers))
        object.__setattr__(self, "frames", tuple(self.frames))

    def to_dict(self) -> dict[str, object]:
        return {
            "chapter_id": self.chapter_id,
            "title": self.title,
            "time_range": {"start_ms": self.start_ms, "end_ms": self.end_ms},
            "section": self.section,
            "question_numbers": list(self.question_numbers),
            "transcript_path": self.transcript_path,
            "frames": [frame.to_dict() for frame in self.frames],
        }


@dataclass(frozen=True)
class MediaLearningResult:
    status: str
    chapters: tuple[MediaChapter, ...] = ()
    message: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"completed", "degraded"}:
            raise ValueError("media result status is unsupported")
        if HOST_PATH_RE.search(self.message):
            raise ValueError("media result message cannot contain a host path")
        object.__setattr__(self, "chapters", tuple(self.chapters))

    @classmethod
    def completed(
        cls, *, chapters: Iterable[MediaChapter] = (), message: str = ""
    ) -> "MediaLearningResult":
        return cls("completed", tuple(chapters), message)

    @classmethod
    def degraded(
        cls, message: str, *, chapters: Iterable[MediaChapter] = ()
    ) -> "MediaLearningResult":
        return cls("degraded", tuple(chapters), message)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": self.status,
            "message": self.message,
            "chapters": [chapter.to_dict() for chapter in self.chapters],
        }


class MediaProvider(Protocol):
    def process(
        self, media_paths: Sequence[Path], output_dir: Path
    ) -> MediaLearningResult: ...


class CommandExecutor:
    """Execute a caller-supplied local command without shell parsing."""

    def run(self, command: Sequence[str]) -> object:
        if isinstance(command, (str, bytes)) or not command:
            raise ValueError("command must be a non-empty argument sequence")
        return subprocess.run(
            tuple(command), check=True, capture_output=True, text=True, shell=False
        )


class UnavailableMediaProvider:
    def __init__(self, message: str = "本地媒体转写能力未配置") -> None:
        self.message = message

    def process(
        self, media_paths: Sequence[Path], output_dir: Path
    ) -> MediaLearningResult:
        return MediaLearningResult.degraded(self.message)


class LocalMediaProvider:
    """Use an injected local transcriber; never installs or downloads models."""

    def __init__(
        self,
        transcriber: Callable[[Path, Path], Iterable[MediaChapter]] | None = None,
        frame_extractor: Callable[
            [Path, Path, Sequence[MediaChapter]], Iterable[MediaChapter]
        ]
        | None = None,
        *,
        command_finder: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._transcriber = transcriber
        self._frame_extractor = frame_extractor
        self._command_finder = command_finder

    def process(
        self, media_paths: Sequence[Path], output_dir: Path
    ) -> MediaLearningResult:
        if not media_paths:
            return MediaLearningResult.completed()
        if self._command_finder("ffmpeg") is None or self._transcriber is None:
            return MediaLearningResult.degraded("本地 ffmpeg 或转写器不可用")
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        chapters: list[MediaChapter] = []
        degraded = self._frame_extractor is None
        try:
            for media_path in media_paths:
                transcribed = tuple(self._transcriber(Path(media_path), destination))
                if self._frame_extractor is None:
                    chapters.extend(transcribed)
                else:
                    chapters.extend(
                        self._frame_extractor(Path(media_path), destination, transcribed)
                    )
        except Exception:
            return MediaLearningResult.degraded(
                "本地媒体处理失败，试卷流程已继续", chapters=chapters
            )
        if degraded:
            return MediaLearningResult.degraded(
                "本地画面提取工具未配置，已保留转写章节", chapters=chapters
            )
        return MediaLearningResult.completed(chapters=chapters)


def write_media_index(result: MediaLearningResult, destination: Path) -> Path:
    """Atomically persist a source-path-free chapter, time and frame index."""
    path = Path(destination)
    if path.is_symlink() or any(
        parent.is_symlink() for parent in path.parents if parent.exists()
    ):
        raise ValueError("media index destination cannot use symlinks")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
