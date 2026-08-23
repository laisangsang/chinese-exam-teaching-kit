"""Page-aware, local-only document extraction."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from .providers import (
    CapabilityUnavailable,
    OcrProvider,
    Renderer,
    TextReader,
    default_ocr_provider,
)


TEXT_LAYER_MINIMUM_CHARACTERS = 80
EFFECTIVE_CHARACTER_RE = re.compile(r"[A-Za-z\u4e00-\u9fff]")
QUESTION_RE = re.compile(r"(?m)^\s*(?P<number>\d{1,3})[.．、]\s*")
RENDERED_PAGE_RE = re.compile(r"page-(?P<number>\d+)")
SECTION_HEADING_PATTERNS = (
    ("reading_1", re.compile(r"(?:信息类文本|非连续性文本)阅读")),
    ("reading_2", re.compile(r"(?:文学类文本|小说|散文)阅读")),
    ("classical_chinese", re.compile(r"文言文阅读|阅读\s*[ⅢIII三]")),
    ("poetry", re.compile(r"古代诗歌阅读|阅读\s*[ⅣIV四]")),
    ("language_use", re.compile(r"语言文字运用")),
    ("composition", re.compile(r"(?:^|\n)\s*(?:三、)?写作")),
)
SUPPORTED_SUFFIXES = frozenset({".md", ".txt", ".docx", ".pdf"})


@dataclass(frozen=True)
class PageText:
    number: int
    text: str


@dataclass(frozen=True)
class ExtractionResult:
    pages: tuple[PageText, ...]
    mode: Literal["text_layer", "ocr", "hybrid"]


@dataclass(frozen=True)
class QuestionCandidate:
    number: int
    page_start: int
    section: str
    text: str
    sequence_warning: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "number": self.number,
            "page_start": self.page_start,
            "section": self.section,
            "text": self.text,
        }
        if self.sequence_warning is not None:
            payload["sequence_warning"] = self.sequence_warning
        return payload


def extract_document(
    path: Path,
    *,
    text_reader: TextReader | None = None,
    renderer: Renderer | None = None,
    ocr: OcrProvider | None = None,
) -> ExtractionResult:
    """Extract a supported local document while keeping PDF page identity."""
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"不支持的文档格式：{suffix or '无扩展名'}")

    if suffix != ".pdf":
        default_readers = {
            ".md": _read_plain_text,
            ".txt": _read_plain_text,
            ".docx": _read_docx,
        }
        read_text = text_reader or default_readers[suffix]
        pages = _number_pages(read_text(source))
        if not pages:
            raise ValueError("文档没有可提取的页面")
        return ExtractionResult(pages, "text_layer")

    read_text = text_reader or _read_pdf_text
    pages = _number_pages(read_text(source))
    if not pages:
        raise ValueError("PDF 没有可提取的页面")
    sparse = {
        page.number
        for page in pages
        if len(EFFECTIVE_CHARACTER_RE.findall(page.text)) < TEXT_LAYER_MINIMUM_CHARACTERS
    }
    if not sparse:
        return ExtractionResult(pages, "text_layer")

    if ocr is None:
        ocr = default_ocr_provider()
    if ocr is None or not ocr.available():
        message = getattr(ocr, "message", "OCR unavailable; provide a local OcrProvider")
        raise CapabilityUnavailable(message)
    render_pages = renderer or _render_pdf_pages
    with tempfile.TemporaryDirectory(prefix="cekit-extract-") as temporary:
        images = tuple(render_pages(source, Path(temporary)))
        image_by_page = _rendered_pages_by_number(images, page_count=len(pages))
        extracted = tuple(
            PageText(page.number, ocr.recognize(image_by_page[page.number]))
            if page.number in sparse
            else page
            for page in pages
        )
    mode: Literal["ocr", "hybrid"] = "ocr" if len(sparse) == len(pages) else "hybrid"
    return ExtractionResult(extracted, mode)


def build_question_candidates(pages: Sequence[PageText]) -> list[QuestionCandidate]:
    """Find numbered question-like blocks and flag uncertain sequencing."""
    candidates: list[QuestionCandidate] = []
    previous_number: int | None = None
    current_section = "unclassified"
    for page in pages:
        matches = tuple(QUESTION_RE.finditer(page.text))
        headings = _section_heading_events(page.text)
        heading_index = 0
        for index, match in enumerate(matches):
            while heading_index < len(headings) and headings[heading_index][0] < match.start():
                current_section = headings[heading_index][1]
                heading_index += 1
            number = int(match.group("number"))
            end = matches[index + 1].start() if index + 1 < len(matches) else len(page.text)
            warning = None
            if previous_number is not None and number != previous_number + 1:
                warning = f"题号从 {previous_number} 跳至 {number}"
            candidates.append(
                QuestionCandidate(
                    number=number,
                    page_start=page.number,
                    section=current_section,
                    text=page.text[match.start() : end].strip(),
                    sequence_warning=warning,
                )
            )
            previous_number = number
        for _, section in headings[heading_index:]:
            current_section = section
    return candidates


def write_extraction_artifacts(
    extraction: ExtractionResult,
    output_dir: Path,
    *,
    questions: Sequence[QuestionCandidate] | None = None,
) -> None:
    """Write deterministic, source-path-free extraction review artifacts."""
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    candidates = list(questions) if questions is not None else build_question_candidates(extraction.pages)
    marked_text = "\n\n".join(
        f"<!-- page:{page.number} -->\n\n{page.text.strip()}" for page in extraction.pages
    )
    (destination / "question_text.md").write_text(marked_text + "\n", encoding="utf-8")
    _write_json(destination / "question_index.json", [candidate.to_dict() for candidate in candidates])
    review = {
        "empty_pages": [page.number for page in extraction.pages if not page.text.strip()],
        "extraction_mode": extraction.mode,
        "sequence_warnings": [
            {"message": candidate.sequence_warning, "number": candidate.number}
            for candidate in candidates
            if candidate.sequence_warning is not None
        ],
        "unclassified_questions": [
            candidate.number for candidate in candidates if candidate.section == "unclassified"
        ],
    }
    _write_json(destination / "extraction_review.json", review)


def _number_pages(texts: Sequence[str]) -> tuple[PageText, ...]:
    return tuple(PageText(number, text or "") for number, text in enumerate(texts, 1))


def _read_plain_text(path: Path) -> list[str]:
    return [path.read_text(encoding="utf-8-sig")]


def _read_docx(path: Path) -> list[str]:
    from docx import Document
    from docx.table import Table

    document = Document(path)
    lines: list[str] = []
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            lines.extend(
                "\t".join(cell.text.strip() for cell in row.cells)
                for row in block.rows
                if any(cell.text.strip() for cell in row.cells)
            )
        elif block.text:
            lines.append(block.text)
    return ["\n".join(lines)]


def _read_pdf_text(path: Path) -> list[str]:
    from pypdf import PdfReader

    return [page.extract_text() or "" for page in PdfReader(path).pages]


def _render_pdf_pages(path: Path, output_dir: Path) -> list[Path]:
    command = shutil.which("pdftoppm")
    if command is None:
        raise CapabilityUnavailable(
            "PDF rendering unavailable; install Poppler (pdftoppm) or provide a Renderer"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = output_dir / "page"
    try:
        subprocess.run(
            [command, "-r", "300", "-png", str(path), str(prefix)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        raise RuntimeError(
            "PDF rendering failed; verify Poppler or provide another local Renderer"
        ) from None
    images = sorted(output_dir.glob("page-*.png"), key=_page_image_sort_key)
    if not images:
        raise RuntimeError("PDF 渲染未生成页面图片")
    return images


def _page_image_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"(\d+)(?!.*\d)", path.stem)
    return (int(match.group(1)) if match else 0, path.name)


def _rendered_pages_by_number(
    images: Sequence[Path],
    *,
    page_count: int,
) -> dict[int, Path]:
    """Validate renderer output using its strict ``page-N`` filename contract."""
    by_number: dict[int, Path] = {}
    for item in images:
        image = Path(item)
        if not image.is_file():
            raise ValueError("渲染结果包含不是可用文件的路径")
        match = RENDERED_PAGE_RE.fullmatch(image.stem)
        if match is None:
            raise ValueError("渲染结果包含无法识别页码的文件；文件名必须为 page-N")
        number = int(match.group("number"))
        if number < 1 or number > page_count:
            raise ValueError(f"渲染页码 {number} 超出文档范围 1-{page_count}")
        if number in by_number:
            raise ValueError(f"渲染结果包含重复页码 {number}")
        by_number[number] = image
    missing = sorted(set(range(1, page_count + 1)) - set(by_number))
    if missing:
        rendered = "、".join(str(number) for number in missing)
        raise ValueError(f"渲染结果缺少第 {rendered} 页")
    return by_number


def _section_heading_events(text: str) -> list[tuple[int, str]]:
    return sorted(
        (match.start(), section)
        for section, pattern in SECTION_HEADING_PATTERNS
        for match in pattern.finditer(text)
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
