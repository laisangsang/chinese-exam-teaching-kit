"""Fail-closed checks for files intended for a public source release."""

from __future__ import annotations

import getpass
import json
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
}

_FIXED_MESSAGES = {
    "absolute_path": "文本包含本机绝对路径定位信息。",
    "binary_file": "跟踪文件不是受支持的 UTF-8 文本。",
    "config_invalid": "公开发行白名单配置无效。",
    "file_too_large": "跟踪文件超过公开发行大小上限。",
    "forbidden_extension": "该文件类型不得进入公开发行。",
    "forbidden_path": "该路径不在公开发行白名单内。",
    "git_error": "无法安全读取 Git 跟踪状态。",
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
    policy = dict(_DEFAULT_POLICY)
    if allowlist is not None:
        policy.update(allowlist)
    else:
        config = repository / "config" / "public_release_allowlist.json"
        if config.is_file():
            try:
                loaded = json.loads(config.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("policy must be an object")
                policy.update(loaded)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
                issues.append(_issue("config_invalid", "config/public_release_allowlist.json"))
    if not _policy_is_safe(policy):
        issues.append(_issue("config_invalid", "config/public_release_allowlist.json"))
        policy = dict(_DEFAULT_POLICY)

    if tracked is None:
        try:
            entries = _tracked_entries(repository)
            remotes = _remote_urls(repository)
        except _GitAuditFailure:
            return _sorted((*issues, _issue("git_error", ".")))
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
        entries = tuple(("100644", str(relative)) for relative in tracked)

    configured_terms = policy.get("private_terms", ())
    terms: list[str] = []
    if isinstance(configured_terms, list):
        terms.extend(str(item) for item in configured_terms)
    terms.extend(str(item) for item in private_terms)
    terms = [item for item in terms if item.strip()]

    seen: set[str] = set()
    portable_seen: dict[str, str] = {}
    for mode, raw_relative in entries:
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
            issues.append(_issue("invalid_path", safe_relative))
            continue
        portable_seen[portable_key] = safe_relative
        path_codes: list[str] = []
        if _contains_secret(safe_relative):
            path_codes.append("secret_pattern")
        if _contains_local_identity(safe_relative):
            path_codes.append("local_identity")
        folded_path = safe_relative.casefold()
        if any(term.casefold() in folded_path for term in terms):
            path_codes.append("private_term")
        file_issues = _audit_tracked_file(
            repository, safe_relative, mode, policy, terms
        )
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


def _tracked_entries(root: Path) -> tuple[tuple[str, str], ...]:
    inventory = _decode_nul_paths(_git(root, ("ls-files", "-z")))
    index = _git(root, ("ls-files", "-s", "-z"))
    indexed: dict[str, list[tuple[str, str]]] = {}
    for record in index.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            fields = metadata.split(b" ")
            if len(fields) != 3:
                raise ValueError("unexpected index record")
            mode = fields[0].decode("ascii")
            stage = fields[2].decode("ascii")
            path = raw_path.decode("utf-8", errors="strict")
        except (ValueError, UnicodeError):
            raise _GitAuditFailure from None
        indexed.setdefault(path, []).append((mode, stage))
    if set(inventory) != set(indexed):
        raise _GitAuditFailure
    entries: list[tuple[str, str]] = []
    for path in inventory:
        records = indexed[path]
        mode = records[0][0] if len(records) == 1 and records[0][1] == "0" else "conflict"
        entries.append((mode, path))
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
    urls: list[str] = []
    for name in names:
        if not name:
            continue
        raw = _git(root, ("remote", "get-url", "--all", name))
        try:
            urls.extend(line for line in raw.decode("utf-8", errors="strict").splitlines() if line)
        except UnicodeError:
            raise _GitAuditFailure from None
    return tuple(urls)


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
    mode: str,
    policy: Mapping[str, object],
    private_terms: Sequence[str],
) -> list[ReleaseIssue]:
    issues: list[ReleaseIssue] = []
    if not _path_allowed(relative, policy):
        issues.append(_issue("forbidden_path", relative))

    if mode == "120000":
        return [*issues, _issue("tracked_symlink", relative)]
    if mode == "160000":
        return [*issues, _issue("tracked_submodule", relative)]
    if mode not in {"100644", "100755"}:
        return [*issues, _issue("invalid_git_mode", relative)]

    target = root.joinpath(*PurePosixPath(relative).parts)
    cursor = root
    for part in PurePosixPath(relative).parts[:-1]:
        cursor /= part
        try:
            parent_info = cursor.lstat()
        except OSError:
            return [*issues, _issue("missing_tracked_file", relative)]
        if stat.S_ISLNK(parent_info.st_mode):
            return [*issues, _issue("tracked_symlink", relative)]
        if not stat.S_ISDIR(parent_info.st_mode):
            return [*issues, _issue("invalid_git_mode", relative)]
    try:
        info = target.lstat()
    except OSError:
        return [*issues, _issue("missing_tracked_file", relative)]
    if stat.S_ISLNK(info.st_mode):
        return [*issues, _issue("tracked_symlink", relative)]
    if not stat.S_ISREG(info.st_mode):
        return [*issues, _issue("invalid_git_mode", relative)]

    extensions = _string_set(policy.get("forbidden_extensions"))
    if target.suffix.casefold() in extensions:
        issues.append(_issue("forbidden_extension", relative))
        return issues
    maximum = policy.get("max_file_bytes", _MAX_TEXT_SCAN_BYTES)
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
        issues.append(_issue("config_invalid", "config/public_release_allowlist.json"))
        maximum = _MAX_TEXT_SCAN_BYTES
    if info.st_size > maximum:
        issues.append(_issue("file_too_large", relative))
        return issues

    try:
        raw = target.read_bytes()
    except OSError:
        return [*issues, _issue("missing_tracked_file", relative)]
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
    issues.extend(_scan_text(relative, text, private_terms))
    return issues


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
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return None
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    invalid_windows_characters = set('<>:"|?*')
    reserved_windows_names = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in range(1, 10)
    }
    for part in candidate.parts:
        if (
            any(character in invalid_windows_characters for character in part)
            or part.endswith((".", " "))
            or part.split(".", 1)[0].upper() in reserved_windows_names
        ):
            return None
    normalized = candidate.as_posix()
    if normalized != value:
        return None
    return normalized


def _scan_text(relative: str, text: str, private_terms: Sequence[str]) -> list[ReleaseIssue]:
    issues: list[ReleaseIssue] = []
    if _contains_absolute_path(text):
        issues.append(_issue("absolute_path", relative))
    if _contains_secret(text):
        issues.append(_issue("secret_pattern", relative))
    if _contains_local_identity(text):
        issues.append(_issue("local_identity", relative))
    folded = text.casefold()
    if any(term.casefold() in folded for term in private_terms):
        issues.append(_issue("private_term", relative))
    return issues


def _contains_absolute_path(text: str) -> bool:
    file_scheme = "file" + ":" + "//"
    if re.search(re.escape(file_scheme), text, flags=re.IGNORECASE):
        return True
    drive = r"(?<![A-Za-z0-9])[A-Za-z]" + re.escape(":") + r"[\\/]"
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
    host_roots = ("Users", "home", "private", "var", "tmp", "Volumes", "etc", "opt", "root")
    posix = r"(?<![:/.\w])/(?:" + "|".join(host_roots) + r")(?:/|\b)"
    return bool(re.search(posix, text))


def _contains_secret(text: str) -> bool:
    placeholder_words = (
        "changeme",
        "dummy",
        "example",
        "placeholder",
        "redacted",
        "sample",
        "test",
        "your-",
        "your_",
    )
    assignment = re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|"
        r"aws[_-]?secret[_-]?access[_-]?key|connection[_-]?string|private[_-]?key)\b"
        r"\s*[:=]\s*['\"]?([^\s'\",;}{]{8,})"
    )
    for match in assignment.finditer(text):
        value = match.group(1).casefold()
        if not any(word in value for word in placeholder_words) and not value.startswith(("${", "<")):
            return True
    direct_patterns = (
        re.escape("sk" + "-") + r"[A-Za-z0-9_-]{12,}",
        r"\bAK" + r"IA[0-9A-Z]{16}\b",
        r"\bAI" + r"za[0-9A-Za-z_-]{25,}\b",
        r"\bgh" + r"[pousr]_[A-Za-z0-9]{20,}\b",
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE " + r"KEY-----",
    )
    if any(re.search(pattern, text) for pattern in direct_patterns):
        return True
    schemes = ("postgres", "postgresql", "mysql", "mongodb", "redis", "amqp")
    connection = r"(?i)\b(?:" + "|".join(schemes) + r")(?::|\+srv:)?//[^\s/@:]+:[^\s/@]+@"
    return bool(re.search(connection, text))


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
    accepted = {
        f"https://github.com/{_SAFE_REMOTE_REPOSITORY}",
        f"https://github.com/{_SAFE_REMOTE_REPOSITORY}.git",
        f"git@github.com:{_SAFE_REMOTE_REPOSITORY}",
        f"git@github.com:{_SAFE_REMOTE_REPOSITORY}.git",
        f"ssh://git@github.com/{_SAFE_REMOTE_REPOSITORY}",
        f"ssh://git@github.com/{_SAFE_REMOTE_REPOSITORY}.git",
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
