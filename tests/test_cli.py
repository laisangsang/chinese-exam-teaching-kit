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


def test_cli_never_echoes_posix_windows_or_unc_paths_from_exceptions(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    task = tmp_path / ".local" / "tasks" / "demo" / "task.json"
    task.parent.mkdir(parents=True)
    task.write_text("{}", encoding="utf-8")
    messages = (
        "failed at '/Users/Alice Smith/exam.pdf'",
        r"failed at C:\Users\Alice Smith\exam.pdf",
        r"failed at \\server\share\Alice Smith\exam.pdf",
        "failed at file:///Users/Alice%20Smith/exam.pdf",
    )
    for message in messages:
        monkeypatch.setattr(
            "chinese_exam_kit.cli.PipelineRunner.resume",
            lambda *args, _message=message, **kwargs: (_ for _ in ()).throw(OSError(_message)),
        )
        assert main(["run", "--task", ".local/tasks/demo/task.json"]) == 1
        rendered = capsys.readouterr().err
        assert "Alice" not in rendered
        assert "exam.pdf" not in rendered


def test_cli_argument_errors_do_not_echo_an_unknown_path(capsys):
    code = main(["--unknown", r"C:\Users\Alice Smith\exam.pdf"])

    assert code == 2
    rendered = capsys.readouterr().err
    assert "Alice" not in rendered
    assert "exam.pdf" not in rendered


def test_cli_accepts_equivalent_macos_var_alias_for_project_content(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    content = project / "content"
    content.mkdir()
    (content / "00_整卷总览与讲评建议.md").write_text(
        "# 原创讲评总览\n\n这是通过校验的原创教师讲评说明。\n", encoding="utf-8"
    )
    canonical = str(content)
    if not canonical.startswith("/private/var/") or not Path("/var").is_symlink():
        return
    alias = canonical.replace("/private/var/", "/var/", 1)

    code = main(["validate", "--content", alias])

    assert code == 0
    assert "通过" in capsys.readouterr().out
