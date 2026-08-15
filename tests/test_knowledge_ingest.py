import pytest

from chinese_exam_kit.knowledge.ingest import (
    CandidateKnowledge,
    Conflict,
    VerificationCase,
    evaluate_status,
)
from tests._host_samples import posix_path


def one_case(**overrides):
    values = {
        "exam_id": "original-exam-a",
        "source_id": "original-source-a",
        "exam_year": 2025,
        "evidence_kind": "formal_exam",
        "official_support": False,
    }
    values.update(overrides)
    return VerificationCase(**values)


def test_candidate_knowledge_is_json_safe():
    candidate = CandidateKnowledge(
        card_type="method",
        title="证据定位",
        statement="先定位，再整合。",
        modules=("reading_1",),
        source={
            "id": "original-example",
            "kind": "original_example",
            "name": "原创微型案例",
            "locator": "examples/original-mini-exam",
        },
    )

    assert candidate.to_dict() == {
        "card_type": "method",
        "title": "证据定位",
        "statement": "先定位，再整合。",
        "modules": ["reading_1"],
        "source": {
            "id": "original-example",
            "kind": "original_example",
            "name": "原创微型案例",
            "locator": "examples/original-mini-exam",
        },
        "risk_level": "normal",
    }


def test_candidate_knowledge_rejects_absolute_source_locator():
    with pytest.raises(ValueError, match="absolute path"):
        CandidateKnowledge(
            card_type="method",
            title="路径边界",
            statement="不记录本机路径。",
            modules=("reading_1",),
            source={
                "id": "original-exam",
                "kind": "formal_exam",
                "name": "原创试卷",
                "locator": posix_path("private", "exam.pdf"),
            },
        )


def test_candidate_knowledge_rejects_source_parent_traversal():
    with pytest.raises(ValueError, match="escape"):
        CandidateKnowledge(
            card_type="method",
            title="路径边界",
            statement="不记录目录逃逸路径。",
            modules=("reading_1",),
            source={
                "id": "original-exam",
                "kind": "formal_exam",
                "name": "原创试卷",
                "locator": "../../private/exam.pdf",
            },
        )


def test_candidate_knowledge_requires_traceable_source_fields():
    with pytest.raises(ValueError, match="source name"):
        CandidateKnowledge(
            card_type="method",
            title="来源边界",
            statement="候选知识必须可追溯。",
            modules=("reading_1",),
            source={
                "id": "original-exam",
                "kind": "formal_exam",
                "locator": "examples/original-exam.md",
            },
        )


def test_one_exam_candidate_cannot_become_stable():
    decision = evaluate_status("method", verification_cases=(one_case(),))

    assert decision.status == "verified"


def test_normal_method_needs_two_distinct_exams_to_be_stable():
    decision = evaluate_status(
        "method",
        verification_cases=(
            one_case(),
            one_case(exam_id="original-exam-b", source_id="original-source-b"),
        ),
    )

    assert decision.status == "stable"


def test_duplicate_exam_does_not_count_twice():
    decision = evaluate_status(
        "method",
        verification_cases=(one_case(), one_case(evidence_kind="formal_answer")),
    )

    assert decision.status == "verified"


def test_high_risk_method_needs_three_diverse_exams():
    cases = (
        one_case(exam_id="a", source_id="same", exam_year=2025),
        one_case(exam_id="b", source_id="same", exam_year=2025),
        one_case(exam_id="c", source_id="same", exam_year=2025),
    )

    decision = evaluate_status("method", risk_level="high", verification_cases=cases)

    assert decision.status == "verified"
    assert "不同来源或不同年份" in decision.reason


def test_high_risk_method_prefers_official_answer_support():
    cases = (
        one_case(exam_id="a", source_id="source-a", exam_year=2023),
        one_case(exam_id="b", source_id="source-b", exam_year=2024),
        one_case(
            exam_id="c",
            source_id="source-c",
            exam_year=2025,
            evidence_kind="formal_answer",
            official_support=True,
        ),
    )

    decision = evaluate_status("method", risk_level="high", verification_cases=cases)

    assert decision.status == "stable"
    assert decision.warnings == ()


def test_conflict_or_counterexample_requires_review():
    conflict = Conflict("新案例与当前规则冲突")

    decision = evaluate_status(
        "method", verification_cases=(one_case(),), conflict=conflict
    )

    assert decision.status == "review_required"
    assert decision.reason == "新案例与当前规则冲突"


def test_text_inference_does_not_verify_a_candidate():
    decision = evaluate_status(
        "method",
        verification_cases=(one_case(evidence_kind="text_inference"),),
    )

    assert decision.status == "candidate"


def test_official_support_requires_formal_answer_evidence():
    with pytest.raises(ValueError, match="formal_answer"):
        VerificationCase(
            exam_id="original-exam",
            source_id="original-source",
            exam_year=2025,
            evidence_kind="formal_exam",
            official_support=True,
        )


@pytest.mark.parametrize(
    "cases",
    [(), (one_case(evidence_kind="text_inference"),)],
)
def test_review_resolution_without_independent_evidence_stays_in_review(cases):
    decision = evaluate_status(
        "method",
        current_status="review_required",
        resolve_review=True,
        verification_cases=cases,
    )

    assert decision.status == "review_required"


def test_review_resolution_with_independent_evidence_can_restore_verified():
    decision = evaluate_status(
        "method",
        current_status="review_required",
        resolve_review=True,
        verification_cases=(one_case(),),
    )

    assert decision.status == "verified"


def test_verification_exam_ids_are_trimmed_before_deduplication():
    decision = evaluate_status(
        "method",
        verification_cases=(
            one_case(exam_id="original-exam-a", source_id="source-a"),
            one_case(exam_id=" original-exam-a ", source_id="source-b"),
        ),
    )

    assert decision.status == "verified"


@pytest.mark.parametrize("field", ["exam_id", "source_id"])
def test_verification_ids_reject_control_characters(field):
    values = {
        "exam_id": "original-exam-a",
        "source_id": "original-source-a",
    }
    values[field] = values[field] + "\nprivate"

    with pytest.raises(ValueError, match="control"):
        VerificationCase(**values)
