"""Resumable, privacy-safe local processing primitives."""

from .intake import archive_inputs, classify_material
from .models import MaterialRecord, PipelineTask, StageRecord
from .answers import AnswerAttachment, attach_reference_answers
from .runner import PipelineRunner, PipelineSummary, StageSummary
from .state import load_task, save_task, transition_stage

__all__ = [
    "MaterialRecord",
    "AnswerAttachment",
    "PipelineTask",
    "PipelineRunner",
    "PipelineSummary",
    "StageRecord",
    "StageSummary",
    "attach_reference_answers",
    "archive_inputs",
    "classify_material",
    "load_task",
    "save_task",
    "transition_stage",
]
