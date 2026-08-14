import json
import hashlib
from pathlib import Path

import pytest

from chinese_exam_kit.pipeline.intake import archive_inputs, classify_material
from chinese_exam_kit.pipeline.models import PipelineTask
from chinese_exam_kit.pipeline.state import save_task
from chinese_exam_kit.workspace import WorkspaceLayout


def _new_task(tmp_path):
    layout = WorkspaceLayout.create(tmp_path, "demo")
    return layout, PipelineTask.create("demo", "原创示例", layout.root)


def test_archive_inputs_deduplicates_by_sha256(tmp_path):
    source_a = tmp_path / "a.md"
    source_b = tmp_path / "b.md"
    source_a.write_text("原创材料", encoding="utf-8")
    source_b.write_text("原创材料", encoding="utf-8")
    layout, task = _new_task(tmp_path)

    updated = archive_inputs(task, (source_a, source_b))

    assert len(updated.materials) == 1
    assert len(updated.materials[0].duplicate_sources) == 1
    assert updated.materials[0].archived_path.startswith("inputs/")
    assert (layout.root / updated.materials[0].archived_path).read_text(encoding="utf-8") == "原创材料"


def test_task_json_never_persists_source_paths_or_original_names(tmp_path):
    source_a = tmp_path / "a.md"
    source_b = tmp_path / "b.md"
    source_a.write_text("原创材料", encoding="utf-8")
    source_b.write_text("原创材料", encoding="utf-8")
    layout, task = _new_task(tmp_path)

    updated = archive_inputs(task, (source_a, source_b))
    save_task(updated)
    payload = (layout.root / "task.json").read_text(encoding="utf-8")

    assert str(tmp_path) not in payload
    assert "a.md" not in payload
    assert "b.md" not in payload
    assert updated.materials[0].archived_path.startswith("inputs/")
    assert all(Path(path).is_absolute() is False for path in updated.materials[0].duplicate_sources)
    assert json.loads(payload)["materials"][0]["archived_path"].startswith("inputs/")


@pytest.mark.parametrize(
    ("filename", "expected"),
    (
        ("paper.md", "document"),
        ("paper.txt", "document"),
        ("paper.docx", "document"),
        ("scan.png", "image"),
        ("lesson.mp3", "audio"),
        ("lesson.mp4", "video"),
    ),
)
def test_classify_material_supports_public_input_families(tmp_path, filename, expected):
    source = tmp_path / filename
    source.write_bytes(b"fixture")

    assert classify_material(source) == expected


def test_archive_inputs_rejects_unsupported_files_without_copying(tmp_path):
    source = tmp_path / "secrets.zip"
    source.write_bytes(b"fixture")
    layout, task = _new_task(tmp_path)

    with pytest.raises(ValueError, match="不支持的材料类型"):
        archive_inputs(task, (source,))

    assert list(layout.inputs.iterdir()) == []


def test_archive_inputs_does_not_mutate_the_original_task(tmp_path):
    source = tmp_path / "paper.txt"
    source.write_text("text", encoding="utf-8")
    _, task = _new_task(tmp_path)

    updated = archive_inputs(task, (source,))

    assert task.materials == ()
    assert len(updated.materials) == 1


def test_archive_inputs_rechecks_destination_containment_before_copying(tmp_path):
    source = tmp_path / "paper.txt"
    source.write_text("text", encoding="utf-8")
    layout, task = _new_task(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    layout.inputs.rmdir()
    layout.inputs.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escape"):
        archive_inputs(task, (source,))

    assert list(outside.iterdir()) == []


def test_archive_inputs_rejects_a_symlinked_archive_file(tmp_path):
    source = tmp_path / "paper.txt"
    source.write_text("text", encoding="utf-8")
    layout, task = _new_task(tmp_path)
    digest = hashlib.sha256(b"text").hexdigest()
    outside = tmp_path / "outside.txt"
    outside.write_text("text", encoding="utf-8")
    destination = layout.inputs / f"material-{digest}.txt"
    destination.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        archive_inputs(task, (source,))


def test_archive_inputs_rejects_a_symlinked_lock_file(tmp_path):
    source = tmp_path / "paper.txt"
    source.write_text("text", encoding="utf-8")
    layout, task = _new_task(tmp_path)
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"0")
    (layout.inputs / ".archive-inputs.lock").symlink_to(outside)

    with pytest.raises(ValueError, match="lock.*symlink"):
        archive_inputs(task, (source,))
