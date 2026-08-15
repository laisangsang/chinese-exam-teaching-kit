import json
import os
import re
from pathlib import Path

import pytest

from chinese_exam_kit.content.validate import (
    ValidationIssue,
    format_issues_json,
    format_issues_text,
    validate_content_dir,
    validate_file,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_reading_one_requires_all_option_evidence(tmp_path):
    source = _write(
        tmp_path / "01.md",
        "# 阅读一\n\n## 第1题\n\n【文本推导】A项正确。",
    )

    issues = validate_file(source, "reading_1")

    assert any(issue.code == "missing_option_evidence" for issue in issues)


def test_option_evidence_is_checked_per_option_not_by_global_keywords(tmp_path):
    source = _write(
        tmp_path / "01.md",
        """# 阅读一

## 全部试题精讲

### 选择题：甲

#### A项

- 原文位置：材料首段。
- 选项改写：替换限定词。
- 正误判断：错误。
- 设误类型：范围扩大。

#### B项

- 原文位置：材料次段。
- 选项改写：保持原意。
- 正误判断：正确。

#### C项

- 原文位置：材料末段。
- 选项改写：改变关系。
- 正误判断：错误。
- 设误类型：因果倒置。

#### D项

- 原文位置：材料末段。
- 选项改写：转述结论。
- 正误判断：正确。
- 设误类型：无。
""",
    )

    issues = validate_file(source, "reading_1")

    assert any(
        issue.code == "missing_option_evidence" and "B" in issue.message
        for issue in issues
    )


def test_subjective_answer_chain_is_checked_inside_each_question(tmp_path):
    source = _write(
        tmp_path / "01.md",
        """# 阅读一

## 主观题答案生成

### 主观题：甲

- 审题：明确任务。
- 证据：定位首段。
- 关系：合并同类信息。
- 评分点：写出两点。
- 参考答案：使用原创概括。
- 失分诊断：避免遗漏限定词。

### 主观题：乙

- 审题：明确任务。
- 证据：定位末段。
- 参考答案：使用原创概括。
- 失分诊断：避免照抄。
""",
    )

    issues = validate_file(source, "reading_1")

    assert any(
        issue.code == "missing_subjective_step" and "乙" in issue.message
        for issue in issues
    )


def test_nested_question_cannot_lend_subjective_steps_to_its_parent(tmp_path):
    source = _write(
        tmp_path / "01.md",
        """# 阅读一

## 主观题答案生成

### 主观题：甲

- 审题：明确甲题任务。

#### 主观题：乙

- 证据：定位乙题原文。
- 关系：整合乙题信息。
- 评分点：形成乙题要点。
- 参考答案：组织乙题答案。
- 失分诊断：说明乙题风险。
""",
    )

    issues = validate_file(source, "reading_1")

    assert any(
        issue.code == "missing_subjective_step"
        and "主观题：甲" in issue.message
        and "证据" in issue.message
        for issue in issues
    )


def test_nested_option_cannot_supply_fields_to_a_direct_option(tmp_path):
    source = _write(
        tmp_path / "01.md",
        """# 阅读一

## 逐选项证据

### 选择题：甲

#### A项

- 原文位置：甲题首段。

##### B项

- 原文位置：嵌套位置。
- 选项改写：嵌套改写。
- 正误判断：嵌套判断。
- 设误类型：嵌套类型。

#### C项

- 原文位置：甲题末段。
- 选项改写：甲题改写。
- 正误判断：甲题判断。
- 设误类型：甲题类型。

#### D项

- 原文位置：甲题末段。
- 选项改写：甲题改写。
- 正误判断：甲题判断。
- 设误类型：甲题类型。
""",
    )

    issues = validate_file(source, "reading_1")

    assert any(
        issue.code == "missing_option_evidence"
        and "A 项" in issue.message
        and "选项改写" in issue.message
        for issue in issues
    )
    assert any(
        issue.code == "missing_option_evidence" and "B 项" in issue.message
        for issue in issues
    )


def test_subjective_container_heading_is_not_a_question_instance(tmp_path):
    source = _write(
        tmp_path / "01.md",
        """# 阅读一

## 主观题答案生成

- 审题：这是章节操作说明。
- 证据：这是章节操作说明。
- 关系：这是章节操作说明。
- 评分点：这是章节操作说明。
- 参考答案：这是章节操作说明。
- 失分诊断：这是章节操作说明。
""",
    )

    issues = validate_file(source, "reading_1")

    assert any(issue.code == "missing_subjective_chain" for issue in issues)


def test_numbered_question_does_not_borrow_choice_markers_from_nested_question(tmp_path):
    source = _write(
        tmp_path / "01.md",
        """# 阅读一

## 第1题

这是第一题的普通说明。

### 第2题

【文本推导】A项表述需要逐项核对。
""",
    )

    issues = validate_file(source, "reading_1")
    option_messages = [
        issue.message for issue in issues if issue.code == "missing_option_evidence"
    ]

    assert any("第2题" in message for message in option_messages)
    assert not any("第1题" in message for message in option_messages)


def _complete_option_question(label: str) -> str:
    options = []
    for option in "ABCD":
        options.append(
            f"""#### {option}项

- 原文位置：{label}的证据位置。
- 选项改写：{label}的选项改写。
- 正误判断：{label}的正误判断。
- 设误类型：{label}的设误说明。
"""
        )
    return f"### 选择题：{label}\n\n" + "\n".join(options)


def _complete_subjective_question(label: str) -> str:
    return f"""### 主观题：{label}

- 审题：明确{label}的任务。
- 证据：定位{label}的证据。
- 关系：整合{label}的信息。
- 评分点：拆分{label}的要点。
- 参考答案：组织{label}的答案。
- 失分诊断：诊断{label}的风险。
"""


def test_multiple_choice_and_subjective_questions_are_validated_independently(tmp_path):
    source = _write(
        tmp_path / "01.md",
        "# 阅读一\n\n## 逐选项证据\n\n"
        + _complete_option_question("甲")
        + "\n"
        + _complete_option_question("乙")
        + "\n## 主观题答案生成\n\n"
        + _complete_subjective_question("丙")
        + "\n"
        + _complete_subjective_question("丁"),
    )

    issues = validate_file(source, "reading_1")

    assert not any(issue.code == "missing_option_evidence" for issue in issues)
    assert not any(
        issue.code in {"missing_subjective_chain", "missing_subjective_step"}
        for issue in issues
    )


def test_evidence_layers_require_substantive_content_not_label_mentions(tmp_path):
    source = _write(
        tmp_path / "01.md",
        """# 阅读一

本讲义将区分【官方评分参考】【文本推导】【教学拓展】。

## 【官方评分参考】

## 【文本推导】

有文本证据支撑的分析。
""",
    )

    issues = validate_file(source, "reading_1")

    assert any(
        issue.code == "empty_evidence_layer" and issue.section == "官方评分参考"
        for issue in issues
    )
    assert any(
        issue.code == "missing_evidence_layer" and issue.section == "教学拓展"
        for issue in issues
    )


def test_required_section_must_be_a_heading_with_substantive_body(tmp_path):
    source = _write(
        tmp_path / "03.md",
        """# 文言文

正文提到全文逐句注释，但这里不是相应章节。

## 重点字词拓展

## 特殊句式

根据语境辨析句式依据。
""",
    )

    issues = validate_file(source, "classical_chinese")

    assert any(
        issue.code == "missing_required_section" and issue.section == "全文逐句注释"
        for issue in issues
    )
    assert any(
        issue.code == "empty_required_section" and issue.section == "重点字词拓展"
        for issue in issues
    )


def test_multiline_and_unclosed_html_comments_do_not_fill_required_sections(tmp_path):
    source = _write(
        tmp_path / "03.md",
        """# 文言文

## 全文逐句注释

<!--
这段注释很长，但不属于教师可见内容。
-->

## 重点字词拓展

<!-- 未闭合注释也不能充当可见内容。
这里仍然只是注释。
""",
    )

    issues = validate_file(source, "classical_chinese")

    assert any(
        issue.code == "empty_required_section" and issue.section == "全文逐句注释"
        for issue in issues
    )
    assert any(
        issue.code == "empty_required_section" and issue.section == "重点字词拓展"
        for issue in issues
    )


def test_html_comments_cannot_create_or_fill_evidence_layers(tmp_path):
    source = _write(
        tmp_path / "01.md",
        """# 阅读一

## 【官方评分参考】

<!--
注释中的正式答案说明不可见。
-->

<!--
## 【文本推导】

注释中的标题与推导均不可见。
-->
""",
    )

    issues = validate_file(source, "reading_1")

    assert any(
        issue.code == "empty_evidence_layer" and issue.section == "官方评分参考"
        for issue in issues
    )
    assert any(
        issue.code == "missing_evidence_layer" and issue.section == "文本推导"
        for issue in issues
    )


@pytest.mark.parametrize(
    ("module_id", "distinct_section"),
    (
        ("reading_1", "多材料关系"),
        ("reading_2", "关键段落五维分析"),
        ("classical_chinese", "翻译题采分点"),
        ("poetry", "逐句翻译与赏析"),
        ("language_use", "通用检查顺序"),
        ("composition", "素材运用边界"),
    ),
)
def test_each_module_exposes_its_distinct_required_structure(
    tmp_path, module_id, distinct_section
):
    source = _write(tmp_path / "source.md", "# 原创讲评\n\n说明文字足够长。\n")

    issues = validate_file(source, module_id)

    assert any(
        issue.code == "missing_required_section" and issue.section == distinct_section
        for issue in issues
    )


def test_formal_content_rejects_template_variables_and_standalone_placeholders(tmp_path):
    source = _write(
        tmp_path / "01.md",
        "# ${材料标题}\n\n## 逐段批注\n\n待补充\n",
    )

    issues = validate_file(source, "reading_1")

    assert "template_variable" in {issue.code for issue in issues}
    assert "placeholder" in {issue.code for issue in issues}


def test_template_mode_accepts_anonymous_variables_but_not_placeholder_lines(tmp_path):
    source = _write(
        tmp_path / "template.md",
        "# ${材料标题}\n\n## 操作说明\n\n在此记录材料的结构与证据。\n",
    )

    issues = validate_file(source, "reading_1", template=True)

    assert not any(issue.code == "template_variable" for issue in issues)


def test_unknown_module_and_invalid_utf8_are_structured_without_absolute_paths(tmp_path):
    source = tmp_path / "secret.md"
    source.write_bytes(b"\xff")

    invalid_module = validate_file(source, "not-a-module")
    unreadable = validate_file(source, "reading_1")

    assert [issue.code for issue in invalid_module] == ["invalid_module"]
    assert [issue.code for issue in unreadable] == ["unreadable_source"]
    rendered = format_issues_json((*invalid_module, *unreadable))
    assert str(tmp_path) not in rendered
    assert "secret.md" in rendered


@pytest.mark.parametrize(
    "module_id",
    ("/Users/private/secret", r"C:\\private\\secret", r"\\\\server\\private"),
)
def test_unknown_module_identifier_is_not_reflected_in_results(tmp_path, module_id):
    source = _write(tmp_path / "source.md", "# 原创内容\n")

    issues = validate_file(source, module_id)
    json_output = format_issues_json(issues)
    text_output = format_issues_text(issues)

    assert [issue.code for issue in issues] == ["invalid_module"]
    assert issues[0].module == "unknown"
    assert module_id not in json_output
    assert module_id not in text_output


def test_issue_renderers_are_deterministic_machine_and_human_outputs(tmp_path):
    source = _write(tmp_path / "01.md", "# 阅读一\n\nTODO\n")
    issues = validate_file(source, "reading_1")

    first = format_issues_json(reversed(issues))
    second = format_issues_json(issues)
    payload = json.loads(first)

    assert first == second
    assert payload == sorted(
        payload,
        key=lambda item: (
            item["path"],
            item["line"] or 0,
            item["code"],
            item["message"],
        ),
    )
    assert format_issues_text(reversed(issues)) == format_issues_text(issues)
    assert all("code" in item and "level" in item for item in payload)


def test_issue_order_uses_every_serialized_field():
    issues = (
        ValidationIssue(
            "warning",
            "same_code",
            "same.md",
            "same message",
            line=7,
            module="reading_1",
            section="乙",
        ),
        ValidationIssue(
            "error",
            "same_code",
            "same.md",
            "same message",
            line=7,
            module="reading_1",
            section="甲",
        ),
    )

    assert format_issues_json(issues) == format_issues_json(reversed(issues))
    assert format_issues_text(issues) == format_issues_text(reversed(issues))


@pytest.mark.parametrize(
    "line", ("> TODO", "1. 待补充", "- [ ] TBD", "> - [x] 同上", "**TODO**。")
)
def test_markdown_prefixed_standalone_placeholders_are_rejected(tmp_path, line):
    source = _write(tmp_path / "01.md", f"# 阅读一\n\n{line}\n")

    issues = validate_file(source, "reading_1")

    assert any(issue.code == "placeholder" for issue in issues)


@pytest.mark.parametrize(
    "sentence",
    ("TODO 项已全部完成。", "此处待补充的是方法说明，而非占位符。", "同上观点仍需结合语境辨析。"),
)
def test_placeholder_words_inside_natural_sentences_are_allowed(tmp_path, sentence):
    source = _write(tmp_path / "01.md", f"# 阅读一\n\n{sentence}\n")

    issues = validate_file(source, "reading_1")

    assert not any(issue.code == "placeholder" for issue in issues)


def test_content_directory_rejects_symlinks_without_following_them(tmp_path):
    content = tmp_path / "content"
    content.mkdir()
    outside = _write(tmp_path / "outside.md", "# 外部内容\n\n${敏感变量}\n")
    link = content / "01_阅读一_信息类文本详案.md"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")

    issues = validate_content_dir(content)

    assert [issue.code for issue in issues] == ["symlink_not_allowed"]
    assert str(outside) not in format_issues_json(issues)


def test_content_directory_reports_missing_or_empty_directories_without_leaking_path(tmp_path):
    missing = validate_content_dir(tmp_path / "private-missing")
    empty_dir = tmp_path / "private-empty"
    empty_dir.mkdir()
    empty = validate_content_dir(empty_dir)

    assert [issue.code for issue in missing] == ["content_dir_missing"]
    assert [issue.code for issue in empty] == ["no_markdown_sources"]
    assert str(tmp_path) not in format_issues_json((*missing, *empty))


def test_content_directory_rejects_unrecognized_markdown_files(tmp_path):
    content = tmp_path / "content"
    content.mkdir()
    _write(content / "notes.md", "# 普通说明\n\n这里没有占位内容。\n")

    issues = validate_content_dir(content)

    assert [issue.code for issue in issues] == ["unknown_module_file"]


def test_content_directory_allows_the_whitelisted_overview_file(tmp_path):
    content = tmp_path / "content"
    content.mkdir()
    _write(
        content / "00_整卷总览与讲评建议.md",
        "# 整卷总览\n\n这是已完成的原创总览说明。\n",
    )

    issues = validate_content_dir(content)

    assert issues == ()


def test_public_templates_are_complete_original_scaffolds_without_placeholder_lines():
    expected = {
        "00_整卷总览与讲评建议.md",
        "01_阅读一_信息类文本详案.md",
        "02_阅读二_文学类文本详案.md",
        "03_阅读三_文言文详案.md",
        "04_阅读四_古诗文详案.md",
        "05_语言文字运用详案.md",
        "06_作文审题与写作指导详案.md",
    }
    paths = tuple(sorted(Path("templates").glob("*.md")))
    placeholder_line = re.compile(
        r"^\s*(?:#{1,6}\s*)?(?:[-*+]\s*)?(?:\*\*)?(?:TODO|TBD|待补充|略|同上)(?:\*\*)?[。；;]?\s*$",
        re.MULTILINE | re.IGNORECASE,
    )

    assert {path.name for path in paths} == expected
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert placeholder_line.search(text) is None
        assert "${" in text
        assert "操作说明" in text


@pytest.mark.parametrize(
    ("filename", "module_id"),
    (
        ("01_阅读一_信息类文本详案.md", "reading_1"),
        ("02_阅读二_文学类文本详案.md", "reading_2"),
        ("03_阅读三_文言文详案.md", "classical_chinese"),
        ("04_阅读四_古诗文详案.md", "poetry"),
        ("05_语言文字运用详案.md", "language_use"),
        ("06_作文审题与写作指导详案.md", "composition"),
    ),
)
def test_each_public_module_template_passes_template_validation(filename, module_id):
    issues = validate_file(Path("templates") / filename, module_id, template=True)

    assert issues == ()
