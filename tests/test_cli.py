import json
from pathlib import Path

from chinese_exam_kit.cli import main


def test_cli_init_run_status_json_is_agent_neutral_and_redacted(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "我的原创试卷.md"
    source.write_text("# 原创试卷\n\n1. 原创题目。\n", encoding="utf-8")

    assert main(["init", "--name", "原创示例", "--input", str(source)]) == 0
    init_output = capsys.readouterr().out.strip()
    assert init_output.startswith(".local/tasks/")
    task_path = tmp_path / init_output
    assert main(["run", "--task", init_output]) == 0
    capsys.readouterr()

    assert main(["status", "--task", init_output, "--json"]) == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    assert payload["status"] == "needs_user_input"
    assert payload["task"] == init_output
    assert str(tmp_path) not in raw
    assert "我的原创试卷.md" not in raw
    assert payload["stages"]["analysis"]["status"] == "waiting"
    assert task_path.is_file()


def test_cli_validate_returns_two_for_invalid_content(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    content = tmp_path / "content"
    content.mkdir()
    (content / "unknown.md").write_text("TODO\n", encoding="utf-8")

    code = main(["validate", "--content", "content"])

    assert code == 2
    output = capsys.readouterr().out
    assert "unknown.md" in output
    assert str(tmp_path) not in output


def test_cli_build_refuses_invalid_content_and_builds_valid_manifest(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    content = tmp_path / "content"
    content.mkdir()
    source = content / "00_整卷总览与讲评建议.md"
    source.write_text("TODO\n", encoding="utf-8")

    assert main(["build", "--content", "content", "--output", "output"]) == 2
    assert not (tmp_path / "output").exists()
    capsys.readouterr()

    source.write_text("# 原创讲评总览\n\n这是通过校验的原创教师讲评说明。\n", encoding="utf-8")
    assert main(["build", "--content", "content", "--output", "output"]) == 0
    output = capsys.readouterr().out

    assert "output/delivery.json" in output
    assert (tmp_path / "output" / "00_整卷总览与讲评建议.docx").is_file()
    assert json.loads((tmp_path / "output" / "delivery.json").read_text(encoding="utf-8"))["visual_status"] == "evidence_ready"


def test_cli_user_errors_are_stable_and_do_not_leak_absolute_paths(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)

    code = main(["run", "--task", str(tmp_path / "missing" / "task.json")])

    captured = capsys.readouterr()
    assert code == 2
    assert str(tmp_path) not in captured.err + captured.out


def test_cli_build_rejects_parent_traversal_before_creating_output(tmp_path, capsys, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    content = project / "content"
    content.mkdir()
    (content / "00_整卷总览与讲评建议.md").write_text(
        "# 原创讲评总览\n\n这是通过校验的原创教师讲评说明。\n", encoding="utf-8"
    )

    code = main(["build", "--content", "content", "--output", "../escaped-output"])

    assert code == 2
    assert not (tmp_path / "escaped-output").exists()
    assert str(tmp_path) not in capsys.readouterr().err


def test_cli_does_not_read_stdin(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def fail_input(*args, **kwargs):
        raise AssertionError("CLI must not read stdin")

    monkeypatch.setattr("builtins.input", fail_input)
    assert main(["status", "--task", ".local/tasks/missing/task.json", "--json"]) == 2
    capsys.readouterr()
