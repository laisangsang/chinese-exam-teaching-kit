"""Create privacy-isolated local workspaces for pipeline tasks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceLayout:
    """Filesystem locations owned by one local pipeline task."""

    root: Path
    inputs: Path

    @classmethod
    def create(cls, root: Path, slug: str) -> "WorkspaceLayout":
        """Create a task workspace below ``<root>/.local/tasks``.

        ``slug`` is deliberately a single path component.  This prevents a
        caller-controlled title or identifier from escaping the private task
        area through absolute paths or parent-directory traversal.
        """
        project_root = Path(root)
        if project_root.exists() and not project_root.is_dir():
            raise ValueError("project root must be a directory")
        if (
            not isinstance(slug, str)
            or not slug
            or slug != slug.strip()
            or slug in {".", ".."}
            or "/" in slug
            or "\\" in slug
            or "\x00" in slug
        ):
            raise ValueError("workspace slug must be one safe path component")

        tasks_root = project_root / ".local" / "tasks"
        workspace_root = tasks_root / slug
        inputs = workspace_root / "inputs"
        managed_paths = (project_root / ".local", tasks_root, workspace_root, inputs)
        if any(path.is_symlink() for path in managed_paths):
            raise ValueError("workspace symlink would escape the managed task area")
        if (
            not tasks_root.resolve().is_relative_to(project_root.resolve())
            or not workspace_root.resolve().is_relative_to(tasks_root.resolve())
        ):
            raise ValueError("workspace slug escapes .local/tasks")

        inputs.mkdir(parents=True, exist_ok=True)
        if not inputs.resolve().is_relative_to(workspace_root.resolve()):
            raise ValueError("workspace inputs escape the task directory")
        return cls(root=workspace_root, inputs=inputs)
