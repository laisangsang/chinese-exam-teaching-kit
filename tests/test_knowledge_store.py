import json
from pathlib import Path

import pytest

from chinese_exam_kit.knowledge.store import (
    build_index,
    load_contract,
    parse_card,
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
sources = [
  {{ kind = "original_example", name = "原创微型案例", locator = "examples/original-mini-exam/README.md" }}
]
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


def _verification_tables(*cases: dict[str, object]) -> str:
    tables = []
    for case in cases:
        lines = ["[[verification_cases]]"]
        for key, value in case.items():
            rendered = json.dumps(value, ensure_ascii=False)
            if isinstance(value, bool):
                rendered = rendered.lower()
            lines.append(f"{key} = {rendered}")
        tables.append("\n".join(lines))
    return "\n\n".join(tables)


def _add_verification_cases(card_path: Path, *cases: dict[str, object]) -> None:
    text = card_path.read_text(encoding="utf-8")
    marker = "+++\n\n## 知识表述"
    card_path.write_text(
        text.replace(marker, f"{_verification_tables(*cases)}\n+++\n\n## 知识表述"),
        encoding="utf-8",
    )


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


def test_index_rejects_windows_absolute_card_path_on_any_platform(tmp_path):
    contract = load_contract(Path("config/knowledge_contract.json"))
    _write_card(tmp_path, card_id="MT-0001", title="索引边界", body="只记录相对路径。")
    parsed = validate_library(tmp_path, contract).cards[0]
    escaped = type(parsed)(
        path=Path(r"C:\private\knowledge\MT-0001.md"),
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


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("schema_version = 1", "schema_version = 2", "unsupported_schema_version"),
        ("schema_version = 1", "schema_version = true", "unsupported_schema_version"),
        ('title = "字段合同"', 'title = ""', "invalid_title"),
        (
            'question_types = ["information_extraction"]',
            'question_types = "information_extraction"',
            "invalid_question_types",
        ),
        ('abilities = ["evidence_location"]', 'abilities = [""]', "invalid_abilities"),
    ],
)
def test_card_metadata_contract_rejects_invalid_shapes(tmp_path, old, new, code):
    contract = load_contract(Path("config/knowledge_contract.json"))
    card_path = _write_card(
        tmp_path, card_id="MT-0001", title="字段合同", body="测试字段合同。"
    )
    card_path.write_text(
        card_path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8"
    )

    result = validate_library(tmp_path, contract)

    assert code in {issue.code for issue in result.errors}


@pytest.mark.parametrize("declared_status", ["verified", "stable"])
def test_verified_or_stable_card_requires_independent_exam_evidence(tmp_path, declared_status):
    contract = load_contract(Path("config/knowledge_contract.json"))
    card_path = _write_card(
        tmp_path, card_id="MT-0001", title="状态门槛", body="测试状态门槛。"
    )
    card_path.write_text(
        card_path.read_text(encoding="utf-8").replace(
            'status = "candidate"', f'status = "{declared_status}"'
        ),
        encoding="utf-8",
    )

    result = validate_library(tmp_path, contract)

    assert "unsupported_declared_status" in {issue.code for issue in result.errors}


def test_stable_card_accepts_two_distinct_exam_cases(tmp_path):
    contract = load_contract(Path("config/knowledge_contract.json"))
    card_path = _write_card(
        tmp_path, card_id="MT-0001", title="稳定门槛", body="测试稳定门槛。"
    )
    card_path.write_text(
        card_path.read_text(encoding="utf-8").replace(
            'status = "candidate"', 'status = "stable"'
        ),
        encoding="utf-8",
    )
    _add_verification_cases(
        card_path,
        {
            "exam_id": "original-exam-a",
            "source_id": "original-source-a",
            "exam_year": 2024,
            "evidence_kind": "formal_exam",
            "official_support": False,
        },
        {
            "exam_id": "original-exam-b",
            "source_id": "original-source-b",
            "exam_year": 2025,
            "evidence_kind": "formal_exam",
            "official_support": False,
        },
    )

    result = validate_library(tmp_path, contract)

    assert result.errors == ()


def test_verification_case_ids_must_be_nonempty_strings(tmp_path):
    contract = load_contract(Path("config/knowledge_contract.json"))
    card_path = _write_card(
        tmp_path, card_id="MT-0001", title="验证字段", body="测试验证字段。"
    )
    _add_verification_cases(
        card_path,
        {
            "exam_id": 1,
            "source_id": "original-source-a",
            "exam_year": 2025,
            "evidence_kind": "formal_exam",
            "official_support": False,
        },
    )

    result = validate_library(tmp_path, contract)

    assert "invalid_verification_case" in {issue.code for issue in result.errors}


@pytest.mark.parametrize(
    ("declared_status", "reason_field"),
    [("review_required", "review_reason"), ("deprecated", "deprecation_reason")],
)
def test_terminal_status_requires_an_explicit_reason(tmp_path, declared_status, reason_field):
    contract = load_contract(Path("config/knowledge_contract.json"))
    card_path = _write_card(
        tmp_path, card_id="MT-0001", title="状态原因", body="测试状态原因。"
    )
    card_path.write_text(
        card_path.read_text(encoding="utf-8").replace(
            'status = "candidate"', f'status = "{declared_status}"'
        ),
        encoding="utf-8",
    )

    result = validate_library(tmp_path, contract)

    assert f"missing_{reason_field}" in {issue.code for issue in result.errors}


def test_relative_library_root_indexes_cards_relative_to_the_library(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = Path("knowledge")
    _write_card(root, card_id="MT-0001", title="相对根", body="测试相对根。")
    contract = load_contract(
        Path(__file__).resolve().parents[1] / "config/knowledge_contract.json"
    )

    direct_index = build_index(
        (parse_card(root / "cards" / "methods" / "MT-0001.md"),), contract, root
    )
    assert direct_index["cards"][0]["path"] == "cards/methods/MT-0001.md"

    result = validate_library(root, contract)
    index = build_index(result.cards, contract, root)

    assert result.errors == ()
    assert index["cards"][0]["path"] == "cards/methods/MT-0001.md"
    (root / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    assert validate_library(root, contract).errors == ()


@pytest.mark.parametrize("link_kind", ["file", "directory"])
def test_validate_library_rejects_symlink_escape_without_reading_target(tmp_path, link_kind):
    if not hasattr(Path, "symlink_to"):
        pytest.skip("symlinks are unavailable")
    root = tmp_path / "knowledge"
    outside = tmp_path / "outside"
    outside_card = _write_card(
        outside, card_id="MT-0001", title="外部卡片", body="不能读取。"
    )
    (root / "cards").mkdir(parents=True)
    try:
        if link_kind == "directory":
            (root / "cards" / "methods").symlink_to(outside_card.parent, target_is_directory=True)
        else:
            (root / "cards" / "methods").mkdir()
            (root / "cards" / "methods" / "MT-0001.md").symlink_to(outside_card)
            (root / "index.json").write_text(
                '{"schema_version":1,"generated_at":null,"cards":[]}\n',
                encoding="utf-8",
            )
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    result = validate_library(root, load_contract(Path("config/knowledge_contract.json")))

    assert result.cards == ()
    assert [issue.code for issue in result.errors] == ["path_escape"]
    rendered = json.dumps([issue.to_dict() for issue in result.errors], ensure_ascii=False)
    assert str(outside) not in rendered
