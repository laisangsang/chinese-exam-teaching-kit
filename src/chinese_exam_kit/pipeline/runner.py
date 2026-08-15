"""Agent-neutral, resumable orchestration for the public local pipeline."""

from __future__ import annotations

import hashlib
import errno
import json
import os
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
    if statuses["analysis"] == "waiting":
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
            task = self._knowledge_pre(task)
            task, ready = self._analysis(task)
            if not ready:
                return summarize(task)
            task = self._delivery(task)
            task = self._knowledge_post(task)
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

    def _knowledge_pre(self, task: PipelineTask) -> PipelineTask:
        fingerprint = _materials_fingerprint(task.materials)
        audit_path = task.workspace / "knowledge" / "pre_audit.json"
        payload = {
            "schema_version": 1,
            "status": "ready_for_question_level_retrieval",
            "note": "分析智能体应按知识库规范执行分析前检索和逐题调用。",
        }

        def write_audit() -> None:
            _atomic_json(audit_path, payload)

        return self._execute(
            task,
            "knowledge_pre",
            fingerprint,
            write_audit,
            current_validator=lambda: _json_file_equals(audit_path, payload),
        )

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

    def _knowledge_post(self, task: PipelineTask) -> PipelineTask:
        fingerprint = _content_fingerprint(task.workspace / "content")
        audit_path = task.workspace / "knowledge" / "post_audit.json"
        payload = {
            "schema_version": 1,
            "status": "review_required",
            "note": "新发现只能进入候选状态；不得自动升级为稳定规则。",
        }

        def write_audit() -> None:
            _atomic_json(audit_path, payload)

        return self._execute(
            task,
            "knowledge_post",
            fingerprint,
            write_audit,
            current_validator=lambda: _json_file_equals(audit_path, payload),
        )

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
