"""Public, offline knowledge-card framework for exam analysis."""

from .ingest import CandidateKnowledge, Conflict, StatusDecision, VerificationCase, evaluate_status
from .matching import KnowledgeSearchWorkOrder, QuestionMatch, match_manifest
from .questions import QuestionKnowledge, append_audit_event
from .store import KnowledgeCard, build_index, load_contract, search_cards, validate_library

__all__ = [
    "CandidateKnowledge",
    "Conflict",
    "KnowledgeCard",
    "KnowledgeSearchWorkOrder",
    "QuestionKnowledge",
    "QuestionMatch",
    "StatusDecision",
    "VerificationCase",
    "append_audit_event",
    "build_index",
    "evaluate_status",
    "load_contract",
    "match_manifest",
    "search_cards",
    "validate_library",
]
