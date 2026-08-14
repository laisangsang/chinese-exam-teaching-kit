"""Deterministic matching from question tasks to knowledge cards."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .questions import QuestionKnowledge
from .store import KnowledgeCard


@dataclass(frozen=True)
class QuestionMatch:
    question_id: str
    card_id: str
    card_status: str
    score: int
    matched_dimensions: tuple[str, ...]
    applicability: str
    reason: str
    boundary: str


@dataclass(frozen=True)
class KnowledgeSearchWorkOrder:
    schema_version: int
    completed_question_ids: tuple[str, ...]
    matches: tuple[QuestionMatch, ...]


def _tokens(values: Iterable[str]) -> set[str]:
    compact = re.sub(r"\s+", "", " ".join(values)).casefold()
    tokens = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", compact))
    tokens.update(compact[index : index + 4] for index in range(max(0, len(compact) - 3)))
    return {token for token in tokens if token}


def _match(question: QuestionKnowledge, card: KnowledgeCard) -> QuestionMatch:
    metadata = card.metadata
    dimensions: list[str] = []
    same_module = question.module in {
        str(value) for value in metadata.get("modules", [])
    }
    if same_module and question.question_type in {
        str(value) for value in metadata.get("question_types", [])
    }:
        dimensions.append("question_type")
    if same_module and set(question.abilities).intersection(
        str(value) for value in metadata.get("abilities", [])
    ):
        dimensions.append("ability")
    if same_module and _tokens((question.task_statement,)).intersection(
        _tokens((card.sections.get("知识表述", ""),))
    ):
        dimensions.append("actual_task")
    if same_module and _tokens(
        (question.evidence_anchor, *question.retrieval_queries)
    ).intersection(_tokens((card.sections.get("支持证据", ""),))):
        dimensions.append("evidence_structure")
    core_match = bool({"actual_task", "evidence_structure"}.intersection(dimensions))
    paired_labels = {"question_type", "ability"}.issubset(dimensions)
    applicable = same_module and (core_match or paired_labels)
    core_count = len({"actual_task", "evidence_structure"}.intersection(dimensions))
    score = core_count * 10 + len(dimensions)
    if not same_module:
        reason = "板块不一致，记录为不适用；不得迁移具体答案或评分口径。"
    elif applicable:
        reason = (
            "题目与知识卡在"
            + "、".join(dimensions)
            + "维度达到保守适用门槛；仍须核对本题证据与评分边界。"
        )
    elif dimensions:
        reason = "仅有单一表面标签相同，未达到方法适用门槛。"
    else:
        reason = "未发现核心任务、证据结构或成组标签的一致性。"
    return QuestionMatch(
        question_id=question.question_id,
        card_id=card.card_id,
        card_status=card.status,
        score=score,
        matched_dimensions=tuple(dimensions),
        applicability="applicable" if applicable else "not_applicable",
        reason=reason,
        boundary=card.sections.get("禁止越界", "不得迁移具体答案。"),
    )


def match_manifest(
    questions: Iterable[QuestionKnowledge],
    cards: Iterable[KnowledgeCard],
) -> KnowledgeSearchWorkOrder:
    question_items = tuple(questions)
    card_items = tuple(cards)
    matches = tuple(
        sorted(
            (
                _match(question, card)
                for question in question_items
                for card in card_items
            ),
            key=lambda item: (
                item.question_id,
                item.applicability != "applicable",
                -item.score,
                item.card_id,
            ),
        )
    )
    return KnowledgeSearchWorkOrder(
        schema_version=1,
        completed_question_ids=tuple(question.question_id for question in question_items),
        matches=matches,
    )
