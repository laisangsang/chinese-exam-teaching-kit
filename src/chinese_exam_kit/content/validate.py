"""Deterministic validation for the six public teaching-guide modules."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable, Mapping


DEFAULT_CONTRACT_RESOURCE = "content_contract.json"
HEADING_RE = re.compile(r"(?m)^(?P<marks>#{1,6})[ \t]+(?P<title>[^\n]+?)\s*$")
TEMPLATE_VARIABLE_RE = re.compile(r"\$\{[^{}\n]+\}")
QUESTION_NUMBER_RE = re.compile(r"第?\s*\d+\s*题")
OPTION_REFERENCE_RE = re.compile(r"(?<![A-Za-z])([A-D])\s*项", re.IGNORECASE)
HTML_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)


@dataclass(frozen=True)
class ValidationIssue:
    """One path-safe, serializable content validation result."""

    level: str
    code: str
    path: str
    message: str
    line: int | None = None
    module: str | None = None
    section: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "code": self.code,
            "path": self.path,
            "line": self.line,
            "module": self.module,
            "section": self.section,
            "message": self.message,
        }


@dataclass(frozen=True)
class _Heading:
    title: str
    level: int
    line: int
    start: int
    body_start: int
    end: int
    body: str
    direct_body: str


def _issue_key(issue: ValidationIssue) -> tuple[object, ...]:
    return (
        issue.path,
        _optional_text_key(issue.module),
        _optional_line_key(issue.line),
        issue.level,
        issue.code,
        _optional_text_key(issue.section),
        issue.message,
    )


def _optional_text_key(value: str | None) -> tuple[bool, str]:
    return (value is not None, value if value is not None else "")


def _optional_line_key(value: int | None) -> tuple[bool, int]:
    return (value is not None, value if value is not None else 0)


def _ordered(issues: Iterable[ValidationIssue]) -> tuple[ValidationIssue, ...]:
    return tuple(sorted(issues, key=_issue_key))


def format_issues_json(issues: Iterable[ValidationIssue]) -> str:
    """Return deterministic UTF-8 JSON without exposing caller paths."""
    payload = [issue.to_dict() for issue in _ordered(issues)]
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def format_issues_text(issues: Iterable[ValidationIssue]) -> str:
    """Return deterministic human-readable validation output."""
    ordered = _ordered(issues)
    if not ordered:
        return "内容校验通过（0 个问题）"
    lines = []
    for issue in ordered:
        location = issue.path + (f":{issue.line}" if issue.line is not None else "")
        lines.append(f"[{issue.level}] {issue.code} {location} — {issue.message}")
    lines.append(f"共 {len(ordered)} 个问题")
    return "\n".join(lines)


def _load_contract(contract: Mapping[str, Any] | Path | None) -> Mapping[str, Any]:
    if contract is None:
        payload: Any = json.loads(
            files("chinese_exam_kit.resources")
            .joinpath(DEFAULT_CONTRACT_RESOURCE)
            .read_text(encoding="utf-8")
        )
    elif isinstance(contract, Mapping):
        payload = contract
    else:
        payload = json.loads(Path(contract).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("content contract schema_version must be 1")
    modules = payload.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise ValueError("content contract requires modules")
    return payload


def _display_path(path: Path) -> str:
    return path.name or "."


def _without_html_comments(text: str) -> str:
    """Hide complete or unclosed comments while preserving offsets and line numbers."""

    def hide(match: re.Match[str]) -> str:
        return "".join("\n" if character == "\n" else " " for character in match.group())

    return HTML_COMMENT_RE.sub(hide, text)


def _headings(text: str) -> tuple[_Heading, ...]:
    visible = _without_html_comments(text)
    matches = tuple(HEADING_RE.finditer(visible))
    result: list[_Heading] = []
    for index, match in enumerate(matches):
        level = len(match.group("marks"))
        end = len(text)
        direct_end = len(text)
        if index + 1 < len(matches):
            direct_end = matches[index + 1].start()
        for following in matches[index + 1 :]:
            if len(following.group("marks")) <= level:
                end = following.start()
                break
        result.append(
            _Heading(
                title=match.group("title").strip(),
                level=level,
                line=text.count("\n", 0, match.start()) + 1,
                start=match.start(),
                body_start=match.end(),
                end=end,
                body=visible[match.end() : end],
                direct_body=visible[match.end() : direct_end],
            )
        )
    return tuple(result)


def _normalized_heading(title: str) -> str:
    value = re.sub(r"[*_`]+", "", title).strip()
    value = re.sub(r"^【|】$", "", value).strip()
    return value


def _substantive_characters(body: str) -> int:
    body = _without_html_comments(body)
    lines = []
    for line in body.splitlines():
        if HEADING_RE.match(line) or line.lstrip().startswith("<!--"):
            continue
        cleaned = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)、]\s*)", "", line)
        cleaned = re.sub(r"[`*_>#|]", "", cleaned)
        cleaned = TEMPLATE_VARIABLE_RE.sub("", cleaned)
        lines.append(cleaned.strip())
    return len("".join(lines))


def _matching_heading(headings: tuple[_Heading, ...], expected: str) -> _Heading | None:
    for heading in headings:
        title = _normalized_heading(heading.title)
        if title == expected or title.startswith(expected + "：") or title.startswith(expected + ":"):
            return heading
    return None


def _field_has_value(text: str, aliases: Iterable[str]) -> bool:
    alternatives = "|".join(re.escape(alias) for alias in aliases)
    pattern = re.compile(
        rf"(?m)^\s*(?:[-*+]\s*)?(?:\*\*)?(?:{alternatives})(?:\*\*)?\s*[：:]\s*(?P<value>\S.*)$"
    )
    return any(_substantive_characters(match.group("value")) > 0 for match in pattern.finditer(text))


def _question_name(heading: _Heading) -> str:
    return TEMPLATE_VARIABLE_RE.sub("变量", _normalized_heading(heading.title))


def _validate_common(
    text: str,
    *,
    display_path: str,
    module_id: str | None,
    contract: Mapping[str, Any],
    template: bool,
) -> list[ValidationIssue]:
    rules = contract.get("validation", {})
    issues: list[ValidationIssue] = []
    placeholder_tokens = rules.get("standalone_placeholders", [])
    if isinstance(placeholder_tokens, list) and placeholder_tokens:
        normalized_tokens = {str(token).casefold(): str(token) for token in placeholder_tokens}
        for line_number, line in enumerate(text.splitlines(), 1):
            token = _standalone_placeholder(line, normalized_tokens)
            if token is not None:
                issues.append(
                    ValidationIssue(
                        "error",
                        "placeholder",
                        display_path,
                        f"不得保留占位表达：{token}",
                        line_number,
                        module_id,
                    )
                )
    if not template:
        for match in TEMPLATE_VARIABLE_RE.finditer(text):
            issues.append(
                ValidationIssue(
                    "error",
                    "template_variable",
                    display_path,
                    "正式内容不得保留模板变量",
                    text.count("\n", 0, match.start()) + 1,
                    module_id,
                )
            )
    return issues


def _standalone_placeholder(
    line: str, normalized_tokens: Mapping[str, str]
) -> str | None:
    """Return a full-line placeholder after removing common Markdown prefixes."""
    value = line.strip()
    value = re.sub(r"^(?:>\s*)+", "", value)
    value = re.sub(r"^#{1,6}\s+", "", value)
    value = re.sub(r"^(?:[-*+]|\d+[.)、])\s+", "", value)
    value = re.sub(r"^\[[ xX]\]\s+", "", value)
    value = value.rstrip("。；;").strip()
    for wrapper in ("**", "__", "`"):
        if (
            value.startswith(wrapper)
            and value.endswith(wrapper)
            and len(value) > len(wrapper) * 2
        ):
            value = value[len(wrapper) : -len(wrapper)].strip()
    return normalized_tokens.get(value.casefold())


def _validate_sections(
    text: str,
    *,
    display_path: str,
    module_id: str,
    module: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> list[ValidationIssue]:
    headings = _headings(text)
    minimum = int(contract.get("validation", {}).get("minimum_section_characters", 6))
    issues: list[ValidationIssue] = []
    for section in module.get("required_sections", []):
        expected = str(section)
        heading = _matching_heading(headings, expected)
        if heading is None:
            issues.append(
                ValidationIssue(
                    "error",
                    "missing_required_section",
                    display_path,
                    f"缺少必备章节：{expected}",
                    module=module_id,
                    section=expected,
                )
            )
        elif _substantive_characters(heading.body) < minimum:
            issues.append(
                ValidationIssue(
                    "error",
                    "empty_required_section",
                    display_path,
                    f"必备章节缺少实质内容：{expected}",
                    heading.line,
                    module_id,
                    expected,
                )
            )
    return issues


def _validate_evidence_layers(
    text: str,
    *,
    display_path: str,
    module_id: str,
    contract: Mapping[str, Any],
) -> list[ValidationIssue]:
    headings = _headings(text)
    minimum = int(contract.get("validation", {}).get("minimum_section_characters", 6))
    issues: list[ValidationIssue] = []
    for layer in contract.get("validation", {}).get("evidence_layers", []):
        name = str(layer)
        heading = _matching_heading(headings, name)
        if heading is None:
            issues.append(
                ValidationIssue(
                    "error",
                    "missing_evidence_layer",
                    display_path,
                    f"缺少证据分层：{name}",
                    module=module_id,
                    section=name,
                )
            )
        elif _substantive_characters(heading.body) < minimum:
            issues.append(
                ValidationIssue(
                    "error",
                    "empty_evidence_layer",
                    display_path,
                    f"证据分层缺少实质内容：{name}",
                    heading.line,
                    module_id,
                    name,
                )
            )
    return issues


def _question_headings(text: str, kind: str) -> tuple[_Heading, ...]:
    headings = _headings(text)
    selected: list[_Heading] = []
    for heading in headings:
        title = _normalized_heading(heading.title)
        explicit = re.fullmatch(rf"{re.escape(kind)}\s*[：:]\s*\S.*", title)
        if explicit:
            selected.append(heading)
        elif (
            kind == "选择题"
            and QUESTION_NUMBER_RE.search(title)
            and _has_direct_option_marker(heading)
        ):
            selected.append(heading)
    return tuple(selected)


def _has_direct_option_marker(heading: _Heading) -> bool:
    if OPTION_REFERENCE_RE.search(heading.direct_body):
        return True
    return any(
        child.level == heading.level + 1
        and re.fullmatch(
            r"[A-Da-d]\s*项(?:[：:].*)?", _normalized_heading(child.title)
        )
        for child in _headings(heading.body)
    )


def _option_sections(block: _Heading) -> Mapping[str, _Heading]:
    sections: dict[str, _Heading] = {}
    for heading in _headings(block.body):
        if heading.level != block.level + 1:
            continue
        match = re.fullmatch(r"([A-Da-d])\s*项(?:[：:].*)?", _normalized_heading(heading.title))
        if match:
            sections[match.group(1).upper()] = heading
    return sections


def _validate_choice_evidence(
    text: str,
    *,
    display_path: str,
    module_id: str,
    module: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> list[ValidationIssue]:
    if not module.get("requires_choice_evidence", False):
        return []
    blocks = _question_headings(text, "选择题")
    if not blocks:
        return [
            ValidationIssue(
                "error",
                "missing_option_evidence",
                display_path,
                "缺少按题、按选项组织的证据核对",
                module=module_id,
                section="选择题逐项证据",
            )
        ]
    choices = tuple(str(item) for item in contract["validation"].get("choice_options", []))
    fields = contract["validation"].get("option_evidence_fields", {})
    issues: list[ValidationIssue] = []
    for block in blocks:
        sections = _option_sections(block)
        for choice in choices:
            section = sections.get(choice)
            missing = []
            if section is None:
                missing = list(fields)
            else:
                for field, aliases in fields.items():
                    if not _field_has_value(
                        section.direct_body, tuple(str(item) for item in aliases)
                    ):
                        missing.append(field)
            if missing:
                issues.append(
                    ValidationIssue(
                        "error",
                        "missing_option_evidence",
                        display_path,
                        f"{_question_name(block)}的 {choice} 项缺少：{'、'.join(missing)}",
                        block.line,
                        module_id,
                        "选择题逐项证据",
                    )
                )
    return issues


def _validate_subjective_chain(
    text: str,
    *,
    display_path: str,
    module_id: str,
    module: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> list[ValidationIssue]:
    if not module.get("requires_subjective_chain", False):
        return []
    blocks = _question_headings(text, "主观题")
    if not blocks:
        return [
            ValidationIssue(
                "error",
                "missing_subjective_chain",
                display_path,
                "缺少按题组织的主观题答案生成链",
                module=module_id,
                section="主观题答案生成",
            )
        ]
    fields = contract["validation"].get("subjective_steps", {})
    issues: list[ValidationIssue] = []
    for block in blocks:
        missing = [
            field
            for field, aliases in fields.items()
            if not _field_has_value(
                block.direct_body, tuple(str(item) for item in aliases)
            )
        ]
        if missing:
            issues.append(
                ValidationIssue(
                    "error",
                    "missing_subjective_step",
                    display_path,
                    f"{_question_name(block)}缺少：{'、'.join(missing)}",
                    block.line,
                    module_id,
                    "主观题答案生成",
                )
            )
    return issues


def validate_file(
    path: Path,
    module_id: str,
    contract: Mapping[str, Any] | Path | None = None,
    *,
    template: bool = False,
) -> tuple[ValidationIssue, ...]:
    """Validate one module Markdown file against the public contract."""
    source = Path(path)
    display_path = _display_path(source)
    try:
        loaded = _load_contract(contract)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return (
            ValidationIssue(
                "error", "invalid_contract", display_path, "内容合同无法读取或格式无效"
            ),
        )
    modules = loaded["modules"]
    if module_id not in modules:
        return (
            ValidationIssue(
                "error", "invalid_module", display_path, "未知的内容板块", module="unknown"
            ),
        )
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return (
            ValidationIssue(
                "error",
                "unreadable_source",
                display_path,
                "Markdown 源文件无法以 UTF-8 读取",
                module=module_id,
            ),
        )
    module = modules[module_id]
    issues = _validate_common(
        text,
        display_path=display_path,
        module_id=module_id,
        contract=loaded,
        template=template,
    )
    issues.extend(
        _validate_sections(
            text,
            display_path=display_path,
            module_id=module_id,
            module=module,
            contract=loaded,
        )
    )
    issues.extend(
        _validate_evidence_layers(
            text,
            display_path=display_path,
            module_id=module_id,
            contract=loaded,
        )
    )
    issues.extend(
        _validate_choice_evidence(
            text,
            display_path=display_path,
            module_id=module_id,
            module=module,
            contract=loaded,
        )
    )
    issues.extend(
        _validate_subjective_chain(
            text,
            display_path=display_path,
            module_id=module_id,
            module=module,
            contract=loaded,
        )
    )
    return _ordered(issues)


def validate_content_dir(
    content_dir: Path,
    contract: Mapping[str, Any] | Path | None = None,
) -> tuple[ValidationIssue, ...]:
    """Validate direct Markdown children without following links or recursion."""
    root = Path(content_dir)
    root_name = _display_path(root)
    if root.is_symlink():
        return (
            ValidationIssue(
                "error", "symlink_not_allowed", root_name, "内容目录不得是符号链接"
            ),
        )
    if not root.is_dir():
        return (
            ValidationIssue(
                "error", "content_dir_missing", root_name, "内容目录不存在或不是目录"
            ),
        )
    try:
        loaded = _load_contract(contract)
        entries = tuple(sorted(root.iterdir(), key=lambda item: item.name))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return (
            ValidationIssue(
                "error", "unreadable_content_dir", root_name, "内容目录或合同无法读取"
            ),
        )
    markdown_entries = tuple(path for path in entries if path.suffix.lower() == ".md")
    if not markdown_entries:
        return (
            ValidationIssue(
                "error", "no_markdown_sources", root_name, "内容目录中没有 Markdown 源稿"
            ),
        )
    filename_modules = loaded.get("module_files", {})
    overview_files = frozenset(str(item) for item in loaded.get("overview_files", ()))
    issues: list[ValidationIssue] = []
    for path in markdown_entries:
        if path.is_symlink():
            issues.append(
                ValidationIssue(
                    "error", "symlink_not_allowed", path.name, "内容源稿不得是符号链接"
                )
            )
            continue
        if not path.is_file():
            issues.append(
                ValidationIssue("error", "invalid_source", path.name, "内容源稿不是常规文件")
            )
            continue
        module_id = filename_modules.get(path.name)
        if module_id is None and path.name not in overview_files:
            issues.append(
                ValidationIssue(
                    "error",
                    "unknown_module_file",
                    path.name,
                    "Markdown 文件名不在公开内容白名单中",
                    module="unknown",
                )
            )
        elif module_id is None:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                issues.append(
                    ValidationIssue(
                        "error", "unreadable_source", path.name, "Markdown 源文件无法以 UTF-8 读取"
                    )
                )
                continue
            issues.extend(
                _validate_common(
                    text,
                    display_path=path.name,
                    module_id=None,
                    contract=loaded,
                    template=False,
                )
            )
        else:
            issues.extend(validate_file(path, str(module_id), loaded))
    return _ordered(issues)
