import json
from pathlib import Path

import pytest

from chinese_exam_kit.knowledge.store import (
    build_index,
    load_contract,
    search_cards,
    validate_library,
)


def _write_card(root: Path, *, card_id: str, title: str, body: str) -> Path:
    path = root / "cards" / "methods" / f"{card_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'''+++
schema_version = 1
id = "{card_id}"
title = "{title}"
card_type = "method"
status = "candidate"
risk_level = "normal"
modules = ["reading_1"]
question_types = ["information_extraction"]
abilities = ["evidence_location"]
sources = [{{ kind = "original_example", name = "原创微型案例", locator = "examples/original-mini-exam/README.md" }}]
+++

## 知识表述
{body}

## 适用条件
题干要求筛选并整合文本信息。

## 禁止越界
不得迁移具体答案。

## 支持证据
当前仅作为候选方法。

## 反例与冲突
出现反例时转入复审。
''',
        encoding="utf-8",
    )
    return path


def test_empty_public_library_is_valid(tmp_path):
    contract = load_contract(Path("config/knowledge_contract.json"))

    result = validate_library(tmp_path, contract)

    assert result.errors == ()
    assert result.cards == ()


def test_empty_index_has_no_fake_timestamp_or_cards(tmp_path):
    contract = load_contract(Path("config/knowledge_contract.json"))

    assert build_index((), contract, tmp_path) == {
        "schema_version": 1,
        "generated_at": None,
        "cards": [],
    }


def test_index_rejects_card_path_outside_library(tmp_path):
    contract = load_contract(Path("config/knowledge_contract.json"))
    _write_card(tmp_path, card_id="MT-0001", title="索引边界", body="只记录相对路径。")
    parsed = validate_library(tmp_path, contract).cards[0]
    escaped = type(parsed)(
        path=Path("/private/knowledge/MT-0001.md"),
        metadata=parsed.metadata,
        sections=parsed.sections,
        body=parsed.body,
    )

    with pytest.raises(ValueError, match="inside the knowledge library"):
        build_index((escaped,), contract, tmp_path)


def test_search_is_deterministic_and_uses_public_card_content(tmp_path):
    contract = load_contract(Path("config/knowledge_contract.json"))
    _write_card(tmp_path, card_id="MT-0002", title="次要方法", body="定位证据后再整合。")
    _write_card(tmp_path, card_id="MT-0001", title="证据定位方法", body="先定位证据，再核对范围。")
    result = validate_library(tmp_path, contract)
    assert result.errors == ()

    first = search_cards(tmp_path, "证据")
    second = search_cards(tmp_path, "证据")

    assert [item.card.card_id for item in first] == ["MT-0001", "MT-0002"]
    assert first == second


def test_absolute_source_locator_is_rejected_without_echoing_it(tmp_path):
    contract = load_contract(Path("config/knowledge_contract.json"))
    _write_card(tmp_path, card_id="MT-0001", title="路径边界", body="测试路径边界。")
    card_path = tmp_path / "cards" / "methods" / "MT-0001.md"
    secret = "/Users/example/private/source.pdf"
    card_path.write_text(
        card_path.read_text(encoding="utf-8").replace(
            "examples/original-mini-exam/README.md", secret
        ),
        encoding="utf-8",
    )

    result = validate_library(tmp_path, contract)

    assert [issue.code for issue in result.errors] == ["absolute_path"]
    assert secret not in json.dumps(
        [issue.to_dict() for issue in result.errors], ensure_ascii=False
    )


def test_missing_card_id_is_reported_instead_of_crashing(tmp_path):
    contract = load_contract(Path("config/knowledge_contract.json"))
    card_path = _write_card(
        tmp_path, card_id="MT-0001", title="不完整卡片", body="测试缺失字段。"
    )
    card_path.write_text(
        card_path.read_text(encoding="utf-8").replace('id = "MT-0001"\n', ""),
        encoding="utf-8",
    )

    result = validate_library(tmp_path, contract)

    assert "missing_metadata" in {issue.code for issue in result.errors}


def test_source_locator_cannot_escape_with_parent_segments(tmp_path):
    contract = load_contract(Path("config/knowledge_contract.json"))
    card_path = _write_card(
        tmp_path, card_id="MT-0001", title="相对路径边界", body="测试目录逃逸。"
    )
    card_path.write_text(
        card_path.read_text(encoding="utf-8").replace(
            "examples/original-mini-exam/README.md", "../../private/source.md"
        ),
        encoding="utf-8",
    )

    result = validate_library(tmp_path, contract)

    assert "path_escape" in {issue.code for issue in result.errors}


def test_source_requires_a_traceable_name(tmp_path):
    contract = load_contract(Path("config/knowledge_contract.json"))
    card_path = _write_card(
        tmp_path, card_id="MT-0001", title="来源字段", body="测试来源字段。"
    )
    card_path.write_text(
        card_path.read_text(encoding="utf-8").replace('name = "原创微型案例", ', ""),
        encoding="utf-8",
    )

    result = validate_library(tmp_path, contract)

    assert "invalid_source" in {issue.code for issue in result.errors}
