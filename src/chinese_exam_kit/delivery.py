"""Path-safe delivery manifests with an explicit human visual-review gate."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


VISUAL_STATUSES = frozenset({"not_run", "evidence_ready", "passed", "failed"})


def _relative_paths(values: Iterable[str], *, field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field} paths must be a non-string iterable")
    normalized: set[str] = set()
    for raw in values:
        if not isinstance(raw, str) or not raw or "\\" in raw:
            raise ValueError(f"{field} paths must be non-empty project-relative paths")
        path = PurePosixPath(raw)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError(f"{field} paths must be project-relative")
        if ":" in path.parts[0]:
            raise ValueError(f"{field} paths must be project-relative")
        normalized.add(path.as_posix())
    return tuple(sorted(normalized))


def _valid_review_time(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


@dataclass(frozen=True, init=False)
class DeliveryManifest:
    """Deterministic delivery metadata; automatic code cannot claim ``passed``."""

    outputs: tuple[str, ...]
    visual_status: str
    evidence: tuple[str, ...]
    reviewed_by: str | None
    reviewed_at: str | None
    schema_version: int

    def __init__(
        self,
        outputs: Iterable[str],
        visual_status: str = "not_run",
        evidence: Iterable[str] = (),
        reviewed_by: str | None = None,
        reviewed_at: str | None = None,
    ) -> None:
        if visual_status == "passed":
            raise ValueError("passed requires the explicit manual review API")
        self._initialize(
            outputs=outputs,
            visual_status=visual_status,
            evidence=evidence,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
            allow_passed=False,
        )

    def _initialize(
        self,
        *,
        outputs: Iterable[str],
        visual_status: str,
        evidence: Iterable[str],
        reviewed_by: str | None,
        reviewed_at: str | None,
        allow_passed: bool,
    ) -> None:
        if visual_status not in VISUAL_STATUSES:
            raise ValueError("visual_status is unsupported")
        safe_outputs = _relative_paths(outputs, field="output")
        safe_evidence = _relative_paths(evidence, field="evidence")
        if not safe_outputs:
            raise ValueError("delivery manifest requires at least one output")
        if visual_status == "evidence_ready" and not safe_evidence:
            raise ValueError("evidence_ready requires visual evidence")
        if visual_status == "passed":
            if not allow_passed:
                raise ValueError("passed requires the explicit manual review API")
            if not reviewed_by or not reviewed_by.strip() or not reviewed_at or not safe_evidence:
                raise ValueError("passed requires a complete review record")
            if not _valid_review_time(reviewed_at):
                raise ValueError("passed requires a timezone-aware review record")
        elif reviewed_by is not None or reviewed_at is not None:
            raise ValueError("review fields are only valid after manual review")
        object.__setattr__(self, "outputs", safe_outputs)
        object.__setattr__(self, "visual_status", visual_status)
        object.__setattr__(self, "evidence", safe_evidence)
        object.__setattr__(self, "reviewed_by", reviewed_by.strip() if reviewed_by else None)
        object.__setattr__(self, "reviewed_at", reviewed_at)
        object.__setattr__(self, "schema_version", 1)

    @classmethod
    def automatic(
        cls, *, outputs: Iterable[str], evidence: Iterable[str] = ()
    ) -> "DeliveryManifest":
        evidence_tuple = tuple(evidence)
        return cls(
            outputs=outputs,
            evidence=evidence_tuple,
            visual_status="evidence_ready" if evidence_tuple else "not_run",
        )

    @classmethod
    def _reviewed(
        cls,
        *,
        outputs: Iterable[str],
        evidence: Iterable[str],
        reviewed_by: str,
        reviewed_at: str,
    ) -> "DeliveryManifest":
        instance = object.__new__(cls)
        instance._initialize(
            outputs=outputs,
            visual_status="passed",
            evidence=evidence,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
            allow_passed=True,
        )
        return instance

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "outputs": list(self.outputs),
            "visual_status": self.visual_status,
            "evidence": list(self.evidence),
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DeliveryManifest":
        return cls._from_dict(data, allow_reviewed=False)

    @classmethod
    def _from_dict(
        cls, data: Mapping[str, Any], *, allow_reviewed: bool
    ) -> "DeliveryManifest":
        if not isinstance(data, Mapping) or data.get("schema_version") != 1:
            raise ValueError("delivery manifest schema_version must be 1")
        status = data.get("visual_status")
        if status == "passed":
            if not allow_reviewed:
                raise ValueError("passed records require the explicit manual review API")
            try:
                return cls._reviewed(
                    outputs=data["outputs"],
                    evidence=data.get("evidence", ()),
                    reviewed_by=data.get("reviewed_by"),
                    reviewed_at=data.get("reviewed_at"),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("passed manifest requires a complete review record") from error
        return cls(
            outputs=data["outputs"],
            visual_status=str(status),
            evidence=data.get("evidence", ()),
            reviewed_by=data.get("reviewed_by"),
            reviewed_at=data.get("reviewed_at"),
        )


def mark_visual_review(
    manifest: DeliveryManifest,
    *,
    signature: str,
    reviewed_at: datetime,
) -> DeliveryManifest:
    """Return a passed copy after an explicit, signed, evidence-backed review."""

    if not isinstance(signature, str) or not signature.strip():
        raise ValueError("manual review signature is required")
    if not isinstance(reviewed_at, datetime) or reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
        raise ValueError("manual review time must be timezone-aware")
    if not manifest.evidence:
        raise ValueError("manual review requires visual evidence")
    return DeliveryManifest._reviewed(
        outputs=manifest.outputs,
        evidence=manifest.evidence,
        reviewed_by=signature,
        reviewed_at=reviewed_at.isoformat(timespec="seconds"),
    )


def _reject_symlink_chain(path: Path) -> None:
    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise ValueError(f"refusing symlink destination: {path.name}")
        if candidate.exists():
            continue


def write_delivery_manifest(manifest: DeliveryManifest, destination: Path) -> Path:
    """Write stable JSON by same-directory temporary file and atomic replace."""

    destination = Path(destination)
    if destination.suffix.lower() != ".json" or ".." in destination.parts:
        raise ValueError("delivery manifest destination must be a JSON file")
    _reject_symlink_chain(destination)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _reject_symlink_chain(destination)
        if destination.exists() and not destination.is_file():
            raise OSError("not a file")
        payload = json.dumps(
            manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".partial.json",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    except ValueError:
        raise
    except OSError:
        raise OSError(f"could not write delivery manifest: {destination.name}") from None
    return destination


def load_delivery_manifest(
    path: Path, *, review_signature: str | None = None
) -> DeliveryManifest:
    path = Path(path)
    _reject_symlink_chain(path)
    if not path.is_file():
        raise ValueError(f"delivery manifest is unavailable: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError(f"delivery manifest is unreadable: {path.name}") from None
    manifest = DeliveryManifest._from_dict(
        payload, allow_reviewed=payload.get("visual_status") == "passed"
    )
    if manifest.visual_status == "passed" and review_signature != manifest.reviewed_by:
        raise ValueError("passed manifest requires matching manual review confirmation")
    return manifest
