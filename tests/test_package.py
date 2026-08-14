from pathlib import Path

from chinese_exam_kit import __version__
from chinese_exam_kit.cli import main


def test_public_version_and_help(capsys):
    assert __version__ == "0.1.0"
    assert main(["--version"]) == 0
    assert "cekit 0.1.0" in capsys.readouterr().out


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
