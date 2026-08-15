"""Fail-closed checks for files intended for a public source release."""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
import stat
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence


_MAX_TEXT_SCAN_BYTES = 2 * 1024 * 1024
_SAFE_REMOTE_REPOSITORY = "laisangsang/chinese-exam-teaching-kit"
_LFS_HEADER = "version " + "https://git-lfs.github.com/spec/v1"

_DEFAULT_POLICY: dict[str, object] = {
    "schema_version": 1,
    "allowed_top_level_directories": [
        ".github",
        ".codebuddy",
        "agent-guides",
        "config",
        "docs",
        "knowledge",
        "scripts",
        "src",
        "templates",
        "tests",
    ],
    "allowed_example_directory": "examples/original-mini-exam",
    "allowed_root_files": [
        ".gitignore",
        "AGENTS.md",
        "CHANGELOG.md",
        "CLAUDE.md",
        "CODEBUDDY.md",
        "CODE_OF_CONDUCT.md",
        "CONTENT_POLICY.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "LICENSE.md",
        "MANIFEST.in",
        "NOTICE",
        "NOTICE.md",
        "PRIVACY.md",
        "SECURITY.md",
        "SUPPORT.md",
        "THIRD_PARTY_NOTICES.md",
        "pyproject.toml",
    ],
    "forbidden_directories": [
        ".git",
        ".local",
        ".pytest_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "materials",
        "output",
        "tmp",
    ],
    "forbidden_extensions": [
        ".7z",
        ".aac",
        ".accdb",
        ".avi",
        ".bin",
        ".bmp",
        ".bz2",
        ".class",
        ".db",
        ".dll",
        ".dmg",
        ".doc",
        ".docm",
        ".docx",
        ".dylib",
        ".eot",
        ".exe",
        ".flac",
        ".gif",
        ".gz",
        ".heic",
        ".ico",
        ".iso",
        ".jpeg",
        ".jpg",
        ".key",
        ".keynote",
        ".m4a",
        ".mdb",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".npy",
        ".npz",
        ".numbers",
        ".odg",
        ".odp",
        ".ods",
        ".odt",
        ".ogg",
        ".otf",
        ".pages",
        ".pdf",
        ".pem",
        ".png",
        ".ppt",
        ".pptm",
        ".pptx",
        ".pkl",
        ".pyc",
        ".pyo",
        ".rar",
        ".rtf",
        ".so",
        ".sqlite",
        ".sqlite3",
        ".svg",
        ".tar",
        ".tgz",
        ".tif",
        ".tiff",
        ".ttf",
        ".ttc",
        ".wav",
        ".wasm",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
        ".xls",
        ".xlsb",
        ".xlsm",
        ".xlsx",
        ".xz",
        ".zst",
        ".zip",
    ],
    "max_file_bytes": _MAX_TEXT_SCAN_BYTES,
    "private_terms": [],
    "public_contacts": [],
}

_FIXED_MESSAGES = {
    "absolute_path": "文本包含本机绝对路径定位信息。",
    "binary_file": "跟踪文件不是受支持的 UTF-8 文本。",
    "config_invalid": "公开发行白名单配置无效。",
    "file_too_large": "跟踪文件超过公开发行大小上限。",
    "forbidden_extension": "该文件类型不得进入公开发行。",
    "forbidden_path": "该路径不在公开发行白名单内。",
    "git_error": "无法安全读取 Git 跟踪状态。",
    "identity_pattern": "文本包含未获准公开的邮箱身份信息。",
    "invalid_git_mode": "Git 索引包含不受支持的文件类型。",
    "invalid_path": "Git 跟踪路径不安全或不可移植。",
    "lfs_pointer": "公开发行不得跟踪 Git LFS 指针。",
    "local_identity": "文本包含本机身份信息。",
    "missing_tracked_file": "Git 跟踪文件在工作区中缺失。",
    "private_term": "文本包含配置为私有的项目或材料名称。",
    "secret_pattern": "文本疑似包含密钥、令牌或连接凭据。",
    "tracked_submodule": "公开发行不得包含 Git 子模块。",
    "tracked_symlink": "公开发行不得包含符号链接。",
    "unsafe_remote": "Git remote 未指向获准的公开仓库。",
    "worktree_mismatch": "工作区内容与待发布的 Git 暂存区不同。",
}


@dataclass(frozen=True)
class ReleaseIssue:
    """A deterministic, redacted release audit finding."""

    code: str
    path: str
    message: str
    level: str = "error"

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "level": self.level,
            "message": self.message,
            "path": self.path,
        }


@dataclass(frozen=True)
class _GitEntry:
    mode: str
    oid: str | None
    path: str
    indexed: bool


class _GitAuditFailure(RuntimeError):
    pass


def audit_repository(
    root: Path | str = Path.cwd(),
    *,
    tracked: Iterable[str] | None = None,
    private_terms: Iterable[str] = (),
    allowlist: Mapping[str, object] | None = None,
) -> tuple[ReleaseIssue, ...]:
    """Audit the tracked public surface without returning host-specific values."""
    repository = Path(root).resolve(strict=False)
    issues: list[ReleaseIssue] = []
    if tracked is None:
        try:
            entries = _tracked_entries(repository)
            remotes = _remote_urls(repository)
        except _GitAuditFailure:
            return (_issue("git_error", "."),)
        if not remotes:
            issues.append(
                _issue(
                    "unsafe_remote",
                    ".git/config",
                    level="warning",
                    message="尚未配置公开仓库 remote。",
                )
            )
        elif any(not _is_allowed_remote(url) for url in remotes):
            issues.append(_issue("unsafe_remote", ".git/config"))
    else:
        entries = tuple(
            _GitEntry("100644", None, str(relative), False) for relative in tracked
        )

    policy = dict(_DEFAULT_POLICY)
    if allowlist is not None:
        policy.update(allowlist)
    elif tracked is None:
        config_entry = next(
            (
                entry
                for entry in entries
                if entry.path == "config/public_release_allowlist.json"
            ),
            None,
        )
        if config_entry is not None and config_entry.mode in {"100644", "100755"}:
            try:
                loaded = json.loads(
                    _read_index_blob(
                        repository, config_entry, _MAX_TEXT_SCAN_BYTES
                    ).decode("utf-8", errors="strict")
                )
                if not isinstance(loaded, dict):
                    raise ValueError("policy must be an object")
                policy.update(loaded)
            except _GitAuditFailure:
                return _sorted((*issues, _issue("git_error", ".")))
            except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
                issues.append(_issue("config_invalid", "config/public_release_allowlist.json"))
    if not _policy_is_safe(policy):
        issues.append(_issue("config_invalid", "config/public_release_allowlist.json"))
        policy = dict(_DEFAULT_POLICY)

    configured_terms = policy.get("private_terms", ())
    terms: list[str] = []
    if isinstance(configured_terms, list):
        terms.extend(str(item) for item in configured_terms)
    terms.extend(str(item) for item in private_terms)
    terms = [item for item in terms if item.strip()]
    contacts = tuple(
        str(item) for item in policy.get("public_contacts", ()) if isinstance(item, str)
    )

    seen: set[str] = set()
    portable_seen: dict[str, str] = {}
    for entry in entries:
        raw_relative = entry.path
        safe_relative = _safe_relative(raw_relative)
        if safe_relative is None:
            issues.append(_issue("invalid_path", "<unsafe-path>"))
            continue
        if safe_relative in seen:
            issues.append(_issue("invalid_git_mode", safe_relative))
            continue
        seen.add(safe_relative)
        portable_key = unicodedata.normalize("NFC", safe_relative).casefold()
        if portable_key in portable_seen:
            issues.append(_issue("invalid_path", "<unsafe-path>"))
            continue
        portable_seen[portable_key] = safe_relative
        path_codes: list[str] = []
        if _contains_secret(safe_relative):
            path_codes.append("secret_pattern")
        if _contains_local_identity(safe_relative):
            path_codes.append("local_identity")
        if _contains_email(safe_relative, contacts):
            path_codes.append("identity_pattern")
        folded_path = safe_relative.casefold()
        if any(term.casefold() in folded_path for term in terms):
            path_codes.append("private_term")
        try:
            file_issues = _audit_tracked_file(
                repository, safe_relative, entry, policy, terms, contacts
            )
        except _GitAuditFailure:
            return _sorted((*issues, _issue("git_error", ".")))
        if path_codes:
            redacted = "<redacted-path>"
            issues.extend(_issue(code, redacted) for code in path_codes)
            issues.extend(
                ReleaseIssue(item.code, redacted, item.message, item.level)
                for item in file_issues
            )
        else:
            issues.extend(file_issues)
    return _sorted(issues)


def _tracked_entries(root: Path) -> tuple[_GitEntry, ...]:
    inventory = _decode_nul_paths(_git(root, ("ls-files", "-z")))
    index = _git(root, ("ls-files", "-s", "-z"))
    indexed: dict[str, list[tuple[str, str, str]]] = {}
    for record in index.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            fields = metadata.split(b" ")
            if len(fields) != 3:
                raise ValueError("unexpected index record")
            mode = fields[0].decode("ascii")
            oid = fields[1].decode("ascii")
            stage = fields[2].decode("ascii")
            path = raw_path.decode("utf-8", errors="strict")
        except (ValueError, UnicodeError):
            raise _GitAuditFailure from None
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", oid):
            raise _GitAuditFailure
        indexed.setdefault(path, []).append((mode, oid, stage))
    if set(inventory) != set(indexed):
        raise _GitAuditFailure
    entries: list[_GitEntry] = []
    for path in inventory:
        records = indexed[path]
        if len(records) != 1 or records[0][2] != "0":
            entries.append(_GitEntry("conflict", None, path, True))
            continue
        mode, oid, _stage = records[0]
        entries.append(_GitEntry(mode, oid, path, True))
    return tuple(entries)


def _decode_nul_paths(output: bytes) -> tuple[str, ...]:
    try:
        return tuple(
            record.decode("utf-8", errors="strict")
            for record in output.split(b"\0")
            if record
        )
    except UnicodeError:
        raise _GitAuditFailure from None


def _remote_urls(root: Path) -> tuple[str, ...]:
    names_raw = _git(root, ("remote",))
    try:
        names = names_raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeError:
        raise _GitAuditFailure from None
    urls: set[str] = set()
    for name in names:
        if not name:
            continue
        for arguments in (
            ("remote", "get-url", "--all", name),
            ("remote", "get-url", "--push", "--all", name),
        ):
            raw = _git(root, arguments)
            try:
                urls.update(
                    line
                    for line in raw.decode("utf-8", errors="strict").splitlines()
                    if line
                )
            except UnicodeError:
                raise _GitAuditFailure from None
    return tuple(sorted(urls))


def _git(root: Path, arguments: Sequence[str]) -> bytes:
    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError:
        raise _GitAuditFailure from None
    if result.returncode != 0:
        raise _GitAuditFailure
    return result.stdout


def _audit_tracked_file(
    root: Path,
    relative: str,
    entry: _GitEntry,
    policy: Mapping[str, object],
    private_terms: Sequence[str],
    public_contacts: Sequence[str],
) -> list[ReleaseIssue]:
    issues: list[ReleaseIssue] = []
    if not _path_allowed(relative, policy):
        issues.append(_issue("forbidden_path", relative))

    if entry.mode == "120000":
        return [*issues, _issue("tracked_symlink", relative)]
    if entry.mode == "160000":
        return [*issues, _issue("tracked_submodule", relative)]
    if entry.mode not in {"100644", "100755"}:
        return [*issues, _issue("invalid_git_mode", relative)]

    extensions = _string_set(policy.get("forbidden_extensions"))
    if PurePosixPath(relative).suffix.casefold() in extensions:
        issues.append(_issue("forbidden_extension", relative))
        return issues
    maximum = policy.get("max_file_bytes", _MAX_TEXT_SCAN_BYTES)
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
        issues.append(_issue("config_invalid", "config/public_release_allowlist.json"))
        maximum = _MAX_TEXT_SCAN_BYTES
    if entry.indexed:
        if entry.oid is None:
            return [*issues, _issue("invalid_git_mode", relative)]
        size = _index_blob_size(root, entry.oid)
        if size > maximum:
            return [*issues, _issue("file_too_large", relative)]
        raw = _read_index_blob(root, entry, maximum, size=size)
        if not _worktree_matches_index(root, relative):
            issues.append(
                _issue("worktree_mismatch", relative, level="warning")
            )
    else:
        raw, failure = _read_injected_file(root, relative, maximum)
        if failure is not None:
            return [*issues, _issue(failure, relative)]
        assert raw is not None
    if b"\0" in raw:
        issues.append(_issue("binary_file", relative))
        return issues
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        issues.append(_issue("binary_file", relative))
        return issues
    if text.startswith(_LFS_HEADER + "\n"):
        issues.append(_issue("lfs_pointer", relative))
        return issues
    issues.extend(
        _scan_text(relative, text, private_terms, public_contacts)
    )
    return issues


def _index_blob_size(root: Path, oid: str) -> int:
    raw = _git(root, ("cat-file", "-s", oid))
    try:
        value = raw.decode("ascii", errors="strict").strip()
        if not re.fullmatch(r"[0-9]+", value):
            raise ValueError("invalid blob size")
        return int(value)
    except (UnicodeError, ValueError):
        raise _GitAuditFailure from None


def _read_index_blob(
    root: Path,
    entry: _GitEntry,
    maximum: int,
    *,
    size: int | None = None,
) -> bytes:
    if entry.oid is None:
        raise _GitAuditFailure
    if size is None:
        size = _index_blob_size(root, entry.oid)
    if size > maximum:
        raise ValueError("indexed blob exceeds read limit")
    raw = _git(root, ("cat-file", "blob", entry.oid))
    if len(raw) != size:
        raise _GitAuditFailure
    return raw


def _worktree_matches_index(root: Path, relative: str) -> bool:
    try:
        result = subprocess.run(
            ("git", "diff-files", "--quiet", "--", relative),
            cwd=root,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError:
        raise _GitAuditFailure from None
    if result.returncode not in {0, 1}:
        raise _GitAuditFailure
    return result.returncode == 0


def _read_injected_file(
    root: Path, relative: str, maximum: int
) -> tuple[bytes | None, str | None]:
    """Test-only injected inventory reader using no-follow file descriptors."""
    target = root.joinpath(*PurePosixPath(relative).parts)
    cursor = root
    for part in PurePosixPath(relative).parts[:-1]:
        cursor /= part
        try:
            parent_info = cursor.lstat()
        except OSError:
            return None, "missing_tracked_file"
        if stat.S_ISLNK(parent_info.st_mode):
            return None, "tracked_symlink"
        if not stat.S_ISDIR(parent_info.st_mode):
            return None, "invalid_git_mode"
    try:
        target_info = target.lstat()
    except OSError:
        return None, "missing_tracked_file"
    if stat.S_ISLNK(target_info.st_mode):
        return None, "tracked_symlink"
    if not stat.S_ISREG(target_info.st_mode):
        return None, "invalid_git_mode"
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags)
    except OSError:
        try:
            if target.is_symlink():
                return None, "tracked_symlink"
        except OSError:
            pass
        return None, "missing_tracked_file"
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None, "invalid_git_mode"
        if info.st_size > maximum:
            return None, "file_too_large"
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum:
            return None, "file_too_large"
        return raw, None
    finally:
        os.close(descriptor)


def _path_allowed(relative: str, policy: Mapping[str, object]) -> bool:
    parts = PurePosixPath(relative).parts
    if not parts:
        return False
    forbidden = _string_set(policy.get("forbidden_directories"))
    if any(
        part.casefold() in forbidden or part.casefold().endswith(".egg-info")
        for part in parts
    ):
        return False
    if len(parts) == 1:
        roots = set(_string_values(policy.get("allowed_root_files")))
        return parts[0] in roots or bool(
            re.fullmatch(r"README(?:\.[A-Za-z0-9_-]+)?\.md", parts[0])
        )
    if parts[0] == "examples":
        example = str(policy.get("allowed_example_directory", ""))
        prefix = tuple(PurePosixPath(example).parts)
        return len(parts) > len(prefix) and parts[: len(prefix)] == prefix
    allowed = set(_string_values(policy.get("allowed_top_level_directories")))
    return parts[0] in allowed


def _safe_relative(value: str) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    if any(
        ord(character) < 32
        or ord(character) == 127
        or unicodedata.category(character) == "Cf"
        for character in value
    ):
        return None
    if unicodedata.normalize("NFC", value) != value:
        return None
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return None
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    invalid_windows_characters = set('<>:"|?*')
    reserved_windows_names = {
        "CLOCK$",
        "CON",
        "CONIN$",
        "CONOUT$",
        "PRN",
        "AUX",
        "NUL",
    } | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in (*range(1, 10), "¹", "²", "³")
    }
    for part in candidate.parts:
        allowed_dot_components = {".codebuddy", ".github", ".gitignore"}
        if (
            any(character in invalid_windows_characters for character in part)
            or (part != part.strip(" .") and part not in allowed_dot_components)
            or (part.startswith(".") and part not in allowed_dot_components)
            or part.split(".", 1)[0].upper() in reserved_windows_names
        ):
            return None
    normalized = candidate.as_posix()
    if normalized != value:
        return None
    return normalized


def _scan_text(
    relative: str,
    text: str,
    private_terms: Sequence[str],
    public_contacts: Sequence[str],
) -> list[ReleaseIssue]:
    issues: list[ReleaseIssue] = []
    if _contains_absolute_path(text):
        issues.append(_issue("absolute_path", relative))
    if _contains_secret(text):
        issues.append(_issue("secret_pattern", relative))
    if _contains_local_identity(text):
        issues.append(_issue("local_identity", relative))
    if _contains_email(text, public_contacts):
        issues.append(_issue("identity_pattern", relative))
    folded = text.casefold()
    if any(term.casefold() in folded for term in private_terms):
        issues.append(_issue("private_term", relative))
    return issues


def _contains_absolute_path(text: str) -> bool:
    file_prefix = "file" + ":"
    if re.search(re.escape(file_prefix) + r"[\\/]{1,3}", text, flags=re.IGNORECASE):
        return True
    drive = r"(?<![A-Za-z0-9\\])[A-Za-z]" + re.escape(":") + r"[\\/]"
    if re.search(drive, text):
        return True
    two_slashes = re.escape("\\" * 2)
    unc = (
        r"(?<![\\])"
        + two_slashes
        + r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}[\\][^\\\s]+"
    )
    if re.search(unc, text):
        return True
    forward_unc = r"(?<![:/])" + re.escape("/" * 2) + r"[A-Za-z0-9._-]+/[A-Za-z0-9._~-]+"
    if re.search(forward_unc, text):
        return True
    candidate_pattern = re.compile(
        r"(?<![\w:/.-])/(?!/)([A-Za-z0-9_\u3400-\u9fff.~+-][^\s'\"<>()\[\]{}]*)"
    )
    markdown_roots = {"api", "assets", "docs", "help", "images", "static"}
    safe_system_prefixes = {
        ("bin", "bash"),
        ("bin", "sh"),
        ("usr", "bin"),
    }
    known_filesystem_roots = {
        "Applications",
        "Library",
        "Users",
        "Volumes",
        "data",
        "etc",
        "home",
        "mnt",
        "opt",
        "private",
        "root",
        "srv",
        "tmp",
        "usr",
        "var",
        "workspace",
    }
    for match in candidate_pattern.finditer(text):
        candidate = match.group(1).rstrip(".,;:!?")
        parts = [part for part in candidate.split("/") if part]
        if not parts:
            continue
        is_markdown_root = (
            parts[0] in markdown_roots
            and match.start() > 0
            and text[match.start() - 1] == "("
        )
        if is_markdown_root:
            continue
        if match.start() > 0 and text[match.start() - 1] == ">":
            continue
        if any(tuple(parts[: len(prefix)]) == prefix for prefix in safe_system_prefixes):
            continue
        context = text[max(0, match.start() - 32) : match.start()]
        field_context = bool(
            re.search(
                r"(?i)(?:source|path|file|locator|location|at|位于|路径|来源)\s*[:=]?\s*$",
                context,
            )
        )
        if parts[0] in known_filesystem_roots or len(parts) >= 2 or field_context:
            return True
    return False


def _contains_secret(text: str) -> bool:
    assignment = re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|"
        r"aws[_-]?secret[_-]?access[_-]?key|connection[_-]?string|private[_-]?key)\b"
        r"\s*[:=]\s*(?:'([^'\r\n]*)'|\"([^\"\r\n]*)\"|([^\s,;}{]+))"
    )
    for match in assignment.finditer(text):
        value = next(group for group in match.groups() if group is not None)
        if len(value) >= 4 and not _strict_placeholder(value):
            return True
    generic_token = re.compile(
        r"(?i)\btoken\b\s*[:=]\s*['\"]([^'\"\r\n]{12,})['\"]"
    )
    if any(not _strict_placeholder(match.group(1)) for match in generic_token.finditer(text)):
        return True
    bearer = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{16,}")
    if bearer.search(text):
        return True
    direct_patterns = (
        re.escape("sk" + "-") + r"[A-Za-z0-9_-]{12,}",
        r"\bAK" + r"IA[0-9A-Z]{16}\b",
        r"\bAI" + r"za[0-9A-Za-z_-]{25,}\b",
        r"\bgh" + r"[pousr]_[A-Za-z0-9]{20,}\b",
        r"\bgithub" + r"_pat_[A-Za-z0-9_]{20,}\b",
        r"\bgl" + r"pat-[A-Za-z0-9_-]{20,}\b",
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE " + r"KEY-----",
    )
    if any(re.search(pattern, text) for pattern in direct_patterns):
        return True
    schemes = ("postgres", "postgresql", "mysql", "mongodb", "redis", "amqp")
    connection = r"(?i)\b(?:" + "|".join(schemes) + r")(?::|\+srv:)?//[^\s/@:]+:[^\s/@]+@"
    return bool(re.search(connection, text))


def _strict_placeholder(value: str) -> bool:
    exact = {
        "changeme",
        "dummy",
        "example-token",
        "placeholder-token",
        "redacted",
        "test-token",
    }
    return bool(
        value.casefold() in exact
        or re.fullmatch(r"<YOUR_[A-Z0-9_]+>", value)
        or re.fullmatch(r"\$\{[A-Z][A-Z0-9_]*}", value)
    )


def _contains_email(text: str, public_contacts: Sequence[str]) -> bool:
    allowed = {contact.casefold() for contact in public_contacts}
    values = _email_values(text)
    noreply = re.compile(
        r"(?i)^[0-9]+\+[A-Za-z0-9-]+@users\.noreply\.github\.com$"
    )
    git_remote_identity = "git" + "@" + "github.com"
    for value, start, end in values:
        if value.casefold() == git_remote_identity and (
            end < len(text) and text[end] in {":", "/"}
            or text[max(0, start - 6) : start].casefold() == "ssh://"
        ):
            continue
        if value.casefold() in allowed or noreply.fullmatch(value):
            continue
        return True
    return False


def _email_values(text: str) -> tuple[tuple[str, int, int], ...]:
    email_pattern = re.compile(
        r"(?i)(?<![A-Za-z0-9.!#$%&'*+=?^_`{|}~-])"
        r"[A-Z0-9.!#$%&'*+=?^_`{|}~-]+@"
        r"[A-Z0-9](?:[A-Z0-9.-]{0,251}[A-Z0-9])?\.[A-Z]{2,63}\b"
    )
    return tuple(
        (match.group(0), match.start(), match.end())
        for match in email_pattern.finditer(text)
    )


def _contains_local_identity(text: str) -> bool:
    candidates: set[str] = set()
    try:
        hostname = socket.gethostname().strip()
        if len(hostname) >= 5 and hostname.casefold() not in {"localhost", "localhost.localdomain"}:
            candidates.add(hostname)
    except OSError:
        pass
    try:
        username = getpass.getuser().strip()
    except (OSError, KeyError):
        username = ""
    if username and len(username) >= 3:
        local_mail = re.compile(
            rf"(?i)\b{re.escape(username)}@(?!users\.noreply\.github\.com\b)[A-Za-z0-9.-]+\.[A-Za-z]{{2,}}\b"
        )
        if local_mail.search(text):
            return True
    folded = text.casefold()
    return any(candidate.casefold() in folded for candidate in candidates)


def _is_allowed_remote(url: str) -> bool:
    value = url.strip()
    if re.match(r"(?i)^file:", value) or value.startswith(("/", "\\")):
        return False
    git_remote_identity = "git" + "@" + "github.com"
    accepted = {
        f"https://github.com/{_SAFE_REMOTE_REPOSITORY}",
        f"https://github.com/{_SAFE_REMOTE_REPOSITORY}.git",
        f"{git_remote_identity}:{_SAFE_REMOTE_REPOSITORY}",
        f"{git_remote_identity}:{_SAFE_REMOTE_REPOSITORY}.git",
        f"ssh://{git_remote_identity}/{_SAFE_REMOTE_REPOSITORY}",
        f"ssh://{git_remote_identity}/{_SAFE_REMOTE_REPOSITORY}.git",
    }
    return value.rstrip("/") in accepted


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return ()
    return tuple(value)


def _policy_is_safe(policy: Mapping[str, object]) -> bool:
    if policy.get("schema_version") != 1:
        return False
    allowed_dirs = set(_string_values(policy.get("allowed_top_level_directories")))
    allowed_roots = set(_string_values(policy.get("allowed_root_files")))
    forbidden_dirs = _string_set(policy.get("forbidden_directories"))
    forbidden_extensions = _string_set(policy.get("forbidden_extensions"))
    default_dirs = set(_string_values(_DEFAULT_POLICY["allowed_top_level_directories"]))
    default_roots = set(_string_values(_DEFAULT_POLICY["allowed_root_files"]))
    required_forbidden_dirs = _string_set(_DEFAULT_POLICY["forbidden_directories"])
    required_extensions = _string_set(_DEFAULT_POLICY["forbidden_extensions"])
    maximum = policy.get("max_file_bytes")
    private_terms = policy.get("private_terms")
    public_contacts = policy.get("public_contacts")
    return bool(
        allowed_dirs <= default_dirs
        and allowed_roots <= default_roots
        and policy.get("allowed_example_directory")
        == _DEFAULT_POLICY["allowed_example_directory"]
        and required_forbidden_dirs <= forbidden_dirs
        and required_extensions <= forbidden_extensions
        and isinstance(maximum, int)
        and not isinstance(maximum, bool)
        and 0 < maximum <= _MAX_TEXT_SCAN_BYTES
        and isinstance(private_terms, list)
        and all(isinstance(item, str) and item.strip() for item in private_terms)
        and isinstance(public_contacts, list)
        and all(
            isinstance(item, str)
            and item.strip() == item
            and len(_email_values(item)) == 1
            and _email_values(item)[0][0].casefold() == item.casefold()
            for item in public_contacts
        )
    )


def _string_set(value: object) -> set[str]:
    return {item.casefold() for item in _string_values(value)}


def _issue(
    code: str,
    path: str,
    *,
    level: str = "error",
    message: str | None = None,
) -> ReleaseIssue:
    return ReleaseIssue(
        code=code,
        path=path,
        message=message if message is not None else _FIXED_MESSAGES[code],
        level=level,
    )


def _sorted(issues: Iterable[ReleaseIssue]) -> tuple[ReleaseIssue, ...]:
    return tuple(
        sorted(
            issues,
            key=lambda item: (item.level, item.path, item.code, item.message),
        )
    )
