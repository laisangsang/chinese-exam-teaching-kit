from pathlib import Path

import pytest

from chinese_exam_kit.workspace import WorkspaceLayout


def test_workspace_is_always_under_dot_local(tmp_path):
    layout = WorkspaceLayout.create(tmp_path, "demo")

    assert layout.root == tmp_path / ".local" / "tasks" / "demo"
    assert layout.inputs.is_relative_to(layout.root)
    assert layout.inputs.is_dir()


@pytest.mark.parametrize("slug", ("../escape", "nested/task", "/tmp/escape", ".", ""))
def test_workspace_rejects_slugs_that_can_escape_task_root(tmp_path, slug):
    with pytest.raises(ValueError, match="slug"):
        WorkspaceLayout.create(tmp_path, slug)


def test_workspace_rejects_a_non_directory_project_root(tmp_path):
    root = tmp_path / "project-file"
    root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="directory"):
        WorkspaceLayout.create(root, "demo")


def test_workspace_rejects_symlinked_local_directory_that_escapes_project(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".local").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escape"):
        WorkspaceLayout.create(tmp_path, "demo")


def test_workspace_rejects_symlinked_inputs_directory_that_escapes_task(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / ".local" / "tasks" / "demo"
    workspace.mkdir(parents=True)
    (workspace / "inputs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escape"):
        WorkspaceLayout.create(tmp_path, "demo")
