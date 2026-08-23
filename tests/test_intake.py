import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from chinese_exam_kit.pipeline import intake
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


def test_archive_inputs_is_idempotent_and_repairs_missing_or_corrupt_archives(tmp_path):
    source = tmp_path / "paper.md"
    source.write_text("可恢复材料", encoding="utf-8")
    layout, task = _new_task(tmp_path)
    first = archive_inputs(task, (source,))
    record = first.materials[0]
    destination = layout.root / record.archived_path

    destination.unlink()
    repaired_missing = archive_inputs(first, (source,))
    assert destination.read_text(encoding="utf-8") == "可恢复材料"
    assert repaired_missing.materials[0].duplicate_sources == (record.archived_path,)

    retried = archive_inputs(repaired_missing, (source,))
    assert retried.materials[0].duplicate_sources == (record.archived_path,)

    destination.write_text("损坏内容", encoding="utf-8")
    repaired_corrupt = archive_inputs(retried, (source,))
    assert destination.read_text(encoding="utf-8") == "可恢复材料"
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == record.sha256
    assert repaired_corrupt.materials[0].duplicate_sources == (record.archived_path,)
    assert not any(child.name.endswith(".partial") for child in layout.inputs.iterdir())

    legacy_record = replace(
        repaired_corrupt.materials[0],
        duplicate_sources=(record.archived_path, record.archived_path),
    )
    legacy_task = replace(repaired_corrupt, materials=(legacy_record,))
    normalized = archive_inputs(legacy_task, (source,))
    assert normalized.materials[0].duplicate_sources == (record.archived_path,)


def test_archive_digest_matches_final_bytes_and_source_is_read_only_once(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "paper.txt"
    source.write_bytes(b"before")
    layout, task = _new_task(tmp_path)
    original_open = Path.open
    source_read_count = 0

    def count_source_reads(path, mode="r", *args, **kwargs):
        nonlocal source_read_count
        if path == source and mode == "rb":
            source_read_count += 1
            if source_read_count > 1:
                raise AssertionError("source file was read again to decide its digest")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", count_source_reads)

    updated = archive_inputs(task, (source,))
    record = updated.materials[0]
    archived = layout.root / record.archived_path

    assert source_read_count == 1
    assert record.sha256 == hashlib.sha256(archived.read_bytes()).hexdigest()
    assert record.size_bytes == archived.stat().st_size


def test_failed_atomic_repair_keeps_existing_corrupt_target(tmp_path, monkeypatch):
    source = tmp_path / "paper.txt"
    source.write_bytes(b"correct source")
    layout, task = _new_task(tmp_path)
    archived_task = archive_inputs(task, (source,))
    destination = layout.root / archived_task.materials[0].archived_path
    corrupt_bytes = b"existing corrupt target"
    destination.write_bytes(corrupt_bytes)
    replace_calls = []

    def fail_replace(staged, target):
        replace_calls.append((Path(staged), Path(target)))
        raise OSError("simulated atomic publish failure")

    monkeypatch.setattr(intake.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated atomic publish failure"):
        archive_inputs(archived_task, (source,))

    assert len(replace_calls) == 1
    staged, target = replace_calls[0]
    assert staged.parent == layout.inputs
    assert target == destination
    assert destination.read_bytes() == corrupt_bytes
    assert not staged.exists()
    assert not any(child.name.endswith(".partial") for child in layout.inputs.iterdir())


def test_archive_copies_through_a_same_directory_temp_and_cleans_it_on_failure(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "paper.txt"
    source.write_bytes(b"source bytes")
    layout, task = _new_task(tmp_path)
    original_open = Path.open
    saw_same_directory_temp = False

    class FailingReader:
        def __init__(self):
            self._file = original_open(source, "rb")
            self._reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._file.close()

        def read(self, size=-1):
            nonlocal saw_same_directory_temp
            self._reads += 1
            if self._reads == 1:
                return self._file.read(2)
            saw_same_directory_temp = any(
                child.name.startswith(".material-") and child.name.endswith(".partial")
                for child in layout.inputs.iterdir()
            )
            raise OSError("simulated interrupted read")

    def failing_source_open(path, mode="r", *args, **kwargs):
        if path == source and mode == "rb":
            return FailingReader()
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_source_open)

    with pytest.raises(OSError, match="simulated interrupted read"):
        archive_inputs(task, (source,))

    assert saw_same_directory_temp
    assert not any(child.name.endswith(".partial") for child in layout.inputs.iterdir())
    assert not any(child.name.startswith("material-") for child in layout.inputs.iterdir())


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


def test_archive_lock_windows_branch_is_injectable_and_always_unlocks(tmp_path):
    archive_dir = tmp_path / "inputs"

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        def __init__(self):
            self.calls = []

        def locking(self, descriptor, operation, size):
            self.calls.append((descriptor, operation, size))

    windows_api = FakeMsvcrt()

    with pytest.raises(RuntimeError, match="inside lock"):
        with intake._archive_lock(
            archive_dir,
            platform_name="nt",
            windows_api=windows_api,
        ):
            raise RuntimeError("inside lock")

    assert [operation for _, operation, _ in windows_api.calls] == [
        windows_api.LK_LOCK,
        windows_api.LK_UNLCK,
    ]
    assert all(size == 1 for _, _, size in windows_api.calls)


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("参考答案与评分标准", "answer_candidate"),
        ("高中语文试卷 选择题", "exam_candidate"),
        ("这是无标记的公开文档", "document_unknown"),
    ),
)
def test_pdf_auto_classification_has_direct_answer_exam_and_unknown_branches(
    tmp_path, monkeypatch, text, expected
):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(intake, "_first_two_pdf_pages_text", lambda _: text)

    assert classify_material(source) == expected


def test_pdf_auto_classification_degrades_parse_failure_to_unknown(tmp_path):
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"not a PDF")

    assert classify_material(source) == "document_unknown"


def test_archive_inputs_explicit_roles_override_filename_and_pdf_guessing(tmp_path):
    exam = tmp_path / "参考答案.pdf"
    answer = tmp_path / "语文试卷.md"
    exam.write_bytes(b"not parsed because role is explicit")
    answer.write_text("名称不决定角色", encoding="utf-8")
    _, task = _new_task(tmp_path)

    updated = archive_inputs(task, (exam, answer), roles=("exam", "answer"))

    assert [record.material_type for record in updated.materials] == [
        "exam_candidate",
        "answer_candidate",
    ]


def test_archive_inputs_rejects_explicit_role_for_non_document_without_copying(tmp_path):
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"video")
    layout, task = _new_task(tmp_path)

    with pytest.raises(ValueError, match="document"):
        archive_inputs(task, (source,), roles=("exam",))

    assert list(layout.inputs.iterdir()) == []
