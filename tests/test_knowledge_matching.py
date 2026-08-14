import json
import threading
from pathlib import Path

import pytest

from chinese_exam_kit.knowledge.matching import match_manifest
from chinese_exam_kit.knowledge.questions import QuestionKnowledge, append_audit_event
from chinese_exam_kit.knowledge.store import KnowledgeCard


def sample_card(card_id="MT-0001"):
    return KnowledgeCard(
        path=Path("cards/methods") / f"{card_id}.md",
        metadata={
            "id": card_id,
            "title": "证据定位方法",
            "card_type": "method",
            "status": "verified",
            "risk_level": "normal",
            "modules": ["reading_1"],
            "question_types": ["information_extraction"],
            "abilities": ["evidence_location"],
        },
        sections={
            "知识表述": "先定位题干限定，再整合文本证据。",
            "适用条件": "信息筛选题",
            "禁止越界": "不得迁移具体答案。",
            "支持证据": "用于文本证据定位。",
        },
        body="先定位题干限定，再整合文本证据。",
    )


def sample_question(question_id="1"):
    return QuestionKnowledge(
        question_id=question_id,
        module="reading_1",
        question_type="information_extraction",
        abilities=("evidence_location",),
        task_statement="筛选并整合文本证据",
        evidence_anchor="第2段",
        answer_boundary="只概括文本已有信息",
        retrieval_queries=("文本证据定位",),
    )


def test_match_manifest_records_applicability_reason():
    order = match_manifest((sample_question(),), (sample_card(),))

    assert order.matches[0].reason
    assert order.matches[0].question_id == "1"
    assert order.matches[0].card_id == "MT-0001"
    assert order.matches[0].applicability == "applicable"


def test_match_manifest_is_deterministic():
    cards = (sample_card("MT-0002"), sample_card("MT-0001"))

    first = match_manifest((sample_question(),), cards)
    second = match_manifest((sample_question(),), tuple(reversed(cards)))

    assert first == second
    assert [match.card_id for match in first.matches] == ["MT-0001", "MT-0002"]


def test_match_manifest_reuses_a_card_generator_for_every_question():
    questions = (sample_question("1"), sample_question("2"))

    order = match_manifest(questions, (card for card in (sample_card(),)))

    assert [(match.question_id, match.card_id) for match in order.matches] == [
        ("1", "MT-0001"),
        ("2", "MT-0001"),
    ]


def test_non_applicable_card_is_recorded_with_reason_and_boundary():
    card = sample_card()
    card = KnowledgeCard(
        path=card.path,
        metadata={**card.metadata, "modules": ["poetry"]},
        sections=card.sections,
        body=card.body,
    )

    order = match_manifest((sample_question(),), (card,))

    assert len(order.matches) == 1
    assert order.matches[0].applicability == "not_applicable"
    assert order.matches[0].reason
    assert order.matches[0].boundary == "不得迁移具体答案。"


def test_single_question_type_label_is_not_enough_for_applicability():
    card = sample_card()
    card = KnowledgeCard(
        path=card.path,
        metadata={**card.metadata, "abilities": ["unrelated_ability"]},
        sections={
            **card.sections,
            "知识表述": "分析诗歌声音与色彩。",
            "支持证据": "只适用于诗歌画面。",
        },
        body="分析诗歌声音与色彩。",
    )

    order = match_manifest((sample_question(),), (card,))

    assert order.matches[0].matched_dimensions == ("question_type",)
    assert order.matches[0].applicability == "not_applicable"


def test_question_type_and_ability_combination_is_conservatively_applicable():
    card = sample_card()
    card = KnowledgeCard(
        path=card.path,
        metadata=card.metadata,
        sections={
            **card.sections,
            "知识表述": "分析诗歌声音与色彩。",
            "支持证据": "只适用于诗歌画面。",
        },
        body="分析诗歌声音与色彩。",
    )

    order = match_manifest((sample_question(),), (card,))

    assert order.matches[0].matched_dimensions == ("question_type", "ability")
    assert order.matches[0].applicability == "applicable"


def test_generic_two_character_overlap_is_not_a_core_task_match():
    question = sample_question()
    question = QuestionKnowledge(
        question_id=question.question_id,
        module=question.module,
        question_type="different_type",
        abilities=("different_ability",),
        task_statement="分析人物选择",
        evidence_anchor="人物行动",
        answer_boundary=question.answer_boundary,
        retrieval_queries=("人物选择",),
    )
    card = sample_card()
    card = KnowledgeCard(
        path=card.path,
        metadata={
            **card.metadata,
            "question_types": ["other_type"],
            "abilities": ["other_ability"],
        },
        sections={
            **card.sections,
            "知识表述": "分析诗歌声音与色彩。",
            "支持证据": "诗歌画面与听觉。",
        },
        body="分析诗歌声音与色彩。",
    )

    order = match_manifest((question,), (card,))

    assert order.matches[0].matched_dimensions == ()
    assert order.matches[0].applicability == "not_applicable"


def test_match_manifest_records_completion_when_no_cards_exist():
    order = match_manifest((sample_question("1"), sample_question("2")), ())

    assert order.matches == ()
    assert order.completed_question_ids == ("1", "2")


def test_append_audit_event_is_json_safe_and_relative(tmp_path):
    path = append_audit_event(
        tmp_path,
        task_id="original-task",
        stage="pre",
        event="search",
        details={"queries": ["证据定位"], "count": 1},
        timestamp="2026-08-15T00:00:00Z",
    )

    assert path == tmp_path / "audit" / "original-task.jsonl"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "applicability": None,
        "card_id": None,
        "details": {"count": 1, "queries": ["证据定位"]},
        "event": "search",
        "reason": None,
        "stage": "pre",
        "task_id": "original-task",
        "timestamp": "2026-08-15T00:00:00Z",
    }


def test_audit_use_records_explicit_applicability(tmp_path):
    path = append_audit_event(
        tmp_path,
        task_id="original-task",
        stage="during",
        event="use",
        card_id="MT-0001",
        applicability="applicable",
        reason="实际任务和证据组织相符",
        timestamp="2026-08-15T00:00:00Z",
    )

    assert json.loads(path.read_text(encoding="utf-8"))["applicability"] == "applicable"


@pytest.mark.parametrize(
    "secret",
    ["/Users/example/private.pdf", r"C:\\Users\\example\\private.pdf", "file:///tmp/private"],
)
def test_append_audit_event_rejects_absolute_path_leaks(tmp_path, secret):
    with pytest.raises(ValueError, match="absolute path"):
        append_audit_event(
            tmp_path,
            task_id="original-task",
            stage="during",
            event="use",
            details={"source": secret},
        )

    assert not (tmp_path / "audit" / "original-task.jsonl").exists()


@pytest.mark.parametrize(
    "secret",
    [
        "前缀 /Users/example/My Private/source.pdf 后缀",
        r"前缀 C:\Users\example\My Files\source.pdf 后缀",
        r"前缀 \\server\share\My Files\source.pdf 后缀",
        "前缀 file:///tmp/My Private/source.pdf 后缀",
        "前缀 file:/tmp/My Private/source.pdf 后缀",
    ],
)
@pytest.mark.parametrize("location", ["key", "value"])
def test_audit_rejects_embedded_absolute_paths_in_keys_and_values(
    tmp_path, secret, location
):
    details = {secret: "safe"} if location == "key" else {"source": secret}

    with pytest.raises(ValueError, match="absolute path") as captured:
        append_audit_event(
            tmp_path,
            task_id="original-task",
            stage="pre",
            event="search",
            details=details,
        )

    assert secret not in str(captured.value)
    assert not (tmp_path / "audit" / "original-task.jsonl").exists()


def test_audit_rejects_single_segment_absolute_posix_path(tmp_path):
    with pytest.raises(ValueError, match="absolute path"):
        append_audit_event(
            tmp_path,
            task_id="original-task",
            stage="pre",
            event="search",
            details={"source": "/tmp"},
        )


def test_append_audit_event_rejects_non_json_values_without_touching_file(tmp_path):
    with pytest.raises(ValueError, match="JSON-safe"):
        append_audit_event(
            tmp_path,
            task_id="original-task",
            stage="pre",
            event="search",
            details={"source": Path("relative-but-not-json-safe")},
        )

    assert not (tmp_path / "audit" / "original-task.jsonl").exists()


def test_append_audit_event_requires_details_object(tmp_path):
    with pytest.raises(ValueError, match="details must be an object"):
        append_audit_event(
            tmp_path,
            task_id="original-task",
            stage="pre",
            event="search",
            details=["not", "an", "object"],
        )


def test_concurrent_audit_appends_keep_every_complete_record(tmp_path):
    barrier = threading.Barrier(8)

    def append(index):
        barrier.wait()
        append_audit_event(
            tmp_path,
            task_id="original-task",
            stage="during",
            event="use",
            card_id=f"MT-{index:04d}",
            reason="适用条件已核对",
            timestamp=f"2026-08-15T00:00:{index:02d}Z",
        )

    threads = [threading.Thread(target=append, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = (tmp_path / "audit" / "original-task.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    records = [json.loads(line) for line in lines]
    assert len(records) == 8
    assert {record["card_id"] for record in records} == {
        f"MT-{index:04d}" for index in range(8)
    }


def test_failed_audit_publish_preserves_previous_complete_file(tmp_path, monkeypatch):
    import chinese_exam_kit.knowledge.questions as questions_module

    path = append_audit_event(
        tmp_path,
        task_id="original-task",
        stage="pre",
        event="search",
        timestamp="2026-08-15T00:00:00Z",
    )
    original = path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("injected publish failure")

    monkeypatch.setattr(questions_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected publish failure"):
        append_audit_event(
            tmp_path,
            task_id="original-task",
            stage="post",
            event="review",
            timestamp="2026-08-15T00:00:01Z",
        )

    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.tmp"))
