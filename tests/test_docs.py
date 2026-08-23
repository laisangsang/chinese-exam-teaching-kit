from __future__ import annotations

import re
import shlex
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from chinese_exam_kit.cli import _parser


ROOT = Path(__file__).parents[1]
README = ROOT / "README.md"
HUMAN_GUIDES = (
    ROOT / "docs/快速开始.md",
    ROOT / "docs/完整安装说明.md",
    ROOT / "docs/原创示例教程.md",
    ROOT / "docs/自动流水线说明.md",
    ROOT / "docs/六板块分析规范.md",
    ROOT / "docs/知识库说明.md",
    ROOT / "docs/Word生成与版式验收.md",
    ROOT / "docs/兼容性.md",
    ROOT / "docs/常见问题.md",
    ROOT / "docs/架构与开发.md",
)
AGENT_GUIDES = (
    ROOT / "agent-guides/通用智能体指南.md",
    ROOT / "agent-guides/Codex.md",
    ROOT / "agent-guides/WorkBuddy-CodeBuddy.md",
    ROOT / "agent-guides/Claude-Code.md",
)
ENTRY_FILES = (
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / ".codebuddy/rules/project/RULE.mdc",
)
DOCUMENTS = (README, *HUMAN_GUIDES, *AGENT_GUIDES, *ENTRY_FILES)
OFFICIAL_EXTERNAL_URLS = {
    "https://github.com/laisangsang/chinese-exam-teaching-kit.git",
    "https://code.claude.com/docs/en/memory",
    "https://cloud.tencent.com/document/product/1831/134362",
    "https://developers.openai.com/",
    "https://developers.openai.com/codex/agent-configuration/agents-md",
}


def _links(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [match.group(1).strip() for match in re.finditer(r"\[[^\]]*\]\((<[^>]+>|[^)]+)\)", text)]


def _broken_local_links(path: Path) -> list[str]:
    broken: list[str] = []
    for raw in _links(path):
        destination = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        parsed = urlsplit(destination)
        if parsed.scheme or destination.startswith("#"):
            continue
        local = unquote(parsed.path)
        target = (path.parent / local).resolve()
        if not target.exists():
            broken.append(raw)
    return broken


def _documented_cekit_commands(text: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for line in text.splitlines():
        candidate = line.strip()
        if candidate.startswith("$ "):
            candidate = candidate[2:]
        if candidate.startswith("cekit "):
            commands.append(shlex.split(candidate))
    return commands


def test_public_document_set_exists_and_all_local_links_resolve():
    missing = [path.relative_to(ROOT).as_posix() for path in DOCUMENTS if not path.is_file()]
    assert missing == []
    broken = {
        path.relative_to(ROOT).as_posix(): _broken_local_links(path)
        for path in DOCUMENTS
        if _broken_local_links(path)
    }
    assert broken == {}


def test_local_link_checker_handles_unicode_and_angle_destinations(tmp_path):
    target = tmp_path / "子目录" / "原创 示例.md"
    target.parent.mkdir()
    target.write_text("# 原创\n", encoding="utf-8")
    source = tmp_path / "索引.md"
    source.write_text("[示例](<子目录/原创%20示例.md>)\n", encoding="utf-8")

    assert _broken_local_links(source) == []


def test_readme_has_three_audience_entrances_and_core_five_minute_commands():
    text = README.read_text(encoding="utf-8")
    for entrance in ("教师入口", "智能体入口", "开发者入口"):
        assert entrance in text
    for command in ("cekit doctor", "cekit init", "cekit run", "cekit validate", "cekit build"):
        assert command in text
    for promise_boundary in ("本地离线", "版权", "隐私", "人工逐页"):
        assert promise_boundary in text


def test_readme_first_screen_has_required_navigation_in_brief_order():
    text = README.read_text(encoding="utf-8")
    first_detailed_install = text.index("## 完整安装与详细流程")
    required_order = (
        "# Chinese Exam Teaching Kit",
        "版权与隐私先行",
        "## 三类入口",
        "## 五分钟原创示例",
        "## 能力边界",
        "## 支持矩阵",
        "## 项目状态与贡献",
    )
    positions = [text.index(item) for item in required_order]

    assert positions == sorted(positions)
    assert all(position < first_detailed_install for position in positions)


def test_every_documented_cekit_invocation_matches_the_real_parser():
    commands = [
        command
        for path in DOCUMENTS
        for command in _documented_cekit_commands(path.read_text(encoding="utf-8"))
    ]
    assert commands
    parser = _parser()
    for command in commands:
        assert command[0] == "cekit"
        try:
            parser.parse_args(command[1:])
        except SystemExit as error:
            pytest.fail(f"文档命令与 CLI 不一致：{shlex.join(command)}（退出码 {error.code}）")


def test_agent_entry_files_are_thin_routes_to_one_normative_contract():
    for path in ENTRY_FILES:
        text = path.read_text(encoding="utf-8")
        assert len(text.encode("utf-8")) < 12_000
        assert "docs/六板块分析规范.md" in text
        assert "docs/自动流水线说明.md" in text
        assert "content/" in text
        assert "knowledge/" in text
    claude_lines = [line.strip() for line in (ROOT / "CLAUDE.md").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert claude_lines[0] == "@AGENTS.md"
    rule = (ROOT / ".codebuddy/rules/project/RULE.mdc").read_text(encoding="utf-8")
    assert rule.startswith("---\n")
    assert "alwaysApply: true" in rule.split("---", 2)[1]


def test_external_links_are_limited_to_verified_official_sources():
    external = {
        raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        for path in DOCUMENTS
        for raw in _links(path)
        if urlsplit(raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw).scheme
    }
    assert external <= OFFICIAL_EXTERNAL_URLS
    assert {
        "https://code.claude.com/docs/en/memory",
        "https://cloud.tencent.com/document/product/1831/134362",
        "https://developers.openai.com/codex/agent-configuration/agents-md",
    } <= external


def test_public_docs_have_no_placeholders_or_host_specific_paths():
    placeholder = re.compile(r"\b(?:TODO|TBD)\b|待补充|稍后补充", re.IGNORECASE)
    posix_home = re.compile(r"/(?:Users|home)/[^/\s]+/")
    windows_home = re.compile(r"[A-Za-z]:\\(?:Users|Documents and Settings)\\")
    for path in DOCUMENTS:
        text = path.read_text(encoding="utf-8")
        assert placeholder.search(text) is None, path
        assert posix_home.search(text) is None, path
        assert windows_home.search(text) is None, path


def test_current_main_docs_do_not_claim_the_public_release_is_still_private_or_unreleased():
    current_status_docs = (
        ROOT / "README.md",
        ROOT / "CHANGELOG.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "SECURITY.md",
        ROOT / "docs/架构与开发.md",
        ROOT / "docs/完整安装说明.md",
        ROOT / "docs/release/v0.1.0-checklist.md",
    )
    stale_phrases = (
        "GitHub 尚未公开",
        "尚未创建正式 Release",
        "正在准备首次公开发行",
        "首次公开发行准备阶段",
        "待公开仓库配置",
        "未声明远端 CI 已验证",
    )

    for path in current_status_docs:
        text = path.read_text(encoding="utf-8")
        assert not any(phrase in text for phrase in stale_phrases), path

    checklist = (ROOT / "docs/release/v0.1.0-checklist.md").read_text(encoding="utf-8")
    assert "历史快照" in checklist
    assert "最终结果" in checklist
