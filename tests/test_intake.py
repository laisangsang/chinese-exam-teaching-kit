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

    legacy_record = replace(
        repaired_corrupt.materials[0],
        duplicate_sources=(record.archived_path, record.archived_path),
    )
    legacy_task = replace(repaired_corrupt, materials=(legacy_record,))
    normalized = archive_inputs(legacy_task, (source,))
    assert normalized.materials[0].duplicate_sources == (record.archived_path,)


def test_archive_digest_matches_bytes_when_source_would_change_between_hash_and_copy(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "paper.txt"
    source.write_bytes(b"before")
    layout, task = _new_task(tmp_path)
    original_sha256_file = intake.sha256_file
    source_mutated = False

    def hash_then_mutate(path):
        nonlocal source_mutated
        digest = original_sha256_file(path)
        if Path(path) == source and not source_mutated:
            source.write_bytes(b"after")
            source_mutated = True
        return digest

    monkeypatch.setattr(intake, "sha256_file", hash_then_mutate)

    updated = archive_inputs(task, (source,))
    record = updated.materials[0]
    archived = layout.root / record.archived_path

    assert record.sha256 == hashlib.sha256(archived.read_bytes()).hexdigest()
    assert record.size_bytes == archived.stat().st_size


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
