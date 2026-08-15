from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import chinese_exam_kit.release_guard as release_guard
from chinese_exam_kit.cli import main
from chinese_exam_kit.release_guard import ReleaseIssue, audit_repository


def _write(root: Path, relative: str, contents: str = "safe\n") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents, encoding="utf-8")
    return target


def _posix_path() -> str:
    return "/" + "/".join(("Users", "person-a", "Desktop", "draft.pdf"))


def _windows_path() -> str:
    return "C:" + "\\" + "\\".join(("Users", "person-a", "draft.pdf"))


def _unc_path() -> str:
    return "\\" * 2 + "server" + "\\" + "share" + "\\" + "draft.pdf"


def _file_uri() -> str:
    return "file:" + "//" + _posix_path()


def _secret() -> str:
    return "sk" + "-" + "livecredentialvalue123456"


def _codes(issues: tuple[ReleaseIssue, ...]) -> set[str]:
    return {issue.code for issue in issues}


@pytest.mark.parametrize(
    ("relative", "contents", "code"),
    [
        ("docs/leak.md", _posix_path(), "absolute_path"),
        ("materials/exam.md", "原创", "forbidden_path"),
        ("docs/key.md", "api_key = '" + _secret() + "'", "secret_pattern"),
        ("docs/drive.md", _windows_path(), "absolute_path"),
        ("docs/share.md", _unc_path(), "absolute_path"),
        ("docs/uri.md", _file_uri(), "absolute_path"),
        ("docs/private.pem", "safe", "forbidden_extension"),
        ("docs/worksheet.pages", "safe", "forbidden_extension"),
    ],
)
def test_release_guard_blocks_unsafe_files(tmp_path, relative, contents, code):
    _write(tmp_path, relative, contents)

    issues = audit_repository(tmp_path, tracked=(relative,))

    assert code in _codes(issues)
    assert all(str(tmp_path) not in issue.message for issue in issues)


def test_release_guard_blocks_large_binary_media_and_lfs_pointers(tmp_path):
    _write(tmp_path, "docs/large.md", "x" * (2 * 1024 * 1024 + 1))
    media = tmp_path / "docs" / "recording.mp4"
    media.write_bytes(b"video")
    lfs = _write(
        tmp_path,
        "docs/asset.txt",
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:" + "a" * 64 + "\nsize 12\n",
    )

    assert "file_too_large" in _codes(
        audit_repository(tmp_path, tracked=("docs/large.md",))
    )
    assert "forbidden_extension" in _codes(
        audit_repository(tmp_path, tracked=("docs/recording.mp4",))
    )
    assert lfs.is_file()
    assert "lfs_pointer" in _codes(
        audit_repository(tmp_path, tracked=("docs/asset.txt",))
    )


@pytest.mark.parametrize(
    "relative",
    (
        "../escape.md",
        "/absolute.md",
        "C:" + "\\" + "outside.md",
        "\\" * 2 + "server" + "share" + "file.md",
        "docs\\outside.md",
        "docs/control\nname.md",
        "docs/AUX.md",
        "docs/bad:name.md",
        "docs/trailing. ",
    ),
)
def test_release_guard_rejects_non_portable_or_escaping_tracked_paths(
    tmp_path, relative
):
    issues = audit_repository(tmp_path, tracked=(relative,))

    assert "invalid_path" in _codes(issues)
    assert all(issue.path == "<unsafe-path>" for issue in issues if issue.code == "invalid_path")


def test_release_guard_allows_only_explicit_root_and_directory_names(tmp_path):
    allowed = {
        "README.md": "# Project\n",
        "README.zh-CN.md": "# 项目\n",
        "AGENTS.md": "# Agent guide\n",
        "docs/guide.md": "See https://example.com/a/b and docs/guide.md.\n",
        "examples/original-mini-exam/question.md": "普通中文/并列/表达，按 A/B 处理。\n",
        "src/chinese_exam_kit/example.py": "VALUE = 'placeholder-token'\n",
    }
    for relative, contents in allowed.items():
        _write(tmp_path, relative, contents)

    issues = audit_repository(tmp_path, tracked=tuple(allowed))

    assert not [issue for issue in issues if issue.level == "error"]

    _write(tmp_path, "examples/borrowed/exam.md", "unsafe")
    _write(tmp_path, "random.md", "unsafe")
    codes = _codes(
        audit_repository(
            tmp_path,
            tracked=("examples/borrowed/exam.md", "random.md"),
        )
    )
    assert "forbidden_path" in codes


def test_release_guard_blocks_symlink_and_missing_tracked_file(tmp_path):
    _write(tmp_path, "docs/real.md")
    link = tmp_path / "docs" / "link.md"
    try:
        link.symlink_to("real.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlink unavailable")

    issues = audit_repository(
        tmp_path,
        tracked=("docs/link.md", "docs/missing.md"),
    )

    assert {"tracked_symlink", "missing_tracked_file"} <= _codes(issues)


def test_release_guard_blocks_a_git_submodule_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(
        release_guard,
        "_tracked_entries",
        lambda root: (("160000", "docs/vendor"),),
    )
    monkeypatch.setattr(release_guard, "_remote_urls", lambda root: ())

    issues = audit_repository(tmp_path)

    assert "tracked_submodule" in _codes(issues)


def test_release_guard_blocks_a_symlinked_parent_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.md").write_text("safe", encoding="utf-8")
    try:
        (tmp_path / "docs").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unavailable")

    issues = audit_repository(tmp_path, tracked=("docs/note.md",))

    assert "tracked_symlink" in _codes(issues)


def test_release_guard_policy_cannot_expand_the_fixed_public_surface(tmp_path):
    _write(tmp_path, "materials/private.md", "safe")
    weakened = {
        "allowed_top_level_directories": ["materials"],
        "forbidden_directories": [],
    }

    issues = audit_repository(
        tmp_path,
        tracked=("materials/private.md",),
        allowlist=weakened,
    )

    assert {"config_invalid", "forbidden_path"} <= _codes(issues)


@pytest.mark.parametrize(
    "contents",
    (
        "aws_secret_access_key = '" + "a" * 40 + "'",
        "connection_" + "string = 'Server=db;Password=" + "a" * 20 + "'",
        "-----BEGIN " + "PRIVATE KEY-----\n" + "a" * 32,
    ),
)
def test_release_guard_blocks_cloud_and_connection_credentials(tmp_path, contents):
    _write(tmp_path, "docs/config.txt", contents)

    issues = audit_repository(tmp_path, tracked=("docs/config.txt",))

    assert "secret_pattern" in _codes(issues)


def test_release_guard_blocks_packaging_metadata_inside_an_allowed_tree(tmp_path):
    _write(tmp_path, "src/package.egg-info/PKG-INFO", "safe")

    issues = audit_repository(
        tmp_path, tracked=("src/package.egg-info/PKG-INFO",)
    )

    assert "forbidden_path" in _codes(issues)


def test_release_guard_scans_configured_private_terms_without_reflecting_them(tmp_path):
    private_name = "private" + "-project-codename"
    _write(tmp_path, "docs/note.md", f"migrated from {private_name}\n")

    issues = audit_repository(
        tmp_path,
        tracked=("docs/note.md",),
        private_terms=(private_name,),
    )

    matches = [issue for issue in issues if issue.code == "private_term"]
    assert len(matches) == 1
    assert private_name not in matches[0].message


def test_release_guard_redacts_private_terms_or_credentials_in_filenames(tmp_path):
    private_name = "private" + "-material-codename"
    credential_name = _secret() + ".md"
    _write(tmp_path, f"docs/{private_name}.md", "safe")
    _write(tmp_path, f"docs/{credential_name}", "safe")

    issues = audit_repository(
        tmp_path,
        tracked=(f"docs/{private_name}.md", f"docs/{credential_name}"),
        private_terms=(private_name,),
    )

    assert {"private_term", "secret_pattern"} <= _codes(issues)
    rendered = json.dumps([issue.to_dict() for issue in issues], ensure_ascii=False)
    assert private_name not in rendered
    assert _secret() not in rendered
    assert all(issue.path == "<redacted-path>" for issue in issues)


def test_issue_order_and_messages_are_deterministic_and_redacted(tmp_path):
    _write(tmp_path, "docs/z.md", _posix_path())
    _write(tmp_path, "docs/a.md", _secret())

    first = audit_repository(tmp_path, tracked=("docs/z.md", "docs/a.md"))
    second = audit_repository(tmp_path, tracked=("docs/a.md", "docs/z.md"))

    assert first == second
    assert [item.to_dict() for item in first] == sorted(
        [item.to_dict() for item in first],
        key=lambda item: (item["level"], item["path"], item["code"], item["message"]),
    )
    rendered = json.dumps([item.to_dict() for item in first], ensure_ascii=False)
    assert _posix_path() not in rendered
    assert _secret() not in rendered


def test_default_git_inventory_handles_unicode_and_reports_control_names(tmp_path):
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    _write(tmp_path, "docs/原创.md", "safe")
    _write(tmp_path, "docs/line\nbreak.md", "safe")
    subprocess.run(("git", "add", "--", "docs"), cwd=tmp_path, check=True)

    issues = audit_repository(tmp_path)

    assert "invalid_path" in _codes(issues)
    assert not any(issue.path == "docs/原创.md" for issue in issues)


def test_git_inventory_uses_nul_delimited_primary_paths_and_index_modes(monkeypatch):
    calls = []

    def fake_git(root, arguments):
        calls.append(arguments)
        if arguments == ("ls-files", "-z"):
            return "docs/原创.md".encode() + b"\0"
        if arguments == ("ls-files", "-s", "-z"):
            return b"100644 " + b"a" * 40 + b" 0\t" + "docs/原创.md".encode() + b"\0"
        raise AssertionError(arguments)

    monkeypatch.setattr(release_guard, "_git", fake_git)

    assert release_guard._tracked_entries(Path(".")) == (("100644", "docs/原创.md"),)
    assert calls == [("ls-files", "-z"), ("ls-files", "-s", "-z")]


def test_default_git_inventory_blocks_local_or_unrelated_remote(tmp_path):
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    _write(tmp_path, "README.md", "safe")
    subprocess.run(("git", "add", "README.md"), cwd=tmp_path, check=True)
    subprocess.run(
        ("git", "remote", "add", "origin", _file_uri()), cwd=tmp_path, check=True
    )

    issues = audit_repository(tmp_path)

    assert "unsafe_remote" in _codes(issues)
    rendered = json.dumps([issue.to_dict() for issue in issues])
    assert _file_uri() not in rendered


@pytest.mark.parametrize(
    "remote",
    (
        "https://github.com/laisangsang/chinese-exam-teaching-kit.git",
        "git@github.com:laisangsang/chinese-exam-teaching-kit.git",
        "ssh://git@github.com/laisangsang/chinese-exam-teaching-kit.git",
    ),
)
def test_only_the_authorized_public_repository_remote_is_accepted(remote):
    assert release_guard._is_allowed_remote(remote)


def test_release_audit_cli_json_is_machine_readable_and_exit_code_tracks_errors(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "docs/leak.md", _posix_path())
    monkeypatch.setattr(
        "chinese_exam_kit.release_guard._tracked_entries",
        lambda root: (("100644", "docs/leak.md"),),
    )
    monkeypatch.setattr(
        "chinese_exam_kit.release_guard._remote_urls", lambda root: ()
    )

    assert main(["release-audit", "--json"]) == 1
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    assert payload[0]["code"] == "absolute_path"
    assert str(tmp_path) not in raw
    assert _posix_path() not in raw


def test_release_audit_cli_human_clean_output(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "README.md", "safe")
    monkeypatch.setattr(
        "chinese_exam_kit.release_guard._tracked_entries",
        lambda root: (("100644", "README.md"),),
    )
    monkeypatch.setattr(
        "chinese_exam_kit.release_guard._remote_urls", lambda root: ()
    )

    assert main(["release-audit"]) == 0
    assert "0 errors" in capsys.readouterr().out
