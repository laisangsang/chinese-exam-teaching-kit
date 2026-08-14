"""Portable public document extraction APIs."""

from .documents import (
    ExtractionResult,
    PageText,
    QuestionCandidate,
    build_question_candidates,
    extract_document,
    write_extraction_artifacts,
)
from .macos import MacOSVisionOcr
from .providers import (
    CapabilityUnavailable,
    OcrProvider,
    Renderer,
    TextReader,
    UnavailableOcr,
)

__all__ = [
    "CapabilityUnavailable",
    "ExtractionResult",
    "MacOSVisionOcr",
    "OcrProvider",
    "PageText",
    "QuestionCandidate",
    "Renderer",
    "TextReader",
    "UnavailableOcr",
    "build_question_candidates",
    "extract_document",
    "write_extraction_artifacts",
]
