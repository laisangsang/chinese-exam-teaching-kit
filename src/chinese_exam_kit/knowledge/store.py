"""Deterministic local storage and search for public knowledge cards."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping


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


def load_contract(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
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


def _card_paths(root: Path) -> list[Path]:
    cards_root = root / "cards"
    return sorted(cards_root.glob("*/*.md")) if cards_root.is_dir() else []


def discover_cards(root: Path) -> tuple[KnowledgeCard, ...]:
    return tuple(parse_card(path) for path in _card_paths(root))


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
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


def _issue(card: KnowledgeCard, root: Path, code: str, message: str, level: str = "error") -> Issue:
    return Issue(level, code, _display_path(card.path, root), message)


def _validate_card(card: KnowledgeCard, root: Path, contract: Mapping[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    metadata = card.metadata
    required = contract["required_metadata"]
    for field in required:
        if field not in metadata:
            issues.append(_issue(card, root, "missing_metadata", f"missing metadata field: {field}"))

    card_type = str(metadata.get("card_type", ""))
    type_contract = contract["card_types"].get(card_type)
    if type_contract is None:
        issues.append(_issue(card, root, "invalid_card_type", "unknown card type"))
    elif not re.fullmatch(rf"{re.escape(type_contract['prefix'])}-\d{{4}}", str(metadata.get("id", ""))):
        issues.append(_issue(card, root, "invalid_id", "card id does not match its type"))

    if metadata.get("status") not in contract["statuses"]:
        issues.append(_issue(card, root, "invalid_status", "unknown knowledge status"))
    if metadata.get("risk_level") not in contract["risk_levels"]:
        issues.append(_issue(card, root, "invalid_risk_level", "unknown risk level"))
    modules = metadata.get("modules", [])
    if not isinstance(modules, list) or not modules or any(
        module not in contract["modules"] for module in modules
    ):
        issues.append(_issue(card, root, "invalid_module", "modules contain unknown or empty values"))

    for heading in contract["required_sections"]:
        if not card.sections.get(heading, "").strip():
            issues.append(_issue(card, root, "missing_section", f"missing or empty section: {heading}"))

    sources = metadata.get("sources", [])
    if not isinstance(sources, list) or not sources:
        issues.append(_issue(card, root, "missing_source", "at least one source is required"))
    else:
        for source in sources:
            if not isinstance(source, dict):
                issues.append(_issue(card, root, "invalid_source", "source must be an object"))
                continue
            if any(
                not isinstance(source.get(field), str) or not source[field].strip()
                for field in contract.get("required_source_fields", ("kind", "name", "locator"))
            ):
                issues.append(_issue(card, root, "invalid_source", "source fields must be nonempty"))
            if source.get("kind") not in contract["source_kinds"]:
                issues.append(_issue(card, root, "invalid_source", "unknown source kind"))
            locator = source.get("locator")
            if not isinstance(locator, str) or not locator.strip():
                issues.append(_issue(card, root, "invalid_source", "source locator is required"))
            elif _is_absolute_locator(locator):
                issues.append(_issue(card, root, "absolute_path", "source locator must be project-relative"))
            elif _locator_escapes(locator):
                issues.append(_issue(card, root, "path_escape", "source locator cannot escape the project"))
    return issues


def validate_library(root: Path, contract: Mapping[str, Any]) -> LibraryValidation:
    root = Path(root)
    issues: list[Issue] = []
    cards: list[KnowledgeCard] = []
    for path in _card_paths(root):
        try:
            cards.append(parse_card(path))
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            issues.append(Issue("error", "card_parse_error", _display_path(path, root), "card cannot be parsed"))

    for card in cards:
        issues.extend(_validate_card(card, root, contract))

    ids: dict[str, int] = {}
    for card in cards:
        card_id = card.metadata.get("id")
        if isinstance(card_id, str) and card_id:
            ids[card_id] = ids.get(card_id, 0) + 1
    for card_id, count in sorted(ids.items()):
        if count > 1:
            issues.append(Issue("error", "duplicate_id", "cards", f"duplicate card id: {card_id}"))
    index_path = root / "index.json"
    if index_path.is_file():
        try:
            saved = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            issues.append(Issue("error", "invalid_index", "index.json", "index is not valid JSON"))
        else:
            has_card_errors = any(issue.level == "error" for issue in issues)
            if not has_card_errors and saved != build_index(tuple(cards), contract, root):
                issues.append(Issue("error", "index_out_of_sync", "index.json", "index must be rebuilt"))
    return LibraryValidation(tuple(cards), tuple(issues))


def build_index(
    cards: Iterable[KnowledgeCard] | Path,
    contract: Mapping[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    if isinstance(cards, Path):
        root = cards
        card_items = discover_cards(cards)
    else:
        card_items = tuple(cards)
    base = Path(root) if root is not None else Path(".")
    entries: list[dict[str, Any]] = []
    for card in sorted(card_items, key=lambda item: item.card_id):
        if card.path.is_absolute():
            try:
                relative_path = card.path.resolve().relative_to(base.resolve()).as_posix()
            except ValueError as error:
                raise ValueError("card path must stay inside the knowledge library") from error
        else:
            relative = PurePosixPath(card.path.as_posix())
            if ".." in relative.parts:
                raise ValueError("card path must stay inside the knowledge library")
            relative_path = relative.as_posix()
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
        matched = tuple(name for name, value in fields.items() if needle and needle in _normalized(value))
        if not needle or matched:
            results.append(SearchResult(card, sum(weights[name] for name in matched), matched))
    return tuple(sorted(results, key=lambda item: (-item.score, item.card.card_id)))
