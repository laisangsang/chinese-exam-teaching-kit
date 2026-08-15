"""Content contracts and teaching-guide validation."""

from .validate import (
    ValidationIssue,
    format_issues_json,
    format_issues_text,
    validate_content_dir,
    validate_file,
)

__all__ = [
    "ValidationIssue",
    "format_issues_json",
    "format_issues_text",
    "validate_content_dir",
    "validate_file",
]
