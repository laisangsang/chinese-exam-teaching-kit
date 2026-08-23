"""Deterministic local storage and search for public knowledge cards."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping

from .ingest import VerificationCase, evaluate_status, normalize_identifier


@dataclass(frozen=True)
class KnowledgeCard:
    path: Path
    metadata: Mapping[str, Any]
    sections: Mapping[str, str]
    body: str

    @property
    def card_id(self) -> str:
        return str(self.metadata["id"])

    @property
    def card_type(self) -> str:
        return str(self.metadata["card_type"])

    @property
    def status(self) -> str:
        return str(self.metadata["status"])


@dataclass(frozen=True)
class Issue:
    level: str
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True)
class LibraryValidation:
    cards: tuple[KnowledgeCard, ...]
    issues: tuple[Issue, ...]

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.level == "error")

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.level == "warning")


@dataclass(frozen=True)
class SearchResult:
    card: KnowledgeCard
    score: int
    matched_fields: tuple[str, ...]


@dataclass(frozen=True)
class IndexBuildFailure:
    errors: tuple[Issue, ...]
    index: None = None


def load_contract(path: Path | None = None) -> dict[str, Any]:
    """Load an explicit knowledge contract or the packaged public default."""
    text = (
        Path(path).read_text(encoding="utf-8")
        if path is not None
        else files("chinese_exam_kit.resources")
        .joinpath("knowledge_contract.json")
        .read_text(encoding="utf-8")
    )
    payload = json.loads(text)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("knowledge contract schema_version must be 1")
    return payload


def _split_front_matter(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "+++":
        raise ValueError("knowledge card must start with TOML front matter")
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "+++":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :]).strip()
    raise ValueError("knowledge card has no closing TOML delimiter")


def _sections(body: str) -> dict[str, str]:
    matches = list(re.finditer(r"(?m)^## ([^\n]+?)\s*$", body))
    return {
        match.group(1).strip(): body[
            match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(body)
        ].strip()
        for index, match in enumerate(matches)
    }


def parse_card(path: Path) -> KnowledgeCard:
    metadata_text, body = _split_front_matter(path.read_text(encoding="utf-8"))
    metadata = tomllib.loads(metadata_text)
    return KnowledgeCard(path=path, metadata=metadata, sections=_sections(body), body=body)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _scan_card_tree_for_symlinks(cards_root: Path, root: Path) -> list[Issue]:
    """Reject every symlink below cards without following its target."""
    issues: list[Issue] = []
    if cards_root.is_symlink():
        return [
            Issue(
                "error",
                "symlink_not_allowed",
                "cards",
                "symbolic links are not allowed in the card tree",
            )
        ]
    if not cards_root.exists():
        return issues

    pending = [cards_root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            issues.append(
                Issue(
                    "error",
                    "unreadable_path",
                    _display_path(directory, root),
                    "card directory cannot be read",
                )
            )
            continue
        for entry in entries:
            if entry.is_symlink():
                issues.append(
                    Issue(
                        "error",
                        "symlink_not_allowed",
                        _display_path(entry, root),
                        "symbolic links are not allowed in the card tree",
                    )
                )
                continue
            try:
                if entry.is_dir():
                    pending.append(entry)
            except OSError:
                issues.append(
                    Issue(
                        "error",
                        "unreadable_path",
                        _display_path(entry, root),
                        "card path cannot be inspected",
                    )
                )
    return issues


def _safe_card_paths(root: Path) -> tuple[list[Path], list[Issue]]:
    """Discover cards only after proving the card tree contains no symlinks."""
    root = root.resolve()
    cards_root = root / "cards"
    issues = _scan_card_tree_for_symlinks(cards_root, root)
    paths: list[Path] = []
    if issues or not cards_root.exists():
        return paths, issues
    try:
        resolved_cards = cards_root.resolve(strict=True)
    except (OSError, RuntimeError):
        issues.append(
            Issue(
                "error",
                "unreadable_path",
                "cards",
                "card directory cannot be resolved",
            )
        )
        return paths, issues
    if not _inside(resolved_cards, root):
        issues.append(Issue("error", "path_escape", "cards", "card path escapes the library"))
        return paths, issues
    try:
        directories = sorted(cards_root.iterdir(), key=lambda item: item.name)
    except OSError:
        issues.append(Issue("error", "unreadable_path", "cards", "card directory cannot be read"))
        return paths, issues
    for directory in directories:
        if directory.is_symlink():
            issues.append(
                Issue(
                    "error",
                    "symlink_not_allowed",
                    _display_path(directory, root),
                    "symbolic links are not allowed in the card tree",
                )
            )
            continue
        try:
            resolved_directory = directory.resolve(strict=True)
        except (OSError, RuntimeError):
            issues.append(
                Issue(
                    "error",
                    "unreadable_path",
                    _display_path(directory, root),
                    "card directory cannot be resolved",
                )
            )
            continue
        if not _inside(resolved_directory, resolved_cards):
            issues.append(
                Issue(
                    "error",
                    "path_escape",
                    _display_path(directory, root),
                    "card path escapes the library",
                )
            )
            continue
        if not directory.is_dir():
            continue
        try:
            candidates = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            issues.append(
                Issue(
                    "error",
                    "unreadable_path",
                    _display_path(directory, root),
                    "card directory cannot be read",
                )
            )
            continue
        for path in candidates:
            if path.is_symlink():
                issues.append(
                    Issue(
                        "error",
                        "symlink_not_allowed",
                        _display_path(path, root),
                        "symbolic links are not allowed in the card tree",
                    )
                )
                continue
            if path.suffix != ".md":
                continue
            try:
                resolved_path = path.resolve(strict=True)
            except (OSError, RuntimeError):
                issues.append(
                    Issue(
                        "error",
                        "unreadable_path",
                        _display_path(path, root),
                        "card file cannot be resolved",
                    )
                )
                continue
            if not _inside(resolved_path, resolved_cards):
                issues.append(
                    Issue(
                        "error",
                        "path_escape",
                        _display_path(path, root),
                        "card path escapes the library",
                    )
                )
                continue
            if path.is_file():
                paths.append(path)
    return paths, issues


def discover_cards(root: Path) -> tuple[KnowledgeCard, ...]:
    paths, issues = _safe_card_paths(Path(root))
    if issues:
        raise ValueError("knowledge library contains unsafe or unreadable card paths")
    return tuple(parse_card(path) for path in paths)


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.absolute().relative_to(root.absolute()).as_posix()
    except ValueError:
        return path.name


def _is_absolute_locator(value: str) -> bool:
    text = value.strip()
    return (
        text.startswith("file:")
        or PurePosixPath(text).is_absolute()
        or PureWindowsPath(text).is_absolute()
        or text.startswith("\\\\")
    )


def _locator_escapes(value: str) -> bool:
    location = value.split("#", 1)[0].replace("\\", "/")
    return ".." in PurePosixPath(location).parts


def _issue(
    card: KnowledgeCard,
    root: Path,
    code: str,
    message: str,
    level: str = "error",
) -> Issue:
    return Issue(level, code, _display_path(card.path, root), message)


def _nonempty_string_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def _verification_cases(
    card: KnowledgeCard,
    root: Path,
) -> tuple[tuple[VerificationCase, ...], list[Issue]]:
    raw_cases = card.metadata.get("verification_cases", [])
    if not isinstance(raw_cases, list):
        return (), [
            _issue(
                card,
                root,
                "invalid_verification_cases",
                "verification_cases must be an array of tables",
            )
        ]
    cases: list[VerificationCase] = []
    issues: list[Issue] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            issues.append(
                _issue(
                    card,
                    root,
                    "invalid_verification_case",
                    "verification case must be an object",
                )
            )
            continue
        try:
            exam_id = raw_case["exam_id"]
            source_id = raw_case["source_id"]
            if (
                not isinstance(exam_id, str)
                or not exam_id.strip()
                or not isinstance(source_id, str)
                or not source_id.strip()
            ):
                raise ValueError("verification ids must be nonempty strings")
            exam_year = raw_case.get("exam_year")
            if exam_year is not None and not isinstance(exam_year, int):
                raise ValueError("exam_year must be an integer")
            official_support = raw_case.get("official_support", False)
            conflict = raw_case.get("conflict", False)
            if not isinstance(official_support, bool) or not isinstance(conflict, bool):
                raise ValueError("verification flags must be booleans")
            cases.append(
                VerificationCase(
                    exam_id=exam_id,
                    source_id=source_id,
                    exam_year=exam_year,
                    evidence_kind=str(raw_case.get("evidence_kind", "formal_exam")),
                    official_support=official_support,
                    conflict=conflict,
                )
            )
        except (KeyError, TypeError, ValueError):
            issues.append(
                _issue(
                    card,
                    root,
                    "invalid_verification_case",
                    "verification case does not satisfy the public contract",
                )
            )
    return tuple(cases), issues


def _validate_card(
    card: KnowledgeCard,
    root: Path,
    contract: Mapping[str, Any],
) -> list[Issue]:
    issues: list[Issue] = []
    metadata = card.metadata
    required = contract["required_metadata"]
    for field in required:
        if field not in metadata:
            issues.append(
                _issue(
                    card,
                    root,
                    "missing_metadata",
                    f"missing metadata field: {field}",
                )
            )

    schema_version = metadata.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != contract["schema_version"]
    ):
        issues.append(
            _issue(
                card,
                root,
                "unsupported_schema_version",
                "card schema_version is unsupported",
            )
        )
    if not isinstance(metadata.get("title"), str) or not str(
        metadata.get("title", "")
    ).strip():
        issues.append(_issue(card, root, "invalid_title", "card title must be nonempty"))

    card_type = str(metadata.get("card_type", ""))
    type_contract = contract["card_types"].get(card_type)
    if type_contract is None:
        issues.append(_issue(card, root, "invalid_card_type", "unknown card type"))
    elif not re.fullmatch(
        rf"{re.escape(type_contract['prefix'])}-\d{{4}}",
        str(metadata.get("id", "")),
    ):
        issues.append(_issue(card, root, "invalid_id", "card id does not match its type"))

    if metadata.get("status") not in contract["statuses"]:
        issues.append(_issue(card, root, "invalid_status", "unknown knowledge status"))
    if metadata.get("risk_level") not in contract["risk_levels"]:
        issues.append(_issue(card, root, "invalid_risk_level", "unknown risk level"))
    modules = metadata.get("modules", [])
    if not _nonempty_string_list(modules) or any(
        module not in contract["modules"] for module in modules if isinstance(module, str)
    ):
        issues.append(
            _issue(
                card,
                root,
                "invalid_module",
                "modules contain unknown or empty values",
            )
        )
    for field in ("question_types", "abilities"):
        if not _nonempty_string_list(metadata.get(field)):
            issues.append(
                _issue(
                    card,
                    root,
                    f"invalid_{field}",
                    f"{field} must be a nonempty string array",
                )
            )

    for heading in contract["required_sections"]:
        if not card.sections.get(heading, "").strip():
            issues.append(
                _issue(
                    card,
                    root,
                    "missing_section",
                    f"missing or empty section: {heading}",
                )
            )

    sources = metadata.get("sources", [])
    source_by_id: dict[str, Mapping[str, Any]] = {}
    if not isinstance(sources, list) or not sources:
        issues.append(_issue(card, root, "missing_source", "at least one source is required"))
    else:
        for source in sources:
            if not isinstance(source, dict):
                issues.append(_issue(card, root, "invalid_source", "source must be an object"))
                continue
            source_id = source.get("id")
            try:
                normalized_source_id = normalize_identifier(
                    source_id, field_name="source id"
                )
            except ValueError:
                normalized_source_id = ""
                issues.append(
                    _issue(
                        card,
                        root,
                        "invalid_source_id",
                        "source id must be a nonempty canonical identifier",
                    )
                )
            else:
                if normalized_source_id != source_id:
                    issues.append(
                        _issue(
                            card,
                            root,
                            "noncanonical_source_id",
                            "source id must not contain surrounding whitespace",
                        )
                    )
                elif normalized_source_id in source_by_id:
                    issues.append(
                        _issue(
                            card,
                            root,
                            "duplicate_source_id",
                            "source ids must be unique within a card",
                        )
                    )
                else:
                    source_by_id[normalized_source_id] = source
            if any(
                not isinstance(source.get(field), str) or not source[field].strip()
                for field in contract.get(
                    "required_source_fields", ("id", "kind", "name", "locator")
                )
            ):
                issues.append(
                    _issue(
                        card,
                        root,
                        "invalid_source",
                        "source fields must be nonempty",
                    )
                )
            if source.get("kind") not in contract["source_kinds"]:
                issues.append(_issue(card, root, "invalid_source", "unknown source kind"))
            locator = source.get("locator")
            if not isinstance(locator, str) or not locator.strip():
                issues.append(_issue(card, root, "invalid_source", "source locator is required"))
            elif _is_absolute_locator(locator):
                issues.append(
                    _issue(
                        card,
                        root,
                        "absolute_path",
                        "source locator must be project-relative",
                    )
                )
            elif _locator_escapes(locator):
                issues.append(
                    _issue(
                        card,
                        root,
                        "path_escape",
                        "source locator cannot escape the project",
                    )
                )

    cases, case_issues = _verification_cases(card, root)
    issues.extend(case_issues)
    bound_cases: list[VerificationCase] = []
    allowed_source_kinds = contract.get(
        "verification_source_kind_map",
        {
            "formal_exam": ["formal_exam"],
            "formal_answer": ["formal_answer"],
            "text_inference": ["text_inference", "original_example"],
        },
    )
    for case in cases:
        source = source_by_id.get(case.source_id)
        if source is None:
            issues.append(
                _issue(
                    card,
                    root,
                    "unknown_verification_source",
                    "verification source_id is not declared by the card",
                )
            )
            continue
        allowed = allowed_source_kinds.get(case.evidence_kind, [])
        if source.get("kind") not in allowed:
            issues.append(
                _issue(
                    card,
                    root,
                    "verification_source_kind_mismatch",
                    "verification evidence kind does not match its bound source",
                )
            )
            continue
        bound_cases.append(case)
    status = metadata.get("status")
    if (
        status in {"verified", "stable"}
        and not case_issues
        and metadata.get("risk_level") in contract["risk_levels"]
    ):
        decision = evaluate_status(
            card_type or "unknown",
            verification_cases=tuple(bound_cases),
            risk_level=str(metadata.get("risk_level", "normal")),
        )
        supported = decision.status in (
            {"verified", "stable"} if status == "verified" else {"stable"}
        )
        if not supported:
            issues.append(
                _issue(
                    card,
                    root,
                    "unsupported_declared_status",
                    "declared status exceeds independent verification evidence",
                )
            )
    status_fields = contract.get(
        "status_required_fields",
        {"review_required": "review_reason", "deprecated": "deprecation_reason"},
    )
    review_field = str(status_fields.get("review_required", "review_reason"))
    if status == "review_required":
        has_conflict = any(case.conflict for case in bound_cases)
        if not has_conflict and not str(metadata.get(review_field, "")).strip():
            issues.append(
                _issue(
                    card,
                    root,
                    f"missing_{review_field}",
                    "review_required status needs a conflict or review reason",
                )
            )
    deprecated_field = str(
        status_fields.get("deprecated", "deprecation_reason")
    )
    if status == "deprecated" and not str(metadata.get(deprecated_field, "")).strip():
        issues.append(
            _issue(
                card,
                root,
                f"missing_{deprecated_field}",
                "deprecated status needs a deprecation reason",
            )
        )
    return issues


def validate_library(root: Path, contract: Mapping[str, Any]) -> LibraryValidation:
    root = Path(root).resolve()
    paths, path_issues = _safe_card_paths(root)
    issues: list[Issue] = list(path_issues)
    cards: list[KnowledgeCard] = []
    for path in paths:
        try:
            cards.append(parse_card(path))
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            issues.append(
                Issue(
                    "error",
                    "card_parse_error",
                    _display_path(path, root),
                    "card cannot be parsed",
                )
            )

    for card in cards:
        issues.extend(_validate_card(card, root, contract))

    ids: dict[str, int] = {}
    for card in cards:
        card_id = card.metadata.get("id")
        if isinstance(card_id, str) and card_id:
            ids[card_id] = ids.get(card_id, 0) + 1
    for card_id, count in sorted(ids.items()):
        if count > 1:
            issues.append(
                Issue(
                    "error",
                    "duplicate_id",
                    "cards",
                    f"duplicate card id: {card_id}",
                )
            )
    index_path = root / "index.json"
    if index_path.is_file():
        try:
            saved = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            issues.append(
                Issue(
                    "error",
                    "invalid_index",
                    "index.json",
                    "index is not valid JSON",
                )
            )
        else:
            has_card_errors = any(issue.level == "error" for issue in issues)
            if not has_card_errors:
                expected_index = build_index(tuple(cards), contract, root)
                if isinstance(expected_index, IndexBuildFailure):
                    issues.extend(expected_index.errors)
                elif saved != expected_index:
                    issues.append(
                        Issue(
                            "error",
                            "index_out_of_sync",
                            "index.json",
                            "index must be rebuilt",
                        )
                    )
    return LibraryValidation(tuple(cards), tuple(issues))


def build_index(
    cards: Iterable[KnowledgeCard] | Path,
    contract: Mapping[str, Any],
    root: Path | None = None,
) -> dict[str, Any] | IndexBuildFailure:
    if isinstance(cards, Path):
        root = cards
        try:
            card_items = discover_cards(cards)
        except (OSError, ValueError):
            return _index_path_failure()
    else:
        card_items = tuple(cards)
    base = Path(root) if root is not None else Path(".")
    try:
        resolved_base = base.resolve()
    except (OSError, RuntimeError):
        return _index_path_failure()
    if _scan_card_tree_for_symlinks(resolved_base / "cards", resolved_base):
        return _index_path_failure()
    entries: list[dict[str, Any]] = []
    for card in sorted(card_items, key=lambda item: item.card_id):
        relative_path = _index_card_path(card.path, base)
        if relative_path is None:
            return _index_path_failure()
        entries.append(
            {
                "id": card.card_id,
                "title": str(card.metadata.get("title", "")),
                "card_type": card.card_type,
                "status": card.status,
                "risk_level": str(card.metadata.get("risk_level", "")),
                "modules": sorted(str(item) for item in card.metadata.get("modules", [])),
                "question_types": sorted(
                    str(item) for item in card.metadata.get("question_types", [])
                ),
                "abilities": sorted(str(item) for item in card.metadata.get("abilities", [])),
                "path": relative_path,
            }
        )
    return {
        "schema_version": int(contract["schema_version"]),
        "generated_at": None,
        "cards": entries,
    }


def _index_path_failure() -> IndexBuildFailure:
    return IndexBuildFailure(
        errors=(
            Issue(
                "error",
                "invalid_card_path",
                "cards",
                "card path must resolve inside the library cards directory",
            ),
        )
    )


def _index_card_path(path: Path, base: Path) -> str | None:
    raw_path = path.as_posix()
    if (PureWindowsPath(raw_path).is_absolute() and not path.is_absolute()) or (
        "\\" in raw_path
    ):
        return None
    relative = PurePosixPath(raw_path)
    if ".." in relative.parts:
        return None
    try:
        resolved_root = base.resolve()
        lexical_cards = resolved_root / "cards"
        if lexical_cards.is_symlink():
            return None
        resolved_cards = lexical_cards.resolve()
        if not _inside(resolved_cards, resolved_root):
            return None
        if path.is_absolute():
            lexical_path = path.absolute()
        elif len(relative.parts) >= 3 and relative.parts[0] == "cards":
            lexical_path = (resolved_root / Path(*relative.parts)).absolute()
        else:
            lexical_path = path.absolute()
        within_cards = lexical_path.relative_to(lexical_cards)
    except (OSError, RuntimeError, ValueError):
        return None
    if len(within_cards.parts) < 2 or within_cards.suffix != ".md":
        return None

    cursor = lexical_cards
    for part in within_cards.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return None
    try:
        resolved_path = lexical_path.resolve()
    except (OSError, RuntimeError):
        return None
    if not _inside(resolved_path, resolved_cards):
        return None
    return PurePosixPath("cards", *within_cards.parts).as_posix()


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def search_cards(
    root: Path,
    query: str,
    *,
    statuses: set[str] | None = None,
    modules: set[str] | None = None,
) -> tuple[SearchResult, ...]:
    needle = _normalized(query)
    results: list[SearchResult] = []
    for card in discover_cards(Path(root)):
        if statuses and card.status not in statuses:
            continue
        card_modules = {str(item) for item in card.metadata.get("modules", [])}
        if modules and not modules.intersection(card_modules):
            continue
        fields = {
            "id": card.card_id,
            "title": str(card.metadata.get("title", "")),
            "tags": " ".join(
                str(value)
                for key in ("modules", "question_types", "abilities")
                for value in card.metadata.get(key, [])
            ),
            "body": card.body,
        }
        weights = {"id": 100, "title": 80, "tags": 50, "body": 20}
        matched = tuple(
            name
            for name, value in fields.items()
            if needle and needle in _normalized(value)
        )
        if not needle or matched:
            results.append(SearchResult(card, sum(weights[name] for name in matched), matched))
    return tuple(sorted(results, key=lambda item: (-item.score, item.card.card_id)))
