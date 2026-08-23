"""Agent-neutral, resumable orchestration for the public local pipeline."""

from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Mapping, Sequence

from chinese_exam_kit.content.docx import build_all
from chinese_exam_kit.content.validate import validate_content_dir
from chinese_exam_kit.delivery import (
    DeliveryManifest,
    load_delivery_manifest,
    write_delivery_manifest,
)
from chinese_exam_kit.extract.documents import extract_document, write_extraction_artifacts
from chinese_exam_kit.media.learning import (
    LocalMediaProvider,
    MediaLearningResult,
    MediaProvider,
    write_media_index,
)
from chinese_exam_kit.knowledge import (
    CandidateKnowledge,
    QuestionKnowledge,
    append_audit_event,
    load_contract as load_knowledge_contract,
    match_manifest,
    validate_library,
)

from .models import MaterialRecord, PipelineTask, STAGES, StageRecord
from .state import load_task, save_task, sha256_file, transition_stage


DOCUMENT_TYPES = frozenset(
    {"document", "document_unknown", "exam_candidate", "answer_candidate"}
)
MEDIA_TYPES = frozenset({"audio", "video"})
DEPENDENT_ON_ANSWERS = (
    "extract",
    "knowledge_pre",
    "analysis",
    "delivery",
    "knowledge_post",
    "verification",
)


@dataclass(frozen=True)
class StageSummary:
    name: str
    status: str
    attempts: int

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "attempts": self.attempts}


@dataclass(frozen=True)
class PipelineSummary:
    task_id: str
    status: str
    stages: Mapping[str, StageSummary]

    def stage(self, name: str) -> StageSummary:
        try:
            return self.stages[name]
        except KeyError as error:
            raise ValueError(f"unknown stage: {name}") from error

    def to_dict(self, *, task_path: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "task_id": self.task_id,
            "status": self.status,
            "stages": {name: self.stages[name].to_dict() for name in STAGES},
        }
        if task_path is not None:
            payload["task"] = task_path
        return payload


def summarize(task: PipelineTask) -> PipelineSummary:
    statuses = {name: task.stages[name].status for name in STAGES}
    if any(value == "waiting" for value in statuses.values()):
        status = "needs_user_input"
    elif any(value == "failed" for value in statuses.values()):
        status = "failed"
    elif all(value in {"completed", "degraded"} for value in statuses.values()):
        status = "completed"
    else:
        status = "running"
    return PipelineSummary(
        task.task_id,
        status,
        {
            name: StageSummary(name, task.stages[name].status, task.stages[name].attempts)
            for name in STAGES
        },
    )


class PipelineRunner:
    """Run only deterministic local stages and pause for human/agent authorship."""

    def __init__(
        self,
        project_root: Path,
        *,
        media_provider: MediaProvider | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.media_provider = media_provider or LocalMediaProvider()

    def run(self, task_path: Path) -> PipelineSummary:
        return self.resume(task_path)

    def resume(self, task_path: Path) -> PipelineSummary:
        safe_path = safe_task_path(self.project_root, task_path)
        with task_lock(safe_path.parent):
            task = load_task(safe_path)
            task = self._intake(task)
            task = self._extract(task)
            task = self._media(task)
            _ensure_current_answer_marker(task, _current_answer_revision(task))
            task, knowledge_ready = self._knowledge_pre(task)
            if not knowledge_ready:
                return summarize(task)
            task, ready = self._analysis(task)
            if not ready:
                return summarize(task)
            task = self._delivery(task)
            task, knowledge_ready = self._knowledge_post(task)
            if not knowledge_ready:
                return summarize(task)
            task = self._verification(task)
            return summarize(task)

    def _intake(self, task: PipelineTask) -> PipelineTask:
        fingerprint_records = tuple(
            record for record in task.materials if record.material_type != "answer_candidate"
        )
        fingerprint = _materials_fingerprint(fingerprint_records)

        def validate_archives() -> bool:
            for record in task.materials:
                archived = _safe_existing_file(task.workspace, record.archived_path)
                if archived is None:
                    raise ValueError("archived input is missing or unsafe")
                if sha256_file(archived) != record.sha256:
                    raise ValueError("archived input digest mismatch")
            return True

        return self._execute(
            task,
            "intake",
            fingerprint,
            validate_archives,
            current_validator=validate_archives,
        )

    def _extract(self, task: PipelineTask) -> PipelineTask:
        records = tuple(
            record for record in task.materials if record.material_type in DOCUMENT_TYPES
        )
        fingerprint = _materials_fingerprint(records)
        base = task.workspace / "artifacts" / "extract"
        receipt_path = base / "receipt.json"
        expected_artifacts = tuple(
            base / record.sha256[:16] / filename
            for record in records
            for filename in (
                "question_text.md",
                "question_index.json",
                "extraction_review.json",
            )
        )

        def extract() -> None:
            _reject_symlinks_below(
                base, managed_root=task.workspace, label="extract artifacts"
            )
            for record in records:
                source = task.workspace / record.archived_path
                result = extract_document(source)
                write_extraction_artifacts(result, base / record.sha256[:16])
            _write_file_receipt(
                receipt_path,
                root=task.workspace,
                fingerprint=fingerprint,
                files=expected_artifacts,
                field="artifacts",
            )

        return self._execute(
            task,
            "extract",
            fingerprint,
            extract,
            current_validator=lambda: _valid_file_receipt(
                receipt_path,
                root=task.workspace,
                fingerprint=fingerprint,
                field="artifacts",
                expected_files=expected_artifacts,
            ),
        )

    def _media(self, task: PipelineTask) -> PipelineTask:
        records = tuple(record for record in task.materials if record.material_type in MEDIA_TYPES)
        fingerprint = _materials_fingerprint(records)
        current = task.stages["media"]
        media_dir = task.workspace / "media"
        _prepare_managed_directory(media_dir, managed_root=task.workspace, label="media")
        receipt_path = media_dir / "receipt.json"
        index_path = media_dir / "index.json"
        receipt = _valid_media_receipt(receipt_path, index_path, fingerprint)
        if (
            receipt is not None
            and receipt["status"] == current.status
            and _is_current(current, fingerprint, {"completed", "degraded"})
        ):
            return task
        if receipt is not None:
            task = _begin(task, "media")
            task = _finish(task, "media", receipt["status"], fingerprint)
            save_task(task)
            return task
        task = _begin(task, "media")
        save_task(task)
        try:
            if records:
                paths = tuple(task.workspace / record.archived_path for record in records)
                result = self.media_provider.process(paths, media_dir)
            else:
                result = MediaLearningResult.completed()
            write_media_index(result, index_path)
        except Exception:
            result = MediaLearningResult.degraded("本地媒体处理失败，试卷流程已继续")
            write_media_index(result, index_path)
        _atomic_json(
            receipt_path,
            {
                "schema_version": 1,
                "fingerprint": fingerprint,
                "status": result.status,
                "index_sha256": sha256_file(index_path),
            },
        )
        task = _finish(task, "media", result.status, fingerprint)
        save_task(task)
        return task

    def _knowledge_pre(self, task: PipelineTask) -> tuple[PipelineTask, bool]:
        knowledge_root = task.workspace / "knowledge"
        manifest_path = knowledge_root / "question-manifest.json"
        try:
            _reject_symlinks_below(
                knowledge_root,
                managed_root=task.workspace,
                label="knowledge evidence",
            )
            contract = load_knowledge_contract()
            allowed_modules = _contract_modules(contract)
            questions, manifest_sha256 = _load_question_manifest(
                task,
                manifest_path,
                allowed_modules=allowed_modules,
            )
            library = validate_library(self.project_root / "knowledge", contract)
            if library.errors:
                raise ValueError("public knowledge library is invalid")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            fingerprint = _fingerprint_parts(
                "knowledge-pre-waiting", _materials_fingerprint(task.materials)
            )
            work_order = task.workspace / "work_orders" / "knowledge-pre.md"
            _ensure_atomic_text(work_order, _knowledge_pre_work_order_text())
            current = task.stages["knowledge_pre"]
            if not (
                work_order.is_file()
                and _is_current(current, fingerprint, {"waiting"})
            ):
                task = _wait(task, "knowledge_pre", fingerprint)
                save_task(task)
            return task, False

        library_fingerprint = _knowledge_library_fingerprint(library.cards)
        search = match_manifest(questions, library.cards)
        match_records = [_question_match_dict(item) for item in search.matches]
        decisions = _pre_decisions(questions, search.matches)
        audit_events = [
            _audit_event_expectation(
                task_id=task.task_id,
                stage="pre",
                event="search",
                card_id=(decision["applicable_card_ids"] or [None])[0],
                applicability=str(decision["applicability"]),
                reason=str(decision["reason"]),
                details={
                    "question_id": decision["question_id"],
                    "applicable_card_ids": decision["applicable_card_ids"],
                },
            )
            for decision in decisions
        ]
        fingerprint = _fingerprint_parts(
            "knowledge-pre", manifest_sha256, library_fingerprint
        )
        receipt_path = task.workspace / "knowledge" / "pre-receipt.json"

        def write_receipt() -> None:
            audit_path: Path | None = None
            for decision in decisions:
                audit_path = append_audit_event(
                    task.workspace / "knowledge",
                    task_id=task.task_id,
                    stage="pre",
                    event="search",
                    card_id=(decision["applicable_card_ids"] or [None])[0],
                    applicability=str(decision["applicability"]),
                    reason=str(decision["reason"]),
                    details={
                        "question_id": decision["question_id"],
                        "applicable_card_ids": decision["applicable_card_ids"],
                    },
                )
            if audit_path is None:
                audit_path = _ensure_empty_audit(task.workspace / "knowledge", task.task_id)
            audit_count, audit_prefix_sha256 = _audit_prefix_receipt(audit_path)
            _atomic_json(
                receipt_path,
                {
                    "schema_version": 1,
                    "fingerprint": fingerprint,
                    "manifest_sha256": manifest_sha256,
                    "library_fingerprint": library_fingerprint,
                    "completed_question_ids": list(search.completed_question_ids),
                    "decisions": decisions,
                    "matches": match_records,
                    "audit_path": audit_path.relative_to(task.workspace).as_posix(),
                    "audit_events": audit_events,
                    "audit_event_count": audit_count,
                    "audit_prefix_sha256": audit_prefix_sha256,
                },
            )

        updated = self._execute(
            task,
            "knowledge_pre",
            fingerprint,
            write_receipt,
            current_validator=lambda: _valid_knowledge_receipt(
                receipt_path,
                workspace=task.workspace,
                fingerprint=fingerprint,
                completed_question_ids=search.completed_question_ids,
                result_field="decisions",
                expected_results=decisions,
                expected_audit_events=audit_events,
                expected_fields={
                    "manifest_sha256": manifest_sha256,
                    "library_fingerprint": library_fingerprint,
                    "matches": match_records,
                    "audit_path": f"knowledge/audit/{task.task_id}.jsonl",
                },
            ),
        )
        return updated, True

    def _analysis(self, task: PipelineTask) -> tuple[PipelineTask, bool]:
        content_dir = task.workspace / "content"
        content_fingerprint = _content_fingerprint(content_dir)
        answer_revision = _current_answer_revision(task)
        stage_fingerprint = _analysis_stage_fingerprint(
            content_fingerprint, answer_revision
        )
        _ensure_current_answer_marker(task, answer_revision)
        acknowledgement_valid = _valid_analysis_acknowledgement(
            content_dir,
            fingerprint=content_fingerprint,
            answer_revision=answer_revision,
        )
        current = task.stages["analysis"]
        analysis_artifact = task.workspace / "artifacts" / "analysis.json"
        if (
            acknowledgement_valid
            and _is_current(current, stage_fingerprint, {"completed"})
            and _valid_analysis_artifact(
                analysis_artifact,
                fingerprint=content_fingerprint,
                answer_revision=answer_revision,
            )
        ):
            return task, True
        issues = validate_content_dir(content_dir)
        blocking = [issue.to_dict() for issue in issues]
        if answer_revision is not None and not acknowledgement_valid:
            blocking.append(
                {
                    "level": "error",
                    "code": "answer_revision_not_acknowledged",
                    "path": "analysis-receipt.json",
                    "line": None,
                    "module": None,
                    "section": None,
                    "message": "正式源稿尚未绑定最新答案版本",
                }
            )
        if blocking:
            work_order = _analysis_work_order_text(
                answer_revision=answer_revision,
                content_fingerprint=content_fingerprint,
            )
            validation_payload = {
                "issues": blocking,
                "schema_version": 1,
                "content_fingerprint": content_fingerprint,
                "answer_revision": answer_revision,
            }
            work_order_path = task.workspace / "work_orders" / "analysis.md"
            validation_path = task.workspace / "work_orders" / "analysis-validation.json"
            _ensure_atomic_text(work_order_path, work_order)
            _ensure_atomic_json(validation_path, validation_payload)
            waiting_artifacts_current = (
                work_order_path.is_file()
                and validation_path.is_file()
                and _is_current(current, stage_fingerprint, {"waiting"})
            )
            if not waiting_artifacts_current:
                task = _wait(task, "analysis", stage_fingerprint)
                save_task(task)
            return task, False

        def record_validated_source() -> None:
            _atomic_json(
                analysis_artifact,
                {
                    "schema_version": 1,
                    "status": "validated",
                    "content_fingerprint": content_fingerprint,
                    "answer_revision": answer_revision,
                },
            )

        return (
            self._execute(
                task,
                "analysis",
                stage_fingerprint,
                record_validated_source,
                current_validator=lambda: _valid_analysis_artifact(
                    analysis_artifact,
                    fingerprint=content_fingerprint,
                    answer_revision=answer_revision,
                ),
            ),
            True,
        )

    def _delivery(self, task: PipelineTask) -> PipelineTask:
        fingerprint = _content_fingerprint(task.workspace / "content")
        receipt_path = task.workspace / "output" / "delivery-receipt.json"
        expected_outputs = tuple(
            task.workspace / "output" / "docx" / f"{path.stem}.docx"
            for path in sorted(
                (task.workspace / "content").glob("*.md"), key=lambda item: item.name
            )
            if path.is_file() and not path.is_symlink()
        ) + (
            task.workspace / "output" / "visual-review-checklist.md",
            task.workspace / "output" / "delivery.json",
        )

        def build() -> None:
            output_dir = task.workspace / "output" / "docx"
            outputs = build_all(task.workspace / "content", output_dir)
            checklist = task.workspace / "output" / "visual-review-checklist.md"
            _atomic_text(
                checklist,
                "# 逐页视觉验收\n\n请渲染并逐页检查全部 Word；此清单仅表示证据包已准备，不表示视觉验收通过。\n",
            )
            relative_outputs = tuple(_project_relative(self.project_root, path) for path in outputs)
            manifest = DeliveryManifest.automatic(
                outputs=relative_outputs,
                evidence=(_project_relative(self.project_root, checklist),),
            )
            write_delivery_manifest(manifest, task.workspace / "output" / "delivery.json")
            receipt_files = tuple(outputs) + (
                checklist,
                task.workspace / "output" / "delivery.json",
            )
            _write_file_receipt(
                receipt_path,
                root=task.workspace,
                fingerprint=fingerprint,
                files=receipt_files,
                field="outputs",
            )

        return self._execute(
            task,
            "delivery",
            fingerprint,
            build,
            current_validator=lambda: _valid_file_receipt(
                receipt_path,
                root=task.workspace,
                fingerprint=fingerprint,
                field="outputs",
                expected_files=expected_outputs,
            ),
        )

    def _knowledge_post(self, task: PipelineTask) -> tuple[PipelineTask, bool]:
        knowledge_root = task.workspace / "knowledge"
        manifest_path = knowledge_root / "question-manifest.json"
        review_path = knowledge_root / "post-review.json"
        try:
            _reject_symlinks_below(
                knowledge_root,
                managed_root=task.workspace,
                label="knowledge evidence",
            )
            allowed_modules = _contract_modules(load_knowledge_contract())
            questions, manifest_sha256 = _load_question_manifest(
                task,
                manifest_path,
                allowed_modules=allowed_modules,
            )
            reviews, review_sha256 = _load_post_review(
                review_path,
                questions,
                allowed_modules=allowed_modules,
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            fingerprint = _fingerprint_parts(
                "knowledge-post-waiting",
                _content_fingerprint(task.workspace / "content"),
                manifest_path.name,
            )
            work_order = task.workspace / "work_orders" / "knowledge-post.md"
            _ensure_atomic_text(work_order, _knowledge_post_work_order_text())
            current = task.stages["knowledge_post"]
            if not (
                work_order.is_file()
                and _is_current(current, fingerprint, {"waiting"})
            ):
                task = _wait(task, "knowledge_post", fingerprint)
                save_task(task)
            return task, False

        pre_receipt = task.workspace / "knowledge" / "pre-receipt.json"
        pre_sha256 = sha256_file(pre_receipt)
        fingerprint = _fingerprint_parts(
            "knowledge-post",
            _content_fingerprint(task.workspace / "content"),
            manifest_sha256,
            review_sha256,
            pre_sha256,
        )
        receipt_path = task.workspace / "knowledge" / "post-receipt.json"
        resolutions = [
            {
                "question_id": item["question_id"],
                "conflict_status": item["conflict"]["status"],
                "candidate_status": item["candidate"]["status"],
            }
            for item in reviews
        ]
        audit_events = _post_audit_expectations(task.task_id, reviews)

        def write_receipt() -> None:
            audit_path: Path | None = None
            for item in reviews:
                conflict = item["conflict"]
                audit_path = append_audit_event(
                    task.workspace / "knowledge",
                    task_id=task.task_id,
                    stage="post",
                    event="review",
                    applicability=(
                        "review_required"
                        if conflict["status"] == "review_required"
                        else "not_applicable"
                    ),
                    reason=conflict["reason"],
                    details={"question_id": item["question_id"]},
                )
                candidate = item["candidate"]
                if candidate["status"] == "candidate":
                    audit_path = append_audit_event(
                        task.workspace / "knowledge",
                        task_id=task.task_id,
                        stage="post",
                        event="candidate",
                        reason=candidate["reason"],
                        details={
                            "question_id": item["question_id"],
                            "candidate": candidate["record"],
                        },
                    )
            if audit_path is None:
                audit_path = _ensure_empty_audit(task.workspace / "knowledge", task.task_id)
            audit_count, audit_prefix_sha256 = _audit_prefix_receipt(audit_path)
            _atomic_json(
                receipt_path,
                {
                    "schema_version": 1,
                    "fingerprint": fingerprint,
                    "manifest_sha256": manifest_sha256,
                    "post_review_sha256": review_sha256,
                    "pre_receipt_sha256": pre_sha256,
                    "completed_question_ids": [item.question_id for item in questions],
                    "resolutions": resolutions,
                    "audit_path": audit_path.relative_to(task.workspace).as_posix(),
                    "audit_events": audit_events,
                    "audit_event_count": audit_count,
                    "audit_prefix_sha256": audit_prefix_sha256,
                },
            )

        updated = self._execute(
            task,
            "knowledge_post",
            fingerprint,
            write_receipt,
            current_validator=lambda: _valid_knowledge_receipt(
                receipt_path,
                workspace=task.workspace,
                fingerprint=fingerprint,
                completed_question_ids=tuple(item.question_id for item in questions),
                result_field="resolutions",
                expected_results=resolutions,
                expected_audit_events=audit_events,
                expected_fields={
                    "manifest_sha256": manifest_sha256,
                    "post_review_sha256": review_sha256,
                    "pre_receipt_sha256": pre_sha256,
                    "audit_path": f"knowledge/audit/{task.task_id}.jsonl",
                },
            ),
        )
        return updated, True

    def _verification(self, task: PipelineTask) -> PipelineTask:
        manifest = task.workspace / "output" / "delivery.json"
        fingerprint = sha256_file(manifest)
        verification_path = task.workspace / "output" / "verification.json"
        delivery_receipt = task.workspace / "output" / "delivery-receipt.json"
        delivery_fingerprint = _content_fingerprint(task.workspace / "content")

        def record() -> None:
            try:
                output_records = _evidence_ready_delivery_outputs(
                    self.project_root,
                    workspace=task.workspace,
                    manifest=manifest,
                    delivery_receipt=delivery_receipt,
                    delivery_fingerprint=delivery_fingerprint,
                )
            except (OSError, UnicodeError, ValueError):
                raise ValueError("verification evidence contract is not satisfied") from None
            _atomic_json(
                verification_path,
                {
                    "schema_version": 1,
                    "content_validation": "passed",
                    "visual_review": "evidence_ready",
                    "manifest_sha256": fingerprint,
                    "outputs": output_records,
                },
            )

        return self._execute(
            task,
            "verification",
            fingerprint,
            record,
            current_validator=lambda: _valid_verification(
                self.project_root,
                verification_path,
                manifest,
                fingerprint,
                workspace=task.workspace,
                delivery_receipt=delivery_receipt,
                delivery_fingerprint=delivery_fingerprint,
            ),
        )

    def _execute(
        self,
        task: PipelineTask,
        stage: str,
        fingerprint: str,
        action: Callable[[], None],
        *,
        current_validator: Callable[[], bool] | None = None,
    ) -> PipelineTask:
        if _is_current(task.stages[stage], fingerprint, {"completed"}):
            try:
                if current_validator is None or current_validator():
                    return task
            except Exception:
                pass
        task = _begin(task, stage)
        save_task(task)
        try:
            action()
            if current_validator is not None:
                try:
                    valid_output = current_validator()
                except Exception:
                    valid_output = False
                if not valid_output:
                    raise ValueError("stage output failed postcondition validation")
        except Exception:
            failed = transition_stage(task, stage, "failed")
            save_task(failed)
            raise
        task = _finish(task, stage, "completed", fingerprint)
        save_task(task)
        return task


def safe_task_path(project_root: Path, task_path: Path) -> Path:
    root = Path(project_root).resolve()
    candidate = Path(task_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    if ".." in candidate.parts:
        raise ValueError("task path cannot contain parent traversal")
    if candidate.name != "task.json":
        raise ValueError("task state path must end with task.json")
    absolute = candidate.absolute()
    managed_chain = (
        absolute,
        absolute.parent,
        absolute.parent.parent,
        absolute.parent.parent.parent,
    )
    if any(path.is_symlink() for path in managed_chain):
        raise ValueError("task path cannot use symlinks")
    try:
        resolved = absolute.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("task path must be inside the current project") from None
    if len(relative.parts) != 4 or relative.parts[:2] != (".local", "tasks"):
        raise ValueError("task path must match .local/tasks/SLUG/task.json")
    if not resolved.is_file():
        raise ValueError("task.json is unavailable")
    return resolved


@contextmanager
def task_lock(workspace: Path) -> Iterator[None]:
    lock_path = Path(workspace) / ".pipeline.lock"
    if lock_path.is_symlink():
        raise ValueError("pipeline lock cannot be a symlink")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        if error.errno in {errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)}:
            raise ValueError("pipeline lock is unsafe") from None
        raise
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("pipeline lock is unsafe")
    with os.fdopen(descriptor, "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _begin(task: PipelineTask, stage: str) -> PipelineTask:
    current = task.stages[stage]
    if current.status == "running":
        return task
    return transition_stage(task, stage, "running")


def _finish(task: PipelineTask, stage: str, status: str, fingerprint: str) -> PipelineTask:
    task = transition_stage(task, stage, status)
    stages = dict(task.stages)
    record = stages[stage]
    stages[stage] = replace(
        record,
        events=record.events
        + ({"event": "stage_result", "fingerprint": fingerprint, "status": status},),
    )
    return replace(task, stages=stages)


def _wait(task: PipelineTask, stage: str, fingerprint: str) -> PipelineTask:
    current = task.stages[stage]
    if current.status == "running":
        task = transition_stage(task, stage, "waiting")
    elif current.status == "waiting":
        task = transition_stage(task, stage, "running")
        task = transition_stage(task, stage, "waiting")
    elif current.status == "pending":
        task = transition_stage(task, stage, "waiting")
    else:
        task = transition_stage(task, stage, "running")
        task = transition_stage(task, stage, "waiting")
    stages = dict(task.stages)
    record = stages[stage]
    stages[stage] = replace(
        record,
        events=record.events
        + ({"event": "stage_result", "fingerprint": fingerprint, "status": "waiting"},),
    )
    task = replace(task, stages=stages)
    stages = dict(task.stages)
    for downstream in STAGES[STAGES.index(stage) + 1 :]:
        downstream_record = stages[downstream]
        if downstream_record.status == "pending":
            continue
        stages[downstream] = replace(
            downstream_record,
            status="pending",
            events=downstream_record.events
            + ({"event": "invalidated", "reason": "analysis source changed"},),
        )
    return replace(task, stages=stages)


def _is_current(record: StageRecord, fingerprint: str, statuses: set[str]) -> bool:
    if record.status not in statuses:
        return False
    latest = next(
        (
            event
            for event in reversed(record.events)
            if event.get("event") == "stage_result"
        ),
        None,
    )
    return bool(
        latest is not None
        and latest.get("fingerprint") == fingerprint
        and latest.get("status") == record.status
    )


def _materials_fingerprint(materials: Sequence[MaterialRecord]) -> str:
    parts = sorted(
        f"{record.sha256}:{record.material_type}:{record.archived_path}"
        for record in materials
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _fingerprint_parts(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _contract_modules(contract: Mapping[str, object]) -> frozenset[str]:
    modules = contract.get("modules")
    if (
        not isinstance(modules, list)
        or not modules
        or not all(isinstance(item, str) and item.strip() for item in modules)
    ):
        raise ValueError("knowledge contract modules are invalid")
    return frozenset(item.strip() for item in modules)


def _load_question_manifest(
    task: PipelineTask,
    path: Path,
    *,
    allowed_modules: frozenset[str],
) -> tuple[tuple[QuestionKnowledge, ...], str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("question manifest is missing or unsafe")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "questions"}
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("questions"), list)
    ):
        raise ValueError("question manifest is invalid")
    expected: dict[tuple[str, int, int, int], str] = {}
    for record in task.materials:
        if record.material_type not in DOCUMENT_TYPES or record.material_type == "answer_candidate":
            continue
        relative = f"artifacts/extract/{record.sha256[:16]}/question_index.json"
        index_path = _safe_existing_file(task.workspace, relative)
        if index_path is None:
            raise ValueError("question index is missing or unsafe")
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(index, list):
            raise ValueError("question index is invalid")
        for ordinal, raw in enumerate(index, 1):
            if (
                not isinstance(raw, dict)
                or not isinstance(raw.get("number"), int)
                or isinstance(raw.get("number"), bool)
                or not isinstance(raw.get("page_start"), int)
                or isinstance(raw.get("page_start"), bool)
            ):
                raise ValueError("question index is invalid")
            expected[(record.sha256, ordinal, raw["number"], raw["page_start"])] = str(
                raw.get("section", "unclassified")
            )
    questions: list[QuestionKnowledge] = []
    actual: set[tuple[str, int, int, int]] = set()
    identifiers: set[str] = set()
    required = {
        "question_id",
        "source_sha256",
        "source_ordinal",
        "question_number",
        "page_start",
        "module",
        "question_type",
        "abilities",
        "task_statement",
        "evidence_anchor",
        "answer_boundary",
        "retrieval_queries",
    }
    for raw in payload["questions"]:
        if not isinstance(raw, dict) or set(raw) != required:
            raise ValueError("question manifest record is invalid")
        identifier = raw["question_id"]
        source_sha = raw["source_sha256"]
        source_ordinal = raw["source_ordinal"]
        question_number = raw["question_number"]
        page_start = raw["page_start"]
        normalized_identifier = identifier.strip() if isinstance(identifier, str) else ""
        text_fields = (
            raw["module"],
            raw["question_type"],
            raw["task_statement"],
            raw["evidence_anchor"],
            raw["answer_boundary"],
        )
        if (
            not isinstance(identifier, str)
            or not normalized_identifier
            or normalized_identifier in identifiers
            or not isinstance(source_sha, str)
            or len(source_sha) != 64
            or not all(
                isinstance(value, int) and not isinstance(value, bool) and value > 0
                for value in (source_ordinal, question_number, page_start)
            )
            or not all(isinstance(value, str) and value.strip() for value in text_fields)
            or raw["module"].strip() not in allowed_modules
            or not isinstance(raw["abilities"], list)
            or not raw["abilities"]
            or not all(isinstance(item, str) and item.strip() for item in raw["abilities"])
            or not isinstance(raw["retrieval_queries"], list)
            or not raw["retrieval_queries"]
            or not all(
                isinstance(item, str) and item.strip()
                for item in raw["retrieval_queries"]
            )
        ):
            raise ValueError("question manifest record is invalid")
        key = (source_sha, source_ordinal, question_number, page_start)
        if key in actual:
            raise ValueError("question manifest record is duplicated")
        extracted_module = expected.get(key)
        module = raw["module"].strip()
        if extracted_module not in {None, "unclassified"} and module != extracted_module:
            raise ValueError("question manifest module conflicts with extraction")
        identifiers.add(normalized_identifier)
        actual.add(key)
        questions.append(
            QuestionKnowledge(
                question_id=normalized_identifier,
                module=module,
                question_type=raw["question_type"].strip(),
                abilities=tuple(item.strip() for item in raw["abilities"]),
                task_statement=raw["task_statement"].strip(),
                evidence_anchor=raw["evidence_anchor"].strip(),
                answer_boundary=raw["answer_boundary"].strip(),
                retrieval_queries=tuple(
                    item.strip() for item in raw["retrieval_queries"]
                ),
            )
        )
    if actual != set(expected):
        raise ValueError("question manifest does not exactly cover extracted questions")
    return tuple(questions), sha256_file(path)


def _knowledge_library_fingerprint(cards: Sequence[object]) -> str:
    records = []
    for card in cards:
        records.append(
            {
                "id": card.card_id,
                "status": card.status,
                "metadata": dict(card.metadata),
                "body": card.body,
            }
        )
    return hashlib.sha256(
        json.dumps(records, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _question_match_dict(match: object) -> dict[str, object]:
    return {
        "question_id": match.question_id,
        "card_id": match.card_id,
        "card_status": match.card_status,
        "score": match.score,
        "matched_dimensions": list(match.matched_dimensions),
        "applicability": match.applicability,
        "reason": match.reason,
        "boundary": match.boundary,
    }


def _pre_decisions(
    questions: Sequence[QuestionKnowledge], matches: Sequence[object]
) -> list[dict[str, object]]:
    decisions = []
    for question in questions:
        applicable_ids = sorted(
            match.card_id
            for match in matches
            if match.question_id == question.question_id
            and match.applicability == "applicable"
            and match.card_status not in {"review_required", "deprecated"}
        )
        applicable = bool(applicable_ids)
        decisions.append(
            {
                "question_id": question.question_id,
                "applicability": "applicable" if applicable else "not_applicable",
                "applicable_card_ids": applicable_ids,
                "reason": (
                    "匹配到达到适用门槛的公共知识卡；仍须核对本题证据与评分边界。"
                    if applicable
                    else "公共知识库没有达到适用门槛的知识卡。"
                ),
            }
        )
    return decisions


def _ensure_empty_audit(root: Path, task_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", task_id):
        raise ValueError("task id is unsafe")
    path = root / "audit" / f"{task_id}.jsonl"
    if not path.exists():
        _atomic_text(path, "")
    return path


def _audit_event_expectation(
    *,
    task_id: str,
    stage: str,
    event: str,
    card_id: object = None,
    applicability: object = None,
    reason: object = None,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "stage": stage,
        "event": event,
        "card_id": card_id,
        "applicability": applicability,
        "reason": reason,
        "details": dict(details or {}),
    }


def _post_audit_expectations(
    task_id: str, reviews: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for item in reviews:
        conflict = item["conflict"]
        candidate = item["candidate"]
        events.append(
            _audit_event_expectation(
                task_id=task_id,
                stage="post",
                event="review",
                applicability=(
                    "review_required"
                    if conflict["status"] == "review_required"
                    else "not_applicable"
                ),
                reason=conflict["reason"],
                details={"question_id": item["question_id"]},
            )
        )
        if candidate["status"] == "candidate":
            events.append(
                _audit_event_expectation(
                    task_id=task_id,
                    stage="post",
                    event="candidate",
                    reason=candidate["reason"],
                    details={
                        "question_id": item["question_id"],
                        "candidate": candidate["record"],
                    },
                )
            )
    return events


def _audit_event_signature(value: object) -> dict[str, object] | None:
    required = {
        "timestamp",
        "task_id",
        "stage",
        "event",
        "card_id",
        "applicability",
        "reason",
        "details",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or not isinstance(value.get("timestamp"), str)
        or not value["timestamp"].strip()
        or not isinstance(value.get("details"), dict)
    ):
        return None
    return {key: value[key] for key in required if key != "timestamp"}


def _valid_knowledge_receipt(
    path: Path,
    *,
    workspace: Path,
    fingerprint: str,
    completed_question_ids: Sequence[str],
    result_field: str,
    expected_results: Sequence[Mapping[str, object]],
    expected_audit_events: Sequence[Mapping[str, object]],
    expected_fields: Mapping[str, object],
) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("fingerprint") != fingerprint
        or payload.get("completed_question_ids") != list(completed_question_ids)
        or payload.get(result_field) != list(expected_results)
        or payload.get("audit_events") != list(expected_audit_events)
        or any(payload.get(key) != value for key, value in expected_fields.items())
        or not isinstance(payload.get("audit_event_count"), int)
        or isinstance(payload.get("audit_event_count"), bool)
        or payload.get("audit_event_count") < 0
        or not isinstance(payload.get("audit_prefix_sha256"), str)
    ):
        return False
    audit_path = _safe_existing_file(workspace, payload.get("audit_path"))
    if audit_path is None:
        return False
    try:
        lines = audit_path.read_bytes().splitlines(keepends=True)
        count = payload["audit_event_count"]
        if len(lines) < count:
            return False
        prefix = b"".join(lines[:count])
        signatures = []
        for line in lines[:count]:
            signature = _audit_event_signature(json.loads(line))
            if signature is None:
                return False
            signatures.append(signature)
    except (OSError, json.JSONDecodeError):
        return False
    if expected_audit_events and signatures[-len(expected_audit_events) :] != list(
        expected_audit_events
    ):
        return False
    return hashlib.sha256(prefix).hexdigest() == payload.get("audit_prefix_sha256")


def _audit_prefix_receipt(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    lines = data.splitlines(keepends=True)
    for line in lines:
        json.loads(line)
    return len(lines), hashlib.sha256(data).hexdigest()


def _load_post_review(
    path: Path,
    questions: Sequence[QuestionKnowledge],
    *,
    allowed_modules: frozenset[str],
) -> tuple[list[dict[str, object]], str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("post review is missing or unsafe")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "questions"}
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("questions"), list)
    ):
        raise ValueError("post review is invalid")
    expected_ids = [question.question_id for question in questions]
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in payload["questions"]:
        if not isinstance(raw, dict) or set(raw) != {"question_id", "conflict", "candidate"}:
            raise ValueError("post review record is invalid")
        question_id = raw["question_id"]
        conflict = raw["conflict"]
        candidate = raw["candidate"]
        if (
            not isinstance(question_id, str)
            or question_id in seen
            or not isinstance(conflict, dict)
            or set(conflict) != {"status", "reason"}
            or conflict.get("status") not in {"none", "review_required"}
            or not isinstance(conflict.get("reason"), str)
            or not conflict["reason"].strip()
            or not isinstance(candidate, dict)
            or candidate.get("status") not in {"none", "candidate"}
            or not isinstance(candidate.get("reason"), str)
            or not candidate["reason"].strip()
        ):
            raise ValueError("post review record is invalid")
        status = candidate["status"]
        expected_candidate_fields = (
            {"status", "reason", "record"}
            if status == "candidate"
            else {"status", "reason"}
        )
        if set(candidate) != expected_candidate_fields:
            raise ValueError("post review candidate is invalid")
        normalized_candidate: dict[str, object] = {
            "status": status,
            "reason": candidate["reason"],
        }
        if status == "candidate":
            record = candidate["record"]
            if not isinstance(record, dict) or set(record) != {
                "card_type",
                "title",
                "statement",
                "modules",
                "source",
                "risk_level",
            }:
                raise ValueError("post review candidate is invalid")
            modules = record["modules"]
            source = record["source"]
            if (
                not all(
                    isinstance(record[field], str) and record[field].strip()
                    for field in ("card_type", "title", "statement", "risk_level")
                )
                or not isinstance(modules, list)
                or not modules
                or not all(
                    isinstance(module, str)
                    and module.strip()
                    and module.strip() in allowed_modules
                    for module in modules
                )
                or not isinstance(source, dict)
            ):
                raise ValueError("post review candidate is invalid")
            candidate_record = CandidateKnowledge(
                card_type=record["card_type"].strip(),
                title=record["title"].strip(),
                statement=record["statement"].strip(),
                modules=tuple(module.strip() for module in modules),
                source=source,
                risk_level=record["risk_level"].strip(),
            )
            normalized_candidate["record"] = candidate_record.to_dict()
        seen.add(question_id)
        normalized.append(
            {
                "question_id": question_id,
                "conflict": {
                    "status": conflict["status"],
                    "reason": conflict["reason"],
                },
                "candidate": normalized_candidate,
            }
        )
    if [item["question_id"] for item in normalized] != expected_ids:
        raise ValueError("post review must exactly cover the question manifest")
    return normalized, sha256_file(path)


def _knowledge_pre_work_order_text() -> str:
    return """# 分析前知识检索工作单

流水线已从题面提取题号，但 `knowledge/question-manifest.json` 尚未以结构化记录精确覆盖全部非答案文档的题目。

请按公开知识库规范逐题记录来源 SHA-256、来源内序号、题号、页码、板块、题型、能力、实际任务、证据抓手、答案边界和检索语句。缺题、多题或绑定不一致时，本阶段会继续等待，不会伪称已完成知识审计。
"""


def _knowledge_post_work_order_text() -> str:
    return """# 分析后知识复核工作单

请在 `knowledge/post-review.json` 对题目清单中的每道题逐一给出冲突裁决和候选知识决定。

冲突状态只能是 `none` 或 `review_required`；新发现只能是 `none` 或绑定公开 `CandidateKnowledge` 字段的 `candidate`。每项必须写明理由，不得自动升级为已核验或稳定规则。缺少任何题的裁决时，本阶段会继续等待。
"""


def _content_fingerprint(content_dir: Path) -> str:
    root = Path(content_dir)
    if root.is_symlink() or not root.is_dir():
        return hashlib.sha256(b"missing-content").hexdigest()
    digest = hashlib.sha256()
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.suffix.lower() != ".md":
            continue
        if path.is_symlink() or not path.is_file():
            digest.update(f"unsafe:{path.name}".encode("utf-8"))
            continue
        digest.update(path.name.encode("utf-8"))
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _analysis_stage_fingerprint(
    content_fingerprint: str, answer_revision: str | None
) -> str:
    payload = f"content:{content_fingerprint}\nanswer:{answer_revision or 'none'}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _analysis_work_order_text(
    *, answer_revision: str | None, content_fingerprint: str
) -> str:
    revision_instructions = ""
    if answer_revision is not None:
        revision_instructions = f"""

## 后补答案修订绑定

最新答案版本令牌：`{answer_revision}`

完成答案差异核对和源稿修订后，请在 `content/analysis-receipt.json` 写入：

```json
{{
  "schema_version": 1,
  "answer_revision": "{answer_revision}",
  "content_fingerprint": "{content_fingerprint}"
}}
```

其中 `content_fingerprint` 必须与最终 Markdown 内容完全匹配。仅修改文件时间、复制旧收据或保留旧答案令牌都不能通过门禁。

请先完成源稿修订，再运行一次 `cekit run` 刷新本工作单中的内容指纹，最后按上方结构写入收据并再次运行流水线。
"""
    return f"""# 分析源稿工作单

当前流水线已完成本地材料归档与提取，并暂停在分析阶段。

任何能够编辑文件的智能体，都可以依据项目根目录中的 `AGENTS.md`、`README.md` 和公开内容合同编写 Markdown 源稿。请把正式源稿写入本任务的 `content/` 目录。整卷应覆盖实际存在的六个板块；单板块任务只完成对应板块，不得虚构缺失内容。

必须区分【官方评分参考】【文本推导】【教学拓展】，落实选择题逐项证据、主观题答案生成和各板块硬性合同。没有正式答案时仍按【文本推导】继续，并明确答案边界。完成后重新运行 `cekit run --task .local/tasks/SLUG/task.json`。

本工作单不调用外部大模型 API，不绑定任何专有智能体语法，也不代表分析已经完成。
{revision_instructions}"""


def _write_analysis_work_order(
    workspace: Path,
    *,
    answer_revision: str | None = None,
    content_fingerprint: str | None = None,
) -> None:
    fingerprint = content_fingerprint or _content_fingerprint(workspace / "content")
    _atomic_text(
        workspace / "work_orders" / "analysis.md",
        _analysis_work_order_text(
            answer_revision=answer_revision, content_fingerprint=fingerprint
        ),
    )


def _atomic_text(path: Path, text: str) -> Path:
    destination = Path(path)
    if destination.is_symlink():
        raise ValueError("output destination cannot be a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.parent.is_symlink():
        raise ValueError("output directory cannot be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _atomic_json(path: Path, payload: object) -> Path:
    return _atomic_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _ensure_atomic_text(path: Path, text: str) -> Path:
    destination = Path(path)
    try:
        if not destination.is_symlink() and destination.read_text(encoding="utf-8") == text:
            return destination
    except (OSError, UnicodeError):
        pass
    return _atomic_text(destination, text)


def _ensure_atomic_json(path: Path, payload: object) -> Path:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return _ensure_atomic_text(path, text)


def _project_relative(project_root: Path, path: Path) -> str:
    try:
        return Path(path).relative_to(project_root).as_posix()
    except ValueError:
        raise ValueError("output path must stay inside the current project") from None


def _valid_media_receipt(
    receipt_path: Path, index_path: Path, fingerprint: str
) -> dict[str, str] | None:
    if (
        receipt_path.is_symlink()
        or index_path.is_symlink()
        or not receipt_path.is_file()
        or not index_path.is_file()
    ):
        return None
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        index_payload = json.loads(index_path.read_text(encoding="utf-8"))
        index_digest = sha256_file(index_path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(index_payload, dict):
        return None
    if (
        set(payload) != {"schema_version", "fingerprint", "status", "index_sha256"}
        or payload.get("schema_version") != 1
        or payload.get("fingerprint") != fingerprint
        or payload.get("status") not in {"completed", "degraded"}
        or index_payload.get("status") != payload.get("status")
        or payload.get("index_sha256") != index_digest
    ):
        return None
    return {"status": str(payload["status"])}


def _json_file_equals(path: Path, expected: object) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return json.loads(path.read_text(encoding="utf-8")) == expected
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def _safe_relative_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    if ":" in path.parts[0]:
        return None
    return path.as_posix()


def _write_file_receipt(
    destination: Path,
    *,
    root: Path,
    fingerprint: str,
    files: Sequence[Path],
    field: str,
) -> Path:
    records = []
    for file_path in sorted((Path(path) for path in files), key=lambda path: path.as_posix()):
        if file_path.is_symlink() or not file_path.is_file():
            raise ValueError(f"{field} receipt contains an unsafe file")
        try:
            relative = file_path.relative_to(root).as_posix()
        except ValueError:
            raise ValueError(f"{field} receipt file escapes the task") from None
        if _safe_existing_file(root, relative) != file_path.resolve():
            raise ValueError(f"{field} receipt contains an unsafe file")
        records.append({"path": relative, "sha256": sha256_file(file_path)})
    return _atomic_json(
        destination,
        {
            "schema_version": 1,
            "fingerprint": fingerprint,
            field: records,
        },
    )


def _valid_file_receipt(
    receipt_path: Path,
    *,
    root: Path,
    fingerprint: str,
    field: str,
    expected_files: Sequence[Path] | None = None,
) -> bool:
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return False
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "fingerprint", field}
        or payload.get("schema_version") != 1
        or payload.get("fingerprint") != fingerprint
        or not isinstance(payload.get(field), list)
    ):
        return False
    records = payload[field]
    if expected_files is not None:
        try:
            expected = {
                Path(path).relative_to(root).as_posix() for path in expected_files
            }
        except ValueError:
            return False
        actual = {
            record.get("path") for record in records if isinstance(record, dict)
        }
        if actual != expected:
            return False
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            return False
        relative = _safe_relative_path(record.get("path"))
        digest = record.get("sha256")
        if relative is None or not isinstance(digest, str) or len(digest) != 64:
            return False
        path = _safe_existing_file(root, relative)
        if path is None or sha256_file(path) != digest:
            return False
    return True


def _current_answer_revision(task: PipelineTask) -> str | None:
    answers = tuple(
        sorted(
            record.sha256
            for record in task.materials
            if record.material_type == "answer_candidate"
        )
    )
    if not answers:
        return None
    return hashlib.sha256("\n".join(answers).encode("utf-8")).hexdigest()


def _ensure_current_answer_marker(
    task: PipelineTask, answer_revision: str | None
) -> None:
    if answer_revision is None:
        return
    digests = sorted(
        record.sha256
        for record in task.materials
        if record.material_type == "answer_candidate"
    )
    _ensure_atomic_json(
        task.workspace / "answers" / "current_revision.json",
        {
            "schema_version": 1,
            "token": answer_revision,
            "answer_sha256": digests,
        },
    )


def _valid_analysis_acknowledgement(
    content_dir: Path, *, fingerprint: str, answer_revision: str | None
) -> bool:
    if answer_revision is None:
        return True
    receipt = content_dir / "analysis-receipt.json"
    if receipt.is_symlink() or not receipt.is_file():
        return False
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and set(payload) == {
            "schema_version",
            "answer_revision",
            "content_fingerprint",
        }
        and payload.get("schema_version") == 1
        and payload.get("answer_revision") == answer_revision
        and payload.get("content_fingerprint") == fingerprint
    )


def _valid_analysis_artifact(
    path: Path, *, fingerprint: str, answer_revision: str | None
) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and set(payload)
        == {
            "schema_version",
            "status",
            "content_fingerprint",
            "answer_revision",
        }
        and payload.get("schema_version") == 1
        and payload.get("status") == "validated"
        and payload.get("content_fingerprint") == fingerprint
        and payload.get("answer_revision") == answer_revision
    )


def _evidence_ready_delivery_outputs(
    project_root: Path,
    *,
    workspace: Path,
    manifest: Path,
    delivery_receipt: Path,
    delivery_fingerprint: str,
) -> list[dict[str, str]]:
    delivery = load_delivery_manifest(manifest)
    if delivery.visual_status != "evidence_ready":
        raise ValueError("delivery visual evidence is not ready")
    output_records = _delivery_path_records(
        project_root, delivery.outputs, field="output"
    )
    evidence_records = _delivery_path_records(
        project_root, delivery.evidence, field="evidence"
    )
    expected_files = tuple(
        Path(project_root) / record["path"]
        for record in output_records + evidence_records
    ) + (manifest,)
    if not _valid_file_receipt(
        delivery_receipt,
        root=workspace,
        fingerprint=delivery_fingerprint,
        field="outputs",
        expected_files=expected_files,
    ):
        raise ValueError("delivery artifacts do not match their receipt")
    return output_records


def _delivery_path_records(
    project_root: Path, paths: Sequence[str], *, field: str
) -> list[dict[str, str]]:
    if not paths:
        raise ValueError(f"delivery manifest {field} paths are empty")
    records = []
    for raw in paths:
        relative = _safe_relative_path(raw)
        if relative is None:
            raise ValueError(f"delivery {field} path is unsafe")
        path = _safe_existing_file(project_root, relative)
        if path is None:
            raise ValueError(f"delivery {field} is missing or unsafe")
        records.append({"path": relative, "sha256": sha256_file(path)})
    return records


def _valid_verification(
    project_root: Path,
    verification: Path,
    manifest: Path,
    manifest_fingerprint: str,
    *,
    workspace: Path,
    delivery_receipt: Path,
    delivery_fingerprint: str,
) -> bool:
    if verification.is_symlink() or not verification.is_file():
        return False
    try:
        payload = json.loads(verification.read_text(encoding="utf-8"))
        output_records = _evidence_ready_delivery_outputs(
            project_root,
            workspace=workspace,
            manifest=manifest,
            delivery_receipt=delivery_receipt,
            delivery_fingerprint=delivery_fingerprint,
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    except (TypeError, ValueError):
        return False
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "schema_version",
            "content_validation",
            "visual_review",
            "manifest_sha256",
            "outputs",
        }
        or payload.get("schema_version") != 1
        or payload.get("content_validation") != "passed"
        or payload.get("visual_review") != "evidence_ready"
        or payload.get("manifest_sha256") != manifest_fingerprint
        or sha256_file(manifest) != manifest_fingerprint
    ):
        return False
    return payload.get("outputs") == output_records


def _safe_existing_file(root: Path, relative: str) -> Path | None:
    safe = _safe_relative_path(relative)
    if safe is None:
        return None
    root_path = Path(root).resolve()
    cursor = root_path
    for part in PurePosixPath(safe).parts:
        cursor = cursor / part
        try:
            metadata = cursor.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(metadata.st_mode):
            return None
    try:
        resolved = cursor.resolve(strict=True)
        resolved.relative_to(root_path)
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        return resolved if stat.S_ISREG(resolved.stat().st_mode) else None
    except OSError:
        return None


def _prepare_managed_directory(
    directory: Path, *, managed_root: Path, label: str
) -> Path:
    """Create a real managed directory without ever following a replacement link."""

    boundary = Path(managed_root)
    target = Path(directory)
    try:
        relative = target.absolute().relative_to(boundary.absolute())
    except ValueError:
        raise ValueError(f"{label} directory escapes its managed root") from None
    if not relative.parts:
        raise ValueError(f"{label} directory must be below its managed root")
    try:
        boundary_metadata = boundary.lstat()
    except OSError:
        raise ValueError(f"{label} managed root is unsafe") from None
    if stat.S_ISLNK(boundary_metadata.st_mode) or not stat.S_ISDIR(
        boundary_metadata.st_mode
    ):
        raise ValueError(f"{label} managed root is unsafe")
    try:
        resolved_boundary = boundary.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(f"{label} managed root is unsafe") from None

    cursor = boundary
    for index, part in enumerate(relative.parts):
        cursor = cursor / part
        is_target = index == len(relative.parts) - 1
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            cursor.mkdir()
            continue
        except OSError:
            raise ValueError(f"{label} directory is unsafe") from None
        if stat.S_ISLNK(metadata.st_mode):
            if not is_target:
                raise ValueError(f"{label} directory cannot contain a symlink")
            cursor.unlink()
            cursor.mkdir()
        elif not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{label} directory is unsafe")
    try:
        cursor.resolve(strict=True).relative_to(resolved_boundary)
    except (OSError, RuntimeError, ValueError):
        raise ValueError(f"{label} directory escapes its managed root") from None
    return cursor


def _reject_symlinks_below(
    root: Path, *, managed_root: Path, label: str
) -> None:
    path = Path(root)
    boundary = Path(managed_root)
    cursor = path
    while cursor != boundary:
        if cursor.is_symlink():
            raise ValueError(f"{label} cannot contain a symlink")
        if cursor.parent == cursor:
            raise ValueError(f"{label} escapes its managed root")
        cursor = cursor.parent
    if not path.exists():
        return
    pending = [path]
    while pending:
        directory = pending.pop()
        for entry in directory.iterdir():
            if entry.is_symlink():
                raise ValueError(f"{label} cannot contain a symlink")
            if entry.is_dir():
                pending.append(entry)
