import json
from pathlib import Path

import pytest

from chinese_exam_kit.cli import main
from chinese_exam_kit.pipeline.state import load_task
from tests._host_samples import file_uri, posix_path, unc_path, windows_path


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
    assert payload["stages"]["knowledge_pre"]["status"] == "waiting"
    assert payload["stages"]["analysis"]["status"] == "pending"
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
        f"failed at '{posix_path('Users', 'person-a', 'exam.pdf')}'",
        f"failed at {windows_path('Users', 'person-a', 'exam.pdf')}",
        f"failed at {unc_path('server', 'share', 'person-a', 'exam.pdf')}",
        f"failed at {file_uri('Users', 'person-a', 'exam.pdf')}",
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
    code = main(["--unknown", windows_path("Users", "person-a", "exam.pdf")])

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
    private_var = posix_path("private", "var") + "/"
    var = posix_path("var") + "/"
    if not canonical.startswith(private_var) or not Path(posix_path("var")).is_symlink():
        return
    alias = canonical.replace(private_var, var, 1)

    code = main(["validate", "--content", alias])

    assert code == 0
    assert "通过" in capsys.readouterr().out


@pytest.mark.parametrize("suffix", (".md", ".txt", ".docx", ".pdf"))
def test_cli_explicit_exam_and_answer_roles_bind_every_document_format(
    tmp_path, monkeypatch, capsys, suffix
):
    monkeypatch.chdir(tmp_path)
    exam = tmp_path / f"exam{suffix}"
    answer = tmp_path / f"answer{suffix}"
    exam.write_bytes(b"explicit exam bytes")
    answer.write_bytes(b"explicit answer bytes")

    code = main(
        [
            "init",
            "--name",
            f"roles-{suffix[1:]}",
            "--exam",
            str(exam),
            "--answer",
            str(answer),
        ]
    )

    assert code == 0
    task_path = tmp_path / capsys.readouterr().out.strip()
    task = load_task(task_path)
    assert [record.material_type for record in task.materials] == [
        "exam_candidate",
        "answer_candidate",
    ]


def test_cli_explicit_pdf_role_does_not_parse_invalid_pdf(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a PDF")

    assert main(["init", "--name", "broken", "--exam", str(broken)]) == 0
    task = load_task(tmp_path / capsys.readouterr().out.strip())

    assert task.materials[0].material_type == "exam_candidate"


def test_cli_init_requires_at_least_one_input_role(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["init", "--name", "empty"]) == 2
    assert not (tmp_path / ".local").exists()
    assert str(tmp_path) not in capsys.readouterr().err


def test_initial_explicit_answer_is_bound_before_knowledge_pause(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    exam = tmp_path / "exam.md"
    answer = tmp_path / "answer.md"
    exam.write_text("# 试卷\n\n1. 原创题。\n", encoding="utf-8")
    answer.write_text("# 参考答案\n\n1. 原创答案。\n", encoding="utf-8")
    assert main(
        ["init", "--name", "bound", "--exam", str(exam), "--answer", str(answer)]
    ) == 0
    task_value = capsys.readouterr().out.strip()

    assert main(["run", "--task", task_value]) == 0

    marker = tmp_path / task_value
    payload = json.loads(
        (marker.parent / "answers" / "current_revision.json").read_text(
            encoding="utf-8"
        )
    )
    answer_record = next(
        record
        for record in load_task(marker).materials
        if record.material_type == "answer_candidate"
    )
    assert payload["answer_sha256"] == [answer_record.sha256]
