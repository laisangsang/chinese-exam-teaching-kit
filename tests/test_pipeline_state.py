import json
from dataclasses import FrozenInstanceError

import pytest

from chinese_exam_kit.pipeline.models import MaterialRecord, PipelineTask, STAGES, StageRecord
from chinese_exam_kit.pipeline.state import load_task, save_task, transition_stage
from chinese_exam_kit.workspace import WorkspaceLayout
from tests._host_samples import posix_path


def _new_task(tmp_path):
    layout = WorkspaceLayout.create(tmp_path, "demo")
    return layout, PipelineTask.create("demo", "原创示例", layout.root)


def test_new_task_uses_public_stages_without_git_commit(tmp_path):
    _, task = _new_task(tmp_path)

    assert tuple(task.stages) == STAGES == (
        "intake",
        "extract",
        "media",
        "knowledge_pre",
        "analysis",
        "delivery",
        "knowledge_post",
        "verification",
    )
    assert all(record.status == "pending" for record in task.stages.values())
    assert "git_commit" not in task.stages


def test_task_and_stage_records_are_immutable(tmp_path):
    _, task = _new_task(tmp_path)

    with pytest.raises(FrozenInstanceError):
        task.title = "changed"
    with pytest.raises(TypeError):
        task.stages["intake"] = task.stages["intake"]


def test_transition_stage_records_attempt_and_rejects_illegal_transition(tmp_path):
    _, task = _new_task(tmp_path)

    running = transition_stage(task, "intake", "running")
    completed = transition_stage(running, "intake", "completed")

    assert task.stages["intake"].status == "pending"
    assert completed.stages["intake"].status == "completed"
    assert completed.stages["intake"].attempts == 1
    assert completed.stages["intake"].events[-1] == {
        "event": "transition",
        "from": "running",
        "to": "completed",
    }
    with pytest.raises(ValueError, match="Illegal stage transition"):
        transition_stage(task, "intake", "completed")


def test_failed_stage_invalidates_downstream_records(tmp_path):
    _, task = _new_task(tmp_path)
    task = transition_stage(task, "analysis", "running")
    task = transition_stage(task, "analysis", "completed")
    task = transition_stage(task, "delivery", "running")
    task = transition_stage(task, "delivery", "completed")
    task = transition_stage(task, "analysis", "running")

    failed = transition_stage(task, "analysis", "failed")

    assert failed.stages["analysis"].status == "failed"
    assert failed.stages["delivery"].status == "pending"
    assert failed.stages["delivery"].events[-1]["event"] == "invalidated"


def test_save_and_load_derive_workspace_without_persisting_absolute_path(tmp_path):
    layout, task = _new_task(tmp_path)

    path = save_task(task)
    payload = path.read_text(encoding="utf-8")
    loaded = load_task(path)

    assert path == layout.root / "task.json"
    assert str(tmp_path) not in payload
    assert "workspace" not in json.loads(payload)
    assert loaded == task


def test_pipeline_task_rejects_workspace_outside_dot_local_tasks(tmp_path):
    with pytest.raises(ValueError, match=r"\.local/tasks"):
        PipelineTask.create("demo", "原创示例", tmp_path / "demo")


@pytest.mark.parametrize(
    "archived_path",
    (
        "../secret.txt",
        posix_path("tmp", "secret.txt"),
        "C:" + "/secret.txt",
        "work/file.txt",
    ),
)
def test_material_record_rejects_paths_outside_task_inputs(archived_path):
    with pytest.raises(ValueError, match="task-relative inputs"):
        MaterialRecord(
            archived_path=archived_path,
            sha256="0" * 64,
            material_type="document",
        )


def test_save_task_rechecks_workspace_containment_before_writing(tmp_path):
    layout, task = _new_task(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    layout.inputs.rmdir()
    layout.root.rmdir()
    layout.root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        save_task(task)

    assert not (outside / "task.json").exists()


def test_save_task_does_not_follow_a_preexisting_partial_symlink(tmp_path):
    layout, task = _new_task(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("do not overwrite", encoding="utf-8")
    (layout.root / "task.json.partial").symlink_to(outside)

    saved = save_task(task)

    assert saved.is_file()
    assert outside.read_text(encoding="utf-8") == "do not overwrite"


@pytest.mark.parametrize(
    "unsafe",
    (
        {"event": "bad", "value": float("nan")},
        {"event": "bad", "value": {1, 2}},
        {"event": "bad", "value": object()},
        {1: "non-string key"},
    ),
)
def test_stage_event_values_are_json_safe_at_model_construction(unsafe):
    with pytest.raises(ValueError, match="JSON-safe"):
        StageRecord(events=(unsafe,))
