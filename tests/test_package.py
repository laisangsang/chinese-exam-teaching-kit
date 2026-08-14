from chinese_exam_kit import __version__
from chinese_exam_kit.cli import main


def test_public_version_and_help(capsys):
    assert __version__ == "0.1.0"
    assert main(["--version"]) == 0
    assert "cekit 0.1.0" in capsys.readouterr().out
