import hashlib
import json
import re
from pathlib import Path

from chinese_exam_kit.content.docx import build_all
from chinese_exam_kit.content.validate import validate_content_dir
from chinese_exam_kit.knowledge.store import build_index, load_contract, validate_library


EXAMPLE_ROOT = Path("examples/original-mini-exam")
CONTENT_NAMES = {
    "00_整卷总览与讲评建议.md",
    "01_阅读一_信息类文本详案.md",
    "02_阅读二_文学类文本详案.md",
    "03_阅读三_文言文详案.md",
    "04_阅读四_古诗文详案.md",
    "05_语言文字运用详案.md",
    "06_作文审题与写作指导详案.md",
}
CARD_PATHS = {
    "cards/overviews/OV-0001.md",
    "cards/question-types/QT-0001.md",
    "cards/methods/MT-0001.md",
}
MANIFEST_LINE = re.compile(r"^- `(?P<digest>[0-9a-f]{64})`  `(?P<path>[^`]+)`$")
MODULE_SOURCE_HEADINGS = {
    "01_阅读一_信息类文本详案.md": "一、阅读一：信息类文本（8分）",
    "02_阅读二_文学类文本详案.md": "二、阅读二：文学类文本（8分）",
    "03_阅读三_文言文详案.md": "三、阅读三：文言文（8分）",
    "04_阅读四_古诗文详案.md": "四、阅读四：古诗文（8分）",
    "05_语言文字运用详案.md": "五、语言文字运用（8分）",
    "06_作文审题与写作指导详案.md": "六、作文（60分）",
}


def _manifest_entries() -> dict[str, str]:
    text = (EXAMPLE_ROOT / "ORIGINALITY.md").read_text(encoding="utf-8")
    return {
        match.group("path"): match.group("digest")
        for line in text.splitlines()
        if (match := MANIFEST_LINE.fullmatch(line))
    }


def _h2_body(text: str, title: str) -> str:
    return _heading_body(text, 2, title)


def _heading_body(text: str, level: int, title: str) -> str:
    marks = "#" * level
    marker = f"{marks} {title}\n"
    start = text.index(marker) + len(marker)
    match = re.search(rf"(?m)^#{{1,{level}}} ", text[start:])
    end = start + match.start() if match else len(text)
    return text[start:end].strip()


def test_original_example_covers_all_modules_and_required_topics():
    content_dir = EXAMPLE_ROOT / "content"

    assert {path.name for path in content_dir.glob("*.md")} == CONTENT_NAMES
    assert validate_content_dir(content_dir) == ()

    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((EXAMPLE_ROOT / "source").glob("*.md"))
    )
    for required in (
        "青禾中学",
        "灯亮之前",
        "周生治圃记",
        "晚课归窗下作",
        "纸笔记录与数字检索",
        "工具保存答案，人仍需形成问题",
    ):
        assert required in combined
    assert "本项目原创示例评分参考" in combined


def test_original_example_builds_seven_docx_files_without_writing_into_example(tmp_path):
    before = {path.relative_to(EXAMPLE_ROOT) for path in EXAMPLE_ROOT.rglob("*") if path.is_file()}

    outputs = build_all(EXAMPLE_ROOT / "content", tmp_path)

    assert len(outputs) == 7
    assert all(path.suffix == ".docx" and path.parent == tmp_path for path in outputs)
    after = {path.relative_to(EXAMPLE_ROOT) for path in EXAMPLE_ROOT.rglob("*") if path.is_file()}
    assert after == before
    assert not any(path.suffix.lower() in {".docx", ".pdf"} for path in EXAMPLE_ROOT.rglob("*"))


def test_each_module_guide_embeds_its_complete_original_question_section():
    source = (EXAMPLE_ROOT / "source" / "原创微型示范卷.md").read_text(encoding="utf-8")

    for filename, source_heading in MODULE_SOURCE_HEADINGS.items():
        guide = (EXAMPLE_ROOT / "content" / filename).read_text(encoding="utf-8")
        source_section = _h2_body(source, source_heading)
        assert "## 本板块完整题面" in guide
        assert source_section in _h2_body(guide, "本板块完整题面")


def test_original_classical_passage_uses_unambiguous_grammar_and_translation():
    source = (EXAMPLE_ROOT / "source" / "原创微型示范卷.md").read_text(encoding="utf-8")
    guide = (EXAMPLE_ROOT / "content" / "03_阅读三_文言文详案.md").read_text(encoding="utf-8")

    for required in ("苗不复败", "豆实虽不及邻圃"):
        assert required in source
        assert required in guide
    for ambiguous in ("败苗者止", "豆实虽未盈邻圃", "名词活用为动词"):
        assert ambiguous not in source
        assert ambiguous not in guide
    assert "语：告诉" in guide


def test_composition_includes_three_additional_complete_and_distinct_structures():
    guide = (EXAMPLE_ROOT / "content" / "06_作文审题与写作指导详案.md").read_text(encoding="utf-8")

    for heading, unit_label in (
        ("方案五：并列式《问题为答案建立坐标》", "分论点"),
        ("方案六：辩证关系式《在答案与问题之间往返》", "分论点"),
        ("方案七：叙事结构《收藏夹的第三行》", "叙事单元"),
    ):
        section = _heading_body(guide, 3, heading)
        assert f"- {unit_label}一：" in section
        assert f"- {unit_label}二：" in section
        assert f"- {unit_label}三：" in section
        assert "- 段落任务：" in section
        assert "- 推进关系：" in section
        assert "- 反面与边界：" in section
        assert "- 结尾落点：" in section


def test_original_example_knowledge_is_candidate_only_and_index_is_reproducible():
    knowledge_root = EXAMPLE_ROOT / "knowledge"
    contract = load_contract(Path("config/knowledge_contract.json"))

    library = validate_library(knowledge_root, contract)

    assert library.errors == ()
    assert {
        path.relative_to(knowledge_root.resolve()).as_posix()
        for path in (card.path for card in library.cards)
    } == CARD_PATHS
    assert {card.status for card in library.cards} == {"candidate"}
    expected_index = build_index(library.cards, contract, knowledge_root)
    assert expected_index == json.loads((knowledge_root / "index.json").read_text(encoding="utf-8"))
    assert json.loads(Path("knowledge/index.json").read_text(encoding="utf-8"))["cards"] == []


def test_originality_manifest_verifies_declared_scope_and_byte_integrity():
    # This checks the project's declared file set and byte integrity; it is not
    # proof of provenance or copyright status outside the repository.
    covered = {
        "README.md",
        "source/原创微型示范卷.md",
        "source/原创答案与评分参考.md",
        *{f"content/{name}" for name in CONTENT_NAMES},
        *{f"knowledge/{path}" for path in CARD_PATHS},
        "knowledge/index.json",
    }

    entries = _manifest_entries()

    assert set(entries) == covered
    for relative, expected_digest in entries.items():
        actual = hashlib.sha256((EXAMPLE_ROOT / relative).read_bytes()).hexdigest()
        assert actual == expected_digest


def test_originality_statement_explicitly_covers_fiction_and_third_party_exclusion():
    statement = (EXAMPLE_ROOT / "ORIGINALITY.md").read_text(encoding="utf-8")

    assert "从零创作" in statement
    assert "人物、学校与数据均为虚构" in statement
    assert "未复制或改写第三方试题、课文、答案" in statement
