import json
import hashlib
import errno
import os
import socket
import zipfile
from pathlib import Path

import pytest

from chinese_exam_kit.pipeline.answers import attach_reference_answers
from chinese_exam_kit.pipeline.intake import archive_inputs
from chinese_exam_kit.pipeline.models import PipelineTask
from chinese_exam_kit.pipeline.runner import PipelineRunner
from chinese_exam_kit.pipeline.state import load_task, save_task
from chinese_exam_kit.workspace import WorkspaceLayout


def _create_task(project: Path, *, media: bool = False) -> Path:
    exam = project / "原创试卷.md"
    exam.write_text("# 原创试卷\n\n1. 原创题目。\n", encoding="utf-8")
    inputs = [exam]
    if media:
        video = project / "原创讲解.mp4"
        video.write_bytes(b"local-media")
        inputs.append(video)
    layout = WorkspaceLayout.create(project, "original-demo")
    task = PipelineTask.create("original-demo", "原创示例", layout.root)
    return save_task(archive_inputs(task, inputs))


def _write_valid_analysis(task_path: Path) -> Path:
    content = task_path.parent / "content"
    content.mkdir(parents=True, exist_ok=True)
    source = content / "00_整卷总览与讲评建议.md"
    source.write_text(
        "# 原创试卷讲评总览\n\n本稿依据原创题面，供教师进行课堂讲评与迁移训练。\n",
        encoding="utf-8",
    )
    return source


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read("word/document.xml").decode("utf-8")


class UnavailableMediaProvider:
    def process(self, media_paths, output_dir):
        from chinese_exam_kit.media.learning import MediaLearningResult

        return MediaLearningResult.degraded("本地媒体工具不可用")


def test_analysis_stage_pauses_with_agent_neutral_work_order(tmp_path):
    task_path = _create_task(tmp_path)

    summary = PipelineRunner(tmp_path).resume(task_path)

    work_order = task_path.parent / "work_orders" / "analysis.md"
    assert summary.status == "needs_user_input"
    assert summary.stage("analysis").status == "waiting"
    assert work_order.is_file()
    text = work_order.read_text(encoding="utf-8")
    assert "任何能够编辑文件的智能体" in text
    assert "六个板块" in text
    assert "Codex" not in text


def test_valid_agent_authored_markdown_resumes_and_builds_delivery(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.run(task_path)
    _write_valid_analysis(task_path)

    summary = runner.resume(task_path)

    assert summary.status == "completed"
    assert summary.stage("analysis").status == "completed"
    assert summary.stage("delivery").status == "completed"
    assert (task_path.parent / "output" / "docx" / "00_整卷总览与讲评建议.docx").is_file()
    manifest = json.loads((task_path.parent / "output" / "delivery.json").read_text(encoding="utf-8"))
    assert manifest["visual_status"] == "evidence_ready"
    assert all(not Path(item).is_absolute() for item in manifest["outputs"] + manifest["evidence"])


def test_resume_is_idempotent_for_completed_stages_and_artifacts(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    _write_valid_analysis(task_path)
    runner.resume(task_path)
    first = load_task(task_path).to_dict()
    output = task_path.parent / "output" / "docx" / "00_整卷总览与讲评建议.docx"
    first_mtime = output.stat().st_mtime_ns

    runner.resume(task_path)

    assert load_task(task_path).to_dict() == first
    assert output.stat().st_mtime_ns == first_mtime


def test_changed_analysis_invalidates_completed_delivery_and_waits_for_repair(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    source = _write_valid_analysis(task_path)
    assert runner.resume(task_path).status == "completed"

    source.write_text("TODO\n", encoding="utf-8")
    summary = runner.resume(task_path)

    assert summary.status == "needs_user_input"
    assert summary.stage("analysis").status == "waiting"
    assert load_task(task_path).stages["delivery"].status == "pending"


def test_stage_currentness_uses_latest_result_when_content_returns_a_to_b_to_a(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    source = _write_valid_analysis(task_path)
    source.write_text("# 版本A\n\n这是通过校验的原创版本A内容。\n", encoding="utf-8")
    runner.resume(task_path)
    output = task_path.parent / "output" / "docx" / "00_整卷总览与讲评建议.docx"
    assert "版本A" in _docx_text(output)

    source.write_text("# 版本B\n\n这是通过校验的原创版本B内容。\n", encoding="utf-8")
    runner.resume(task_path)
    assert "版本B" in _docx_text(output)
    source.write_text("# 版本A\n\n这是通过校验的原创版本A内容。\n", encoding="utf-8")

    runner.resume(task_path)

    assert "版本A" in _docx_text(output)
    assert "版本B" not in _docx_text(output)


def test_running_stage_recovers_after_completion_write_crash(tmp_path, monkeypatch):
    task_path = _create_task(tmp_path)
    import chinese_exam_kit.pipeline.runner as runner_module

    real_save = runner_module.save_task
    failed = False

    def crash_once(task):
        nonlocal failed
        if not failed and task.stages["extract"].status == "completed":
            failed = True
            raise OSError("simulated crash")
        return real_save(task)

    monkeypatch.setattr(runner_module, "save_task", crash_once)
    with pytest.raises(OSError, match="simulated crash"):
        PipelineRunner(tmp_path).resume(task_path)
    assert load_task(task_path).stages["extract"].status == "running"

    monkeypatch.setattr(runner_module, "save_task", real_save)
    summary = PipelineRunner(tmp_path).resume(task_path)

    assert summary.stage("extract").status == "completed"
    assert summary.stage("analysis").status == "waiting"


def test_missing_video_tools_degrades_without_blocking_exam(tmp_path):
    task_path = _create_task(tmp_path, media=True)
    summary = PipelineRunner(tmp_path, media_provider=UnavailableMediaProvider()).resume(task_path)

    assert summary.stage("media").status == "degraded"
    assert summary.stage("analysis").status == "waiting"
    assert summary.status == "needs_user_input"


def test_media_receipt_prevents_duplicate_provider_work_after_state_write_crash(
    tmp_path, monkeypatch
):
    task_path = _create_task(tmp_path, media=True)
    calls = 0

    class CountingProvider:
        def process(self, media_paths, output_dir):
            nonlocal calls
            calls += 1
            from chinese_exam_kit.media.learning import MediaLearningResult

            return MediaLearningResult.completed()

    import chinese_exam_kit.pipeline.runner as runner_module

    real_save = runner_module.save_task
    failed = False

    def fail_media_completion_once(task):
        nonlocal failed
        if not failed and task.stages["media"].status == "completed":
            failed = True
            raise OSError("simulated media state crash")
        return real_save(task)

    monkeypatch.setattr(runner_module, "save_task", fail_media_completion_once)
    with pytest.raises(OSError, match="simulated media state crash"):
        PipelineRunner(tmp_path, media_provider=CountingProvider()).resume(task_path)

    monkeypatch.setattr(runner_module, "save_task", real_save)
    PipelineRunner(tmp_path, media_provider=CountingProvider()).resume(task_path)

    assert calls == 1


def test_missing_media_index_forces_only_media_rebuild(tmp_path):
    task_path = _create_task(tmp_path, media=True)
    calls = 0

    class CountingProvider:
        def process(self, media_paths, output_dir):
            nonlocal calls
            calls += 1
            from chinese_exam_kit.media.learning import MediaLearningResult

            return MediaLearningResult.degraded("本地能力不可用")

    runner = PipelineRunner(tmp_path, media_provider=CountingProvider())
    runner.resume(task_path)
    extract_events = load_task(task_path).stages["extract"].events
    (task_path.parent / "media" / "index.json").unlink()

    runner.resume(task_path)

    assert calls == 2
    assert load_task(task_path).stages["extract"].events == extract_events


def test_media_directory_symlink_is_replaced_without_touching_external_target(
    tmp_path,
):
    task_path = _create_task(tmp_path, media=True)
    runner = PipelineRunner(tmp_path, media_provider=UnavailableMediaProvider())
    runner.resume(task_path)
    media = task_path.parent / "media"
    outside = tmp_path / "outside-media"
    media.rename(outside)
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    media.symlink_to(outside, target_is_directory=True)

    summary = runner.resume(task_path)

    assert summary.stage("media").status == "degraded"
    assert media.is_dir() and not media.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert (media / "index.json").is_file()


def test_missing_extract_artifact_is_rebuilt_without_repeating_intake(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    task = load_task(task_path)
    intake_events = task.stages["intake"].events
    artifact = next((task_path.parent / "artifacts" / "extract").rglob("question_text.md"))
    artifact.unlink()

    runner.resume(task_path)

    assert artifact.is_file()
    assert load_task(task_path).stages["intake"].events == intake_events


def test_extract_receipt_never_trusts_artifacts_reached_through_nested_symlink(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    task = load_task(task_path)
    digest_dir = task_path.parent / "artifacts" / "extract" / task.materials[0].sha256[:16]
    outside = tmp_path / "outside-extract"
    digest_dir.rename(outside)
    digest_dir.symlink_to(outside, target_is_directory=True)
    before = {path.name: path.read_bytes() for path in outside.iterdir()}

    with pytest.raises(ValueError, match="extract.*symlink"):
        runner.resume(task_path)

    assert {path.name: path.read_bytes() for path in outside.iterdir()} == before
    assert load_task(task_path).stages["extract"].status == "failed"


def test_missing_knowledge_audits_are_rebuilt_at_their_own_stages(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    pre = task_path.parent / "knowledge" / "pre_audit.json"
    pre.unlink()

    runner.resume(task_path)
    assert pre.is_file()
    _write_valid_analysis(task_path)
    runner.resume(task_path)
    post = task_path.parent / "knowledge" / "post_audit.json"
    post.unlink()

    runner.resume(task_path)

    assert post.is_file()


def test_tampered_waiting_validation_report_is_repaired_without_new_stage_event(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    before = load_task(task_path).stages["analysis"].events
    report = task_path.parent / "work_orders" / "analysis-validation.json"
    report.write_text('{"leak":"/Users/Alice Smith/exam.pdf"}', encoding="utf-8")

    runner.resume(task_path)

    assert "/Users/" not in report.read_text(encoding="utf-8")
    assert load_task(task_path).stages["analysis"].events == before


def test_missing_archive_never_remains_falsely_completed(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    task = load_task(task_path)
    (task.workspace / task.materials[0].archived_path).unlink()

    with pytest.raises(ValueError, match="archived input"):
        runner.resume(task_path)

    assert load_task(task_path).stages["intake"].status == "failed"


@pytest.mark.parametrize("damage", ("missing", "tampered"))
def test_answer_archive_integrity_is_checked_without_using_original_source(
    tmp_path, damage
):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    answer = tmp_path / "原创参考答案.md"
    answer.write_text("# 参考答案\n\n1. A。\n", encoding="utf-8")
    attachment = attach_reference_answers(task_path, [answer])
    task = load_task(task_path)
    answer_record = next(
        record for record in task.materials if record.sha256 in attachment.added_sha256
    )
    archived = task.workspace / answer_record.archived_path
    if damage == "missing":
        archived.unlink()
    else:
        archived.write_text("篡改后的答案", encoding="utf-8")
    original_bytes = answer.read_bytes()

    with pytest.raises(ValueError, match="archived input") as captured:
        runner.resume(task_path)

    failed = load_task(task_path)
    assert failed.stages["intake"].status == "failed"
    assert failed.stages["extract"].status != "completed"
    assert answer.read_bytes() == original_bytes
    assert str(tmp_path) not in str(captured.value)


def test_inputs_directory_symlink_is_never_followed_to_validate_archives(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    task = load_task(task_path)
    inputs = task.workspace / "inputs"
    outside = tmp_path / "outside-inputs"
    inputs.rename(outside)
    inputs.symlink_to(outside, target_is_directory=True)
    before = {
        path.name: path.read_bytes() for path in outside.iterdir() if path.is_file()
    }

    with pytest.raises(ValueError, match="archived input"):
        runner.resume(task_path)

    after = {
        path.name: path.read_bytes() for path in outside.iterdir() if path.is_file()
    }
    assert after == before
    assert inputs.is_symlink()
    assert load_task(task_path).stages["intake"].status == "failed"


def test_attaching_new_answers_invalidates_dependents_but_never_media(tmp_path):
    task_path = _create_task(tmp_path, media=True)
    runner = PipelineRunner(tmp_path, media_provider=UnavailableMediaProvider())
    runner.resume(task_path)
    before = load_task(task_path)
    media_events = before.stages["media"].events
    answer = tmp_path / "原创参考答案.md"
    answer.write_text("# 参考答案\n\n1. A。\n", encoding="utf-8")

    changed = attach_reference_answers(task_path, [answer])
    duplicate = attach_reference_answers(task_path, [answer])

    assert changed.changed is True
    assert duplicate.changed is False
    updated = load_task(task_path)
    assert updated.stages["media"].events == media_events
    assert updated.stages["media"].status == "degraded"
    for name in ("extract", "knowledge_pre", "analysis", "delivery", "knowledge_post", "verification"):
        assert updated.stages[name].status == "pending"
    ledger = json.loads((task_path.parent / "answers" / "differences.json").read_text(encoding="utf-8"))
    assert len(ledger["versions"]) == 1
    assert str(answer) not in json.dumps(ledger, ensure_ascii=False)

    intake_events = updated.stages["intake"].events
    runner.resume(task_path)
    resumed = load_task(task_path)
    assert resumed.stages["intake"].events == intake_events
    assert resumed.stages["media"].events == media_events


def test_answer_revision_requires_content_bound_acknowledgement_before_delivery(tmp_path):
    task_path = _create_task(tmp_path, media=True)
    runner = PipelineRunner(tmp_path, media_provider=UnavailableMediaProvider())
    runner.resume(task_path)
    source = _write_valid_analysis(task_path)
    assert runner.resume(task_path).status == "completed"
    before_answer = load_task(task_path)
    media_events = before_answer.stages["media"].events
    old_analysis_fingerprint = next(
        event["fingerprint"]
        for event in reversed(before_answer.stages["analysis"].events)
        if event["event"] == "stage_result"
    )
    answer = tmp_path / "原创参考答案.md"
    answer.write_text("# 参考答案\n\n1. A。\n", encoding="utf-8")
    attach_reference_answers(task_path, [answer])

    waiting = runner.resume(task_path)

    assert waiting.status == "needs_user_input"
    waiting_task = load_task(task_path)
    new_analysis_fingerprint = next(
        event["fingerprint"]
        for event in reversed(waiting_task.stages["analysis"].events)
        if event["event"] == "stage_result"
    )
    assert new_analysis_fingerprint != old_analysis_fingerprint
    revision = json.loads(
        (task_path.parent / "answers" / "current_revision.json").read_text(encoding="utf-8")
    )["token"]
    work_order = (task_path.parent / "work_orders" / "analysis.md").read_text(encoding="utf-8")
    assert revision in work_order
    source.write_text(
        "# 修订后原创讲评\n\n本稿已依据后补正式答案重新核对并完成内容修订。\n",
        encoding="utf-8",
    )
    from chinese_exam_kit.pipeline.runner import _content_fingerprint

    (task_path.parent / "content" / "analysis-receipt.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "answer_revision": revision,
                "content_fingerprint": _content_fingerprint(task_path.parent / "content"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    completed = runner.resume(task_path)

    assert completed.status == "completed"
    assert load_task(task_path).stages["media"].events == media_events


def test_missing_answer_revision_marker_is_rebuilt_without_bypassing_wait(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    _write_valid_analysis(task_path)
    runner.resume(task_path)
    answer = tmp_path / "原创参考答案.md"
    answer.write_text("# 参考答案\n\n1. A。\n", encoding="utf-8")
    attach_reference_answers(task_path, [answer])
    marker = task_path.parent / "answers" / "current_revision.json"
    expected = json.loads(marker.read_text(encoding="utf-8"))["token"]
    marker.unlink()

    summary = runner.resume(task_path)

    assert summary.status == "needs_user_input"
    assert json.loads(marker.read_text(encoding="utf-8"))["token"] == expected


def test_missing_docx_or_tampered_delivery_receipt_forces_delivery_rebuild(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    _write_valid_analysis(task_path)
    runner.resume(task_path)
    output = task_path.parent / "output" / "docx" / "00_整卷总览与讲评建议.docx"
    output.unlink()

    runner.resume(task_path)
    assert output.is_file()
    receipt = task_path.parent / "output" / "delivery-receipt.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["outputs"][0]["sha256"] = "0" * 64
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    mtime = output.stat().st_mtime_ns

    runner.resume(task_path)

    assert output.stat().st_mtime_ns != mtime


def test_tampered_verification_content_status_is_rebuilt_to_passed(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    _write_valid_analysis(task_path)
    assert runner.resume(task_path).status == "completed"
    verification = task_path.parent / "output" / "verification.json"
    payload = json.loads(verification.read_text(encoding="utf-8"))
    payload["content_validation"] = "failed"
    verification.write_text(json.dumps(payload), encoding="utf-8")

    assert runner.resume(task_path).status == "completed"

    repaired = json.loads(verification.read_text(encoding="utf-8"))
    assert repaired["content_validation"] == "passed"
    assert repaired["visual_review"] == "evidence_ready"


@pytest.mark.parametrize("tampered", ("passed", "failed", "unknown"))
def test_tampered_verification_visual_status_never_remains_completed(
    tmp_path, tampered
):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    _write_valid_analysis(task_path)
    assert runner.resume(task_path).status == "completed"
    verification = task_path.parent / "output" / "verification.json"
    payload = json.loads(verification.read_text(encoding="utf-8"))
    payload["visual_review"] = tampered
    verification.write_text(json.dumps(payload), encoding="utf-8")

    assert runner.resume(task_path).status == "completed"

    repaired = json.loads(verification.read_text(encoding="utf-8"))
    manifest = json.loads(
        (task_path.parent / "output" / "delivery.json").read_text(encoding="utf-8")
    )
    assert repaired["visual_review"] == "evidence_ready"
    assert manifest["visual_status"] == repaired["visual_review"]


def test_answer_attachment_recovers_without_duplicate_ledger_after_state_write_crash(
    tmp_path, monkeypatch
):
    task_path = _create_task(tmp_path)
    answer = tmp_path / "原创参考答案.md"
    answer.write_text("# 参考答案\n\n1. B。\n", encoding="utf-8")
    import chinese_exam_kit.pipeline.answers as answers_module

    real_save = answers_module.save_task

    def fail_save(task):
        raise OSError("simulated state write crash")

    monkeypatch.setattr(answers_module, "save_task", fail_save)
    with pytest.raises(OSError, match="simulated state write crash"):
        attach_reference_answers(task_path, [answer])

    monkeypatch.setattr(answers_module, "save_task", real_save)
    result = attach_reference_answers(task_path, [answer])
    ledger = json.loads((task_path.parent / "answers" / "differences.json").read_text(encoding="utf-8"))

    assert result.changed is True
    assert len(ledger["versions"]) == 1
    assert load_task(task_path).materials[-1].material_type == "answer_candidate"


def test_answer_attachment_snapshots_previous_markdown_and_word_for_traceability(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    source = _write_valid_analysis(task_path)
    runner.resume(task_path)
    original_markdown = source.read_bytes()
    original_docx = (
        task_path.parent / "output" / "docx" / "00_整卷总览与讲评建议.docx"
    ).read_bytes()
    answer = tmp_path / "原创参考答案.md"
    answer.write_text("# 参考答案\n\n1. C。\n", encoding="utf-8")

    attachment = attach_reference_answers(task_path, [answer])
    revision = task_path.parent / "answers" / "revisions" / attachment.added_sha256[0][:16]
    manifest = json.loads((revision / "snapshot.json").read_text(encoding="utf-8"))

    assert (revision / "content" / source.name).read_bytes() == original_markdown
    assert (
        revision / "output" / "docx" / "00_整卷总览与讲评建议.docx"
    ).read_bytes() == original_docx
    assert all(not Path(item["path"]).is_absolute() for item in manifest["artifacts"])
    assert str(tmp_path) not in json.dumps(manifest, ensure_ascii=False)


def test_answer_snapshot_rejects_nested_symlink_escape(tmp_path):
    task_path = _create_task(tmp_path)
    runner = PipelineRunner(tmp_path)
    runner.resume(task_path)
    _write_valid_analysis(task_path)
    runner.resume(task_path)
    answer = tmp_path / "原创参考答案.md"
    answer.write_text("# 参考答案\n\n1. D。\n", encoding="utf-8")
    digest = hashlib.sha256(answer.read_bytes()).hexdigest()
    revision = task_path.parent / "answers" / "revisions" / digest[:16]
    revision.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (revision / "output").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink|unsafe"):
        attach_reference_answers(task_path, [answer])

    assert not any(outside.iterdir())


def test_runner_rejects_task_symlink_escape(tmp_path):
    task_path = _create_task(tmp_path)
    outside = tmp_path / "outside-task.json"
    outside.write_bytes(task_path.read_bytes())
    link = task_path.parent / "linked-task.json"
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="task.json|symlink"):
        PipelineRunner(tmp_path).resume(link)


def test_runner_rejects_parent_traversal_even_when_it_resolves_to_same_task(tmp_path):
    task_path = _create_task(tmp_path)
    (task_path.parent / "nested").mkdir()
    traversing = task_path.parent / "nested" / ".." / "task.json"

    with pytest.raises(ValueError, match="task path"):
        PipelineRunner(tmp_path).resume(traversing)


def test_runner_rejects_preexisting_pipeline_lock_symlink(tmp_path):
    task_path = _create_task(tmp_path)
    outside = tmp_path / "outside-lock"
    outside.write_text("do not touch", encoding="utf-8")
    (task_path.parent / ".pipeline.lock").symlink_to(outside)

    with pytest.raises(ValueError, match="lock.*symlink"):
        PipelineRunner(tmp_path).resume(task_path)

    assert outside.read_text(encoding="utf-8") == "do not touch"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX no-follow flag unavailable")
def test_pipeline_lock_uses_no_follow_and_redacts_eloop(tmp_path, monkeypatch):
    task_path = _create_task(tmp_path)
    import chinese_exam_kit.pipeline.runner as runner_module

    real_open = runner_module.os.open

    def simulated_race(path, flags, mode=0o777):
        if Path(path).name == ".pipeline.lock":
            assert flags & os.O_NOFOLLOW
            raise OSError(errno.ELOOP, "symlink race", str(path))
        return real_open(path, flags, mode)

    monkeypatch.setattr(runner_module.os, "open", simulated_race)

    with pytest.raises(ValueError, match="pipeline lock is unsafe") as captured:
        PipelineRunner(tmp_path).resume(task_path)
    assert str(tmp_path) not in str(captured.value)


def test_equivalent_macos_var_alias_task_path_is_accepted(tmp_path):
    task_path = _create_task(tmp_path)
    canonical = str(task_path)
    if not canonical.startswith("/private/var/") or not Path("/var").is_symlink():
        pytest.skip("macOS /var alias unavailable")
    alias = Path(canonical.replace("/private/var/", "/var/", 1))
    project_alias = Path(str(tmp_path).replace("/private/var/", "/var/", 1))

    summary = PipelineRunner(project_alias).resume(alias)

    assert summary.status == "needs_user_input"


@pytest.mark.parametrize("bad_paths", ("answer.md", b"answer.md", [object()]))
def test_answer_attachment_rejects_scalar_or_non_pathlike_inputs(tmp_path, bad_paths):
    task_path = _create_task(tmp_path)

    with pytest.raises(TypeError, match="path-like"):
        attach_reference_answers(task_path, bad_paths)


def test_pipeline_has_no_network_or_git_side_effects(tmp_path, monkeypatch):
    task_path = _create_task(tmp_path)
    git_sentinel = tmp_path / ".git" / "sentinel"
    git_sentinel.parent.mkdir()
    git_sentinel.write_text("unchanged", encoding="utf-8")

    def reject_network(*args, **kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    PipelineRunner(tmp_path).resume(task_path)

    assert git_sentinel.read_text(encoding="utf-8") == "unchanged"
