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


def _generic_posix(*parts: str) -> str:
    return "/" + "/".join(parts)


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
        ("docs/key.md", "api_" + "key = '" + _secret() + "'", "secret_pattern"),
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
        lambda root: (
            release_guard._GitEntry("160000", "a" * 40, "docs/vendor", True),
        ),
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
        "aws_secret_" + "access_key = '" + "a" * 40 + "'",
        "connection_" + "string = 'Server=db;Pass" + "word=" + "a" * 20 + "'",
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

    assert release_guard._tracked_entries(Path(".")) == (
        release_guard._GitEntry("100644", "a" * 40, "docs/原创.md", True),
    )
    assert calls == [("ls-files", "-z"), ("ls-files", "-s", "-z")]


def test_unmerged_non_stage_zero_index_is_rejected(tmp_path, monkeypatch):
    relative = "docs/conflict.md"
    oid_a = b"a" * 40
    oid_b = b"b" * 40

    def fake_git(root, arguments):
        if arguments == ("ls-files", "-z"):
            return relative.encode() + b"\0"
        if arguments == ("ls-files", "-s", "-z"):
            return (
                b"100644 " + oid_a + b" 1\t" + relative.encode() + b"\0"
                b"100644 " + oid_b + b" 2\t" + relative.encode() + b"\0"
            )
        raise AssertionError(arguments)

    monkeypatch.setattr(release_guard, "_git", fake_git)
    entry = release_guard._tracked_entries(tmp_path)[0]
    assert entry.mode == "conflict"
    monkeypatch.setattr(release_guard, "_tracked_entries", lambda root: (entry,))
    monkeypatch.setattr(release_guard, "_remote_urls", lambda root: ())

    assert "invalid_git_mode" in _codes(audit_repository(tmp_path))


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
        "git" + "@" + "github.com:laisangsang/chinese-exam-teaching-kit.git",
        "ssh://" + "git" + "@" + "github.com/laisangsang/chinese-exam-teaching-kit.git",
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
        lambda root: (
            release_guard._GitEntry("100644", None, "docs/leak.md", False),
        ),
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
        lambda root: (
            release_guard._GitEntry("100644", None, "README.md", False),
        ),
    )
    monkeypatch.setattr(
        "chinese_exam_kit.release_guard._remote_urls", lambda root: ()
    )

    assert main(["release-audit"]) == 0
    assert "0 errors" in capsys.readouterr().out


def test_index_blob_is_release_truth_even_when_worktree_was_made_safe(tmp_path):
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    relative = "docs/staged.md"
    _write(tmp_path, relative, "safe\n")
    subprocess.run(("git", "add", "--", relative), cwd=tmp_path, check=True)
    _write(tmp_path, relative, "api_" + "key = '" + _secret() + "'\n")
    subprocess.run(("git", "add", "--", relative), cwd=tmp_path, check=True)
    _write(tmp_path, relative, "safe worktree\n")

    issues = audit_repository(tmp_path)

    assert "secret_pattern" in _codes(issues)
    assert "worktree_mismatch" in _codes(issues)


@pytest.mark.parametrize(
    ("relative", "staged", "code"),
    (
        ("docs/staged.txt", b"safe\0binary", "binary_file"),
        (
            "docs/pointer.txt",
            (
                "version https://git-lfs.github.com/spec/v1\n"
                "oid sha256:" + "b" * 64 + "\nsize 99\n"
            ).encode(),
            "lfs_pointer",
        ),
        ("docs/staged.pdf", b"document", "forbidden_extension"),
    ),
)
def test_staged_binary_lfs_and_forbidden_types_are_checked_from_index(
    tmp_path, relative, staged, code
):
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(staged)
    subprocess.run(("git", "add", "--", relative), cwd=tmp_path, check=True)
    target.write_text("safe worktree\n", encoding="utf-8")

    assert code in _codes(audit_repository(tmp_path))


def test_push_remote_is_audited_even_when_fetch_remote_is_safe(tmp_path):
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    _write(tmp_path, "README.md", "safe")
    subprocess.run(("git", "add", "README.md"), cwd=tmp_path, check=True)
    safe = "https://github.com/laisangsang/chinese-exam-teaching-kit.git"
    subprocess.run(("git", "remote", "add", "origin", safe), cwd=tmp_path, check=True)
    subprocess.run(
        ("git", "remote", "set-url", "--add", "--push", "origin", _file_uri()),
        cwd=tmp_path,
        check=True,
    )

    issues = audit_repository(tmp_path)

    assert "unsafe_remote" in _codes(issues)


@pytest.mark.parametrize(
    "locator",
    (
        _generic_posix("mnt", "share", "exam.md"),
        _generic_posix("srv", "app", "config.toml"),
        _generic_posix("workspace", "project", "note.md"),
        _generic_posix("data", "exam.pdf"),
        _generic_posix("custom-root"),
        "file:" + _generic_posix("srv", "data", "exam.pdf"),
        "/" * 2 + "/".join(("server", "share", "exam.pdf")),
    ),
)
def test_generic_absolute_filesystem_locators_are_blocked(tmp_path, locator):
    _write(tmp_path, "docs/locator.md", "source=" + locator)

    assert "absolute_path" in _codes(
        audit_repository(tmp_path, tracked=("docs/locator.md",))
    )


@pytest.mark.parametrize(
    "safe_text",
    (
        "See https://example.com/docs/guide and http://example.org/a/b.",
        "Markdown link: [guide](/docs/guide.md)",
        "Asset link: [logo](/assets/logo.svg)",
        "按 A/B/C 处理，普通中文/并列/表达。",
    ),
)
def test_url_markdown_root_links_and_slash_expressions_are_not_paths(
    tmp_path, safe_text
):
    _write(tmp_path, "docs/safe.md", safe_text)

    assert "absolute_path" not in _codes(
        audit_repository(tmp_path, tracked=("docs/safe.md",))
    )


@pytest.mark.parametrize(
    "value",
    (
        "prod-test-" + "a" * 24,
        "sample-live-" + "b" * 24,
        "example-real-" + "c" * 24,
        "example-token " + "d" * 24,
    ),
)
def test_secret_values_merely_containing_placeholder_words_are_blocked(
    tmp_path, value
):
    _write(tmp_path, "docs/key.txt", "api_" + "key = '" + value + "'")

    assert "secret_pattern" in _codes(
        audit_repository(tmp_path, tracked=("docs/key.txt",))
    )


def test_generic_quoted_token_and_bearer_credential_are_blocked(tmp_path):
    generic = "to" + "ken = '" + "a" * 28 + "'"
    bearer = "Authorization: " + "Bearer " + "b" * 28
    _write(tmp_path, "docs/tokens.txt", generic + "\n" + bearer)

    assert "secret_pattern" in _codes(
        audit_repository(tmp_path, tracked=("docs/tokens.txt",))
    )


@pytest.mark.parametrize(
    "value",
    ("<YOUR_API_KEY>", "example-token", "placeholder-token", "${API_KEY}"),
)
def test_only_strict_complete_placeholder_values_are_allowed(tmp_path, value):
    _write(tmp_path, "docs/key.txt", "api_" + "key = '" + value + "'")

    assert "secret_pattern" not in _codes(
        audit_repository(tmp_path, tracked=("docs/key.txt",))
    )


@pytest.mark.parametrize(
    "token",
    (
        "github" + "_pat_" + "a" * 30,
        "gh" + "p_" + "b" * 36,
        "gl" + "pat-" + "c" * 24,
    ),
)
def test_modern_repository_tokens_are_blocked(tmp_path, token):
    _write(tmp_path, "docs/token.txt", token)

    assert "secret_pattern" in _codes(
        audit_repository(tmp_path, tracked=("docs/token.txt",))
    )


@pytest.mark.parametrize(
    "email",
    (
        "teacher" + "@" + "school.example",
        "Person.Name" + "@" + "Company.Example",
    ),
)
def test_ordinary_personal_or_organization_emails_are_blocked(tmp_path, email):
    _write(tmp_path, "docs/contact.md", "contact: " + email)

    assert "identity_pattern" in _codes(
        audit_repository(tmp_path, tracked=("docs/contact.md",))
    )


def test_github_noreply_and_exact_configured_public_contacts_are_allowed(tmp_path):
    noreply = "210877483" + "+laisangsang@" + "users.noreply.github.com"
    public_contact = "project-contact" + "@" + "example.org"
    _write(
        tmp_path,
        "docs/contact.md",
        noreply + "\n" + noreply.upper() + "\n" + public_contact,
    )

    issues = audit_repository(
        tmp_path,
        tracked=("docs/contact.md",),
        allowlist={"public_contacts": [public_contact]},
    )

    assert "identity_pattern" not in _codes(issues)


def test_public_contact_allowlist_is_exact_not_domain_wide(tmp_path):
    public_contact = "project-contact" + "@" + "example.org"
    other_contact = "other-contact" + "@" + "example.org"
    _write(tmp_path, "docs/contact.md", other_contact)

    issues = audit_repository(
        tmp_path,
        tracked=("docs/contact.md",),
        allowlist={"public_contacts": [public_contact]},
    )

    assert "identity_pattern" in _codes(issues)


@pytest.mark.parametrize(
    "relative",
    (
        "docs/" + "safe" + chr(0x202E) + "txt.md",
        "docs/" + "zero" + chr(0x200B) + "width.md",
        "docs/" + "e" + chr(0x0301) + ".md",
        "docs/ leading.md",
        "docs/.hidden.md",
        "docs/COM" + chr(0x00B9) + ".md",
    ),
)
def test_unicode_format_non_nfc_and_edge_punctuation_paths_are_redacted(
    tmp_path, relative
):
    issues = audit_repository(tmp_path, tracked=(relative,))

    invalid = [issue for issue in issues if issue.code == "invalid_path"]
    assert len(invalid) == 1
    assert invalid[0].path == "<unsafe-path>"
    assert relative not in json.dumps([item.to_dict() for item in issues])


def test_casefold_path_collisions_are_redacted(tmp_path):
    _write(tmp_path, "docs/Case.md", "safe")

    issues = audit_repository(
        tmp_path, tracked=("docs/Case.md", "docs/case.md")
    )

    invalid = [issue for issue in issues if issue.code == "invalid_path"]
    assert invalid and all(issue.path == "<unsafe-path>" for issue in invalid)


def test_staged_policy_cannot_be_hidden_by_a_safe_worktree_policy(tmp_path):
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    unsafe_policy = {
        "schema_version": 1,
        "allowed_top_level_directories": ["materials"],
        "allowed_example_directory": "examples/original-mini-exam",
        "allowed_root_files": [],
        "forbidden_directories": [],
        "forbidden_extensions": [],
        "max_file_bytes": 2097152,
        "private_terms": [],
        "public_contacts": [],
    }
    policy_path = tmp_path / "config" / "public_release_allowlist.json"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text(json.dumps(unsafe_policy), encoding="utf-8")
    _write(tmp_path, "materials/private.md", "safe")
    subprocess.run(("git", "add", "--", "config", "materials"), cwd=tmp_path, check=True)
    policy_path.write_text("{}", encoding="utf-8")

    issues = audit_repository(tmp_path)

    assert {"config_invalid", "forbidden_path"} <= _codes(issues)


@pytest.mark.parametrize(
    "markdown",
    (
        "[示例](/examples/original-mini-exam/README.md)",
        "[源码](/src/chinese_exam_kit/cli.py)",
        "![图示](/docs/assets/example.svg)",
    ),
)
def test_valid_markdown_public_root_destinations_are_not_host_paths(
    tmp_path, markdown
):
    _write(tmp_path, "docs/links.md", markdown)

    assert "absolute_path" not in _codes(
        audit_repository(tmp_path, tracked=("docs/links.md",))
    )


@pytest.mark.parametrize(
    "destination",
    (
        _generic_posix("docs", "..", "materials", "private.md"),
        _generic_posix("examples", "borrowed", "exam.md"),
        _generic_posix("Users", "person-a", "private.md"),
        _generic_posix("src") + "\\" + "outside.py",
    ),
)
def test_unsafe_markdown_root_destinations_remain_blocked(tmp_path, destination):
    _write(tmp_path, "docs/links.md", "[不安全](" + destination + ")")

    assert "absolute_path" in _codes(
        audit_repository(tmp_path, tracked=("docs/links.md",))
    )


def test_http_urls_are_masked_before_drive_and_path_detection(tmp_path):
    url = "https://example.com/" + "C:" + "/guide"
    _write(tmp_path, "docs/url.md", "See " + url + " and https://example.org/a/b.")

    assert "absolute_path" not in _codes(
        audit_repository(tmp_path, tracked=("docs/url.md",))
    )


def test_http_url_userinfo_credential_is_still_blocked_as_a_secret(tmp_path):
    url = "https://" + "person:" + "a" * 24 + "@" + "example.com/docs"
    _write(tmp_path, "docs/url.md", url)

    issues = audit_repository(tmp_path, tracked=("docs/url.md",))

    assert "absolute_path" not in _codes(issues)
    assert "secret_pattern" in _codes(issues)


@pytest.mark.parametrize(
    "command",
    (
        "command >" + _generic_posix("Users", "person-a", "private.txt"),
        "command 2>" + _generic_posix("tmp", "error.log"),
        "command < " + _generic_posix("data", "input"),
    ),
)
def test_shell_redirection_absolute_paths_are_blocked(tmp_path, command):
    _write(tmp_path, "docs/shell.md", command)

    assert "absolute_path" in _codes(
        audit_repository(tmp_path, tracked=("docs/shell.md",))
    )


def test_blockquote_text_and_relative_links_are_safe_but_quoted_paths_are_not(
    tmp_path,
):
    safe = "> 普通文字\n\n[相对链接](../guide.md)\n"
    _write(tmp_path, "docs/quote.md", safe)
    assert "absolute_path" not in _codes(
        audit_repository(tmp_path, tracked=("docs/quote.md",))
    )

    quoted_path = "> " + _generic_posix("Users", "person-a", "private.md")
    _write(tmp_path, "docs/quote.md", quoted_path)
    assert "absolute_path" in _codes(
        audit_repository(tmp_path, tracked=("docs/quote.md",))
    )
