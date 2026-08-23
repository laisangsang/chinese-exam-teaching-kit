from pathlib import Path
import tomllib

from chinese_exam_kit import __version__
from chinese_exam_kit.cli import main


def test_public_version_and_help(capsys):
    assert __version__ == "0.1.0"
    assert main(["--version"]) == 0
    assert f"cekit {__version__}" in capsys.readouterr().out


def test_version_output_uses_the_imported_package_version(monkeypatch, capsys):
    monkeypatch.setattr("chinese_exam_kit.cli.__version__", "0.1.1")
    assert main(["--version"]) == 0
    assert "cekit 0.1.1" in capsys.readouterr().out


def test_gitignore_excludes_private_media_and_office_documents():
    ignored_patterns = (Path(__file__).parents[1] / ".gitignore").read_text().splitlines()

    for extension in (
        "*.mov",
        "*.avi",
        "*.mkv",
        "*.webm",
        "*.ppt",
        "*.pptx",
        "*.xls",
        "*.xlsx",
        "*.doc",
    ):
        assert extension in ignored_patterns


def test_build_metadata_uses_pep639_and_one_declared_version_source():
    root = Path(__file__).parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["build-system"]["requires"][0].startswith("setuptools>=77")
    assert metadata["project"]["license"] == "Apache-2.0"
    assert metadata["project"]["license-files"] == ["LICENSE", "NOTICE"]
    assert '__version__ = "0.1.0"' not in (
        root / "src/chinese_exam_kit/__init__.py"
    ).read_text(encoding="utf-8")


def test_release_audit_builds_and_smokes_wheel_and_sdist_noneditable():
    root = Path(__file__).parents[1]
    workflow = (root / ".github/workflows/release-audit.yml").read_text(
        encoding="utf-8"
    )
    smoke = root / "scripts/noneditable_smoke.py"

    assert smoke.is_file()
    assert '"python-version": "3.11"' in workflow
    assert "python -m build" in workflow
    assert "dist/*.whl dist/*.tar.gz" in workflow
    assert "pip\", \"install" in smoke.read_text(encoding="utf-8")
