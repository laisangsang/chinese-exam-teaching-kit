"""Portable interfaces for document text, rendering, and OCR providers."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence


class CapabilityUnavailable(RuntimeError):
    """Raised only when a requested optional local capability is unavailable."""


class TextReader(Protocol):
    def __call__(self, source: Path) -> Sequence[str]: ...


class Renderer(Protocol):
    def __call__(self, source: Path, output_dir: Path) -> Sequence[Path]: ...


class OcrProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def recognize(self, image: Path) -> str: ...


class UnavailableOcr:
    """Explicit placeholder used when no OCR implementation can run locally."""

    name = "unavailable"

    def __init__(self, message: str) -> None:
        self.message = message

    def available(self) -> bool:
        return False

    def recognize(self, image: Path) -> str:
        raise CapabilityUnavailable(self.message)
