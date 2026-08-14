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
    reason: str
    boundary: str


@dataclass(frozen=True)
class KnowledgeSearchWorkOrder:
    schema_version: int
    matches: tuple[QuestionMatch, ...]


def _tokens(values: Iterable[str]) -> set[str]:
    compact = re.sub(r"\s+", "", " ".join(values)).casefold()
    tokens = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", compact))
    tokens.update(compact[index : index + 2] for index in range(max(0, len(compact) - 1)))
    return {token for token in tokens if token}


def _match(question: QuestionKnowledge, card: KnowledgeCard) -> QuestionMatch | None:
    metadata = card.metadata
    if question.module not in {str(value) for value in metadata.get("modules", [])}:
        return None
    dimensions: list[str] = []
    if question.question_type in {str(value) for value in metadata.get("question_types", [])}:
        dimensions.append("question_type")
    if set(question.abilities).intersection(str(value) for value in metadata.get("abilities", [])):
        dimensions.append("ability")
    question_tokens = _tokens((question.task_statement, *question.retrieval_queries))
    card_tokens = _tokens((card.sections.get("知识表述", ""), card.sections.get("支持证据", "")))
    if question_tokens.intersection(card_tokens):
        dimensions.append("evidence_structure")
    if not dimensions:
        return None
    score = 10 * ("evidence_structure" in dimensions) + len(dimensions)
    reason = "题目与知识卡在" + "、".join(dimensions) + "维度相符；仍须核对本题证据与评分边界。"
    return QuestionMatch(
        question_id=question.question_id,
        card_id=card.card_id,
        card_status=card.status,
        score=score,
        matched_dimensions=tuple(dimensions),
        reason=reason,
        boundary=card.sections.get("禁止越界", "不得迁移具体答案。"),
    )


def match_manifest(
    questions: Iterable[QuestionKnowledge],
    cards: Iterable[KnowledgeCard],
) -> KnowledgeSearchWorkOrder:
    card_items = tuple(cards)
    matches = tuple(
        sorted(
            (
                match
                for question in questions
                for card in card_items
                if (match := _match(question, card)) is not None
            ),
            key=lambda item: (item.question_id, -item.score, item.card_id),
        )
    )
    return KnowledgeSearchWorkOrder(schema_version=1, matches=matches)
