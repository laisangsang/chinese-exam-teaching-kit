"""Agent-neutral, resumable orchestration for the public local pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from chinese_exam_kit.content.docx import build_all
from chinese_exam_kit.content.validate import validate_content_dir
from chinese_exam_kit.delivery import (
    DeliveryManifest,
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
        records = tuple(
            record for record in task.materials if record.material_type != "answer_candidate"
        )
        fingerprint = _materials_fingerprint(records)

        def validate_archives() -> None:
            for record in records:
                archived = task.workspace / record.archived_path
                if archived.is_symlink() or not archived.is_file():
                    raise ValueError("archived input is missing or unsafe")
                if sha256_file(archived) != record.sha256:
                    raise ValueError("archived input digest mismatch")

        return self._execute(task, "intake", fingerprint, validate_archives)

    def _extract(self, task: PipelineTask) -> PipelineTask:
        records = tuple(
            record for record in task.materials if record.material_type in DOCUMENT_TYPES
        )
        fingerprint = _materials_fingerprint(records)

        def extract() -> None:
            base = task.workspace / "artifacts" / "extract"
            for record in records:
                source = task.workspace / record.archived_path
                result = extract_document(source)
                write_extraction_artifacts(result, base / record.sha256[:16])

        return self._execute(task, "extract", fingerprint, extract)

    def _media(self, task: PipelineTask) -> PipelineTask:
        records = tuple(record for record in task.materials if record.material_type in MEDIA_TYPES)
        fingerprint = _materials_fingerprint(records)
        current = task.stages["media"]
        if _is_current(current, fingerprint, {"completed", "degraded"}):
            return task
        receipt_path = task.workspace / "media" / "receipt.json"
        index_path = task.workspace / "media" / "index.json"
        receipt = _valid_media_receipt(receipt_path, index_path, fingerprint)
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
                result = self.media_provider.process(paths, task.workspace / "media")
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

        def write_audit() -> None:
            _atomic_json(
                task.workspace / "knowledge" / "pre_audit.json",
                {
                    "schema_version": 1,
                    "status": "ready_for_question_level_retrieval",
                    "note": "分析智能体应按知识库规范执行分析前检索和逐题调用。",
                },
            )

        return self._execute(task, "knowledge_pre", fingerprint, write_audit)

    def _analysis(self, task: PipelineTask) -> tuple[PipelineTask, bool]:
        content_dir = task.workspace / "content"
        fingerprint = _content_fingerprint(content_dir)
        current = task.stages["analysis"]
        if _is_current(current, fingerprint, {"completed"}):
            return task, True
        issues = validate_content_dir(content_dir)
        if issues:
            if not _is_current(current, fingerprint, {"waiting"}):
                _write_analysis_work_order(task.workspace)
                _atomic_json(
                    task.workspace / "work_orders" / "analysis-validation.json",
                    {"issues": [issue.to_dict() for issue in issues], "schema_version": 1},
                )
                task = _wait(task, "analysis", fingerprint)
                save_task(task)
            return task, False

        def record_validated_source() -> None:
            _atomic_json(
                task.workspace / "artifacts" / "analysis.json",
                {
                    "schema_version": 1,
                    "status": "validated",
                    "content_fingerprint": fingerprint,
                },
            )

        return self._execute(task, "analysis", fingerprint, record_validated_source), True

    def _delivery(self, task: PipelineTask) -> PipelineTask:
        fingerprint = _content_fingerprint(task.workspace / "content")

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

        return self._execute(task, "delivery", fingerprint, build)

    def _knowledge_post(self, task: PipelineTask) -> PipelineTask:
        fingerprint = _content_fingerprint(task.workspace / "content")

        def write_audit() -> None:
            _atomic_json(
                task.workspace / "knowledge" / "post_audit.json",
                {
                    "schema_version": 1,
                    "status": "review_required",
                    "note": "新发现只能进入候选状态；不得自动升级为稳定规则。",
                },
            )

        return self._execute(task, "knowledge_post", fingerprint, write_audit)

    def _verification(self, task: PipelineTask) -> PipelineTask:
        manifest = task.workspace / "output" / "delivery.json"
        fingerprint = sha256_file(manifest)

        def record() -> None:
            _atomic_json(
                task.workspace / "output" / "verification.json",
                {
                    "schema_version": 1,
                    "content_validation": "passed",
                    "visual_review": "evidence_ready",
                },
            )

        return self._execute(task, "verification", fingerprint, record)

    def _execute(
        self,
        task: PipelineTask,
        stage: str,
        fingerprint: str,
        action: Callable[[], None],
    ) -> PipelineTask:
        if _is_current(task.stages[stage], fingerprint, {"completed"}):
            return task
        task = _begin(task, stage)
        save_task(task)
        try:
            action()
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
        relative = absolute.relative_to(root)
    except ValueError:
        raise ValueError("task path must be inside the current project") from None
    if len(relative.parts) != 4 or relative.parts[:2] != (".local", "tasks"):
        raise ValueError("task path must match .local/tasks/SLUG/task.json")
    if not absolute.is_file():
        raise ValueError("task.json is unavailable")
    return absolute


@contextmanager
def task_lock(workspace: Path) -> Iterator[None]:
    lock_path = Path(workspace) / ".pipeline.lock"
    if lock_path.is_symlink():
        raise ValueError("pipeline lock cannot be a symlink")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
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
    return any(
        event.get("event") == "stage_result"
        and event.get("fingerprint") == fingerprint
        and event.get("status") == record.status
        for event in reversed(record.events)
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


def _write_analysis_work_order(workspace: Path) -> None:
    _atomic_text(
        workspace / "work_orders" / "analysis.md",
        """# 分析源稿工作单

当前流水线已完成本地材料归档与提取，并暂停在分析阶段。

任何能够编辑文件的智能体，都可以依据项目根目录中的 `AGENTS.md`、`README.md` 和公开内容合同编写 Markdown 源稿。请把正式源稿写入本任务的 `content/` 目录。整卷应覆盖实际存在的六个板块；单板块任务只完成对应板块，不得虚构缺失内容。

必须区分【官方评分参考】【文本推导】【教学拓展】，落实选择题逐项证据、主观题答案生成和各板块硬性合同。没有正式答案时仍按【文本推导】继续，并明确答案边界。完成后重新运行 `cekit run --task .local/tasks/SLUG/task.json`。

本工作单不调用外部大模型 API，不绑定任何专有智能体语法，也不代表分析已经完成。
""",
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
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("schema_version") != 1
        or payload.get("fingerprint") != fingerprint
        or payload.get("status") not in {"completed", "degraded"}
        or payload.get("index_sha256") != sha256_file(index_path)
    ):
        return None
    return {"status": str(payload["status"])}
