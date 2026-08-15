"""Candidate records and conservative, evidence-based status decisions."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping


STATUSES = frozenset({"candidate", "verified", "stable", "review_required", "deprecated"})
EVIDENCE_KINDS = frozenset({"formal_exam", "formal_answer", "text_inference"})


def normalize_identifier(value: str, *, field_name: str) -> str:
    """Trim an identifier while preserving case and rejecting control characters."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError(f"{field_name} must not contain control characters")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _absolute_path_like(value: str) -> bool:
    text = value.strip()
    return (
        text.startswith("file:")
        or PurePosixPath(text).is_absolute()
        or PureWindowsPath(text).is_absolute()
        or text.startswith("\\\\")
    )


def _path_escapes(value: str) -> bool:
    location = value.split("#", 1)[0].replace("\\", "/")
    return ".." in PurePosixPath(location).parts


@dataclass(frozen=True)
class CandidateKnowledge:
    card_type: str
    title: str
    statement: str
    modules: tuple[str, ...]
    source: Mapping[str, Any]
    risk_level: str = "normal"

    def __post_init__(self) -> None:
        if not all((self.card_type.strip(), self.title.strip(), self.statement.strip())):
            raise ValueError("candidate type, title and statement are required")
        if self.risk_level not in {"normal", "high"}:
            raise ValueError("candidate risk_level must be normal or high")
        if not self.modules or any(not item.strip() for item in self.modules):
            raise ValueError("candidate modules require nonempty values")
        try:
            json.dumps(dict(self.source), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("candidate source must be JSON-safe") from error
        for field in ("id", "kind", "name", "locator"):
            value = self.source.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"candidate source {field} is required")
        source_id = str(self.source["id"])
        if normalize_identifier(source_id, field_name="candidate source id") != source_id:
            raise ValueError("candidate source id must not contain surrounding whitespace")
        locator = self.source.get("locator")
        if isinstance(locator, str) and _absolute_path_like(locator):
            raise ValueError("candidate source must not contain an absolute path")
        if isinstance(locator, str) and _path_escapes(locator):
            raise ValueError("candidate source locator cannot escape the project")

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_type": self.card_type,
            "title": self.title,
            "statement": self.statement,
            "modules": list(self.modules),
            "source": dict(self.source),
            "risk_level": self.risk_level,
        }


@dataclass(frozen=True)
class VerificationCase:
    exam_id: str
    source_id: str
    exam_year: int | None = None
    evidence_kind: str = "formal_exam"
    official_support: bool = False
    conflict: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "exam_id",
            normalize_identifier(self.exam_id, field_name="verification exam_id"),
        )
        object.__setattr__(
            self,
            "source_id",
            normalize_identifier(self.source_id, field_name="verification source_id"),
        )
        if self.exam_year is not None and not 1900 <= self.exam_year <= 9999:
            raise ValueError("verification exam_year is invalid")
        if self.evidence_kind not in EVIDENCE_KINDS:
            raise ValueError("unknown verification evidence_kind")
        if self.official_support and self.evidence_kind != "formal_answer":
            raise ValueError("official_support requires formal_answer evidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "exam_id": self.exam_id,
            "source_id": self.source_id,
            "exam_year": self.exam_year,
            "evidence_kind": self.evidence_kind,
            "official_support": self.official_support,
            "conflict": self.conflict,
        }


@dataclass(frozen=True)
class Conflict:
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("conflict reason is required")


@dataclass(frozen=True)
class StatusDecision:
    status: str
    reason: str
    warnings: tuple[str, ...] = ()


def evaluate_status(
    card_type: str,
    *,
    verification_cases: tuple[VerificationCase, ...] = (),
    risk_level: str = "normal",
    current_status: str = "candidate",
    conflict: Conflict | None = None,
    resolve_review: bool = False,
) -> StatusDecision:
    """Return the highest status supported by independent evidence."""
    if not card_type.strip():
        raise ValueError("card_type is required")
    if current_status not in STATUSES:
        raise ValueError("unknown current status")
    if risk_level not in {"normal", "high"}:
        raise ValueError("risk_level must be normal or high")
    if conflict is not None or any(case.conflict for case in verification_cases):
        reason = (
            conflict.reason
            if conflict is not None
            else "verification case records a conflict"
        )
        return StatusDecision("review_required", reason)
    if current_status == "deprecated":
        return StatusDecision("deprecated", "deprecated knowledge remains as a historical record")
    if current_status == "review_required" and not resolve_review:
        return StatusDecision("review_required", "explicit review resolution is required")

    positive = tuple(
        case for case in verification_cases if case.evidence_kind != "text_inference"
    )
    unique_exams = {case.exam_id for case in positive}
    if current_status == "review_required" and resolve_review and not unique_exams:
        return StatusDecision(
            "review_required", "independent review evidence is required for resolution"
        )
    if not unique_exams:
        return StatusDecision("candidate", "no independent exam verification recorded")
    threshold = 3 if risk_level == "high" else 2
    if len(unique_exams) < threshold:
        return StatusDecision("verified", f"validated by {len(unique_exams)} distinct exam(s)")

    if risk_level == "high":
        unique_sources = {case.source_id for case in positive}
        unique_years = {case.exam_year for case in positive if case.exam_year is not None}
        if max(len(unique_sources), len(unique_years)) < 3:
            return StatusDecision("verified", "高风险方法需要三套不同来源或不同年份的试卷")
        official = any(
            case.evidence_kind == "formal_answer" and case.official_support for case in positive
        )
        warnings = (
            ()
            if official
            else ("high-risk stable knowledge should prefer official answer support",)
        )
        return StatusDecision("stable", "validated by three diverse exams", warnings)
    return StatusDecision("stable", f"validated by {len(unique_exams)} distinct exams")
