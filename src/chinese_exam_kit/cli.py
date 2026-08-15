"""Non-interactive command line interface for the public local workflow."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

from chinese_exam_kit import __version__
from chinese_exam_kit.content.docx import build_all
from chinese_exam_kit.content.validate import format_issues_text, validate_content_dir
from chinese_exam_kit.delivery import DeliveryManifest, write_delivery_manifest
from chinese_exam_kit.doctor import inspect_environment, render_report
from chinese_exam_kit.pipeline.intake import archive_inputs
from chinese_exam_kit.pipeline.models import PipelineTask
from chinese_exam_kit.pipeline.runner import (
    PipelineRunner,
    _atomic_text,
    _project_relative,
    safe_task_path,
    summarize,
)
from chinese_exam_kit.pipeline.state import load_task, save_task
from chinese_exam_kit.workspace import WorkspaceLayout


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, "cekit: 参数无效\n")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="cekit")
    parser.add_argument("--version", action="store_true")
    commands = parser.add_subparsers(dest="command")

    doctor = commands.add_parser("doctor", help="check local capabilities")
    doctor.add_argument("--json", action="store_true", dest="json_output")
    doctor.add_argument("--report", action="store_true")

    init = commands.add_parser("init", help="create a private local task")
    init.add_argument("--name", required=True)
    init.add_argument("--input", required=True, action="append", dest="inputs")

    run = commands.add_parser("run", help="run or resume a local task")
    run.add_argument("--task", required=True)

    status = commands.add_parser("status", help="show task state")
    status.add_argument("--task", required=True)
    status.add_argument("--json", action="store_true", dest="json_output")

    validate = commands.add_parser("validate", help="validate formal Markdown")
    group = validate.add_mutually_exclusive_group(required=True)
    group.add_argument("--task")
    group.add_argument("--content")

    build = commands.add_parser("build", help="validate and build Word documents")
    build.add_argument("--content", required=True)
    build.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    root = Path.cwd().resolve()
    if args.version:
        print(f"cekit {__version__}")
        return 0
    try:
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "init":
            return _init(root, args.name, args.inputs)
        if args.command == "run":
            summary = PipelineRunner(root).resume(args.task)
            print(_summary_text(summary.status))
            return 0
        if args.command == "status":
            task_path = safe_task_path(root, args.task)
            summary = summarize(load_task(task_path))
            safe_name = _display_path(root, task_path)
            if args.json_output:
                print(
                    json.dumps(
                        summary.to_dict(task_path=safe_name),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            else:
                print(_status_text(summary, safe_name))
            return 0
        if args.command == "validate":
            content = (
                _task_content(root, args.task)
                if args.task
                else _relative_input(root, args.content)
            )
            issues = validate_content_dir(content)
            print(format_issues_text(issues))
            return 2 if issues else 0
        if args.command == "build":
            content = _relative_input(root, args.content)
            output = _relative_output(root, args.output)
            issues = validate_content_dir(content)
            if issues:
                print(format_issues_text(issues))
                return 2
            manifest_path = _build(root, content, output)
            print(f"已生成 {_display_path(root, manifest_path)}")
            return 0
        parser.print_help()
        return 0
    except (ValueError, FileNotFoundError, UnicodeError):
        print("输入错误：参数或项目内文件状态无效。", file=sys.stderr)
        return 2
    except Exception:
        print("内部错误：本地处理失败，请检查环境与文件权限。", file=sys.stderr)
        return 1


def _doctor(args: argparse.Namespace) -> int:
    report = inspect_environment()
    if args.report:
        print(render_report(report, redact=True))
    elif args.json_output:
        print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(render_report(report, redact=True))
    return 1 if any(
        not item.available and item.level == "core" for item in report.capabilities
    ) else 0


def _init(root: Path, name: str, inputs: list[str]) -> int:
    slug = _slugify(name)
    layout = WorkspaceLayout.create(root, slug)
    task_path = layout.root / "task.json"
    if task_path.exists():
        raise ValueError("同名任务已经存在")
    task = PipelineTask.create(slug, name.strip(), layout.root)
    sources = tuple(_relative_or_external_input(root, value) for value in inputs)
    task = archive_inputs(task, sources)
    save_task(task)
    print(_display_path(root, task_path))
    return 0


def _build(root: Path, content: Path, output: Path) -> Path:
    outputs = build_all(content, output)
    checklist = output / "visual-review-checklist.md"
    _atomic_text(
        checklist,
        "# 逐页视觉验收\n\n请渲染并逐页检查全部 Word；本清单不代表视觉验收通过。\n",
    )
    manifest = DeliveryManifest.automatic(
        outputs=tuple(_project_relative(root, path) for path in outputs),
        evidence=(_project_relative(root, checklist),),
    )
    return write_delivery_manifest(manifest, output / "delivery.json")


def _task_content(root: Path, task_value: str) -> Path:
    return safe_task_path(root, task_value).parent / "content"


def _relative_input(root: Path, value: str) -> Path:
    candidate = Path(value)
    if ".." in candidate.parts:
        raise ValueError("路径必须位于当前项目内")
    if not candidate.is_absolute():
        candidate = root / candidate
    absolute = candidate.absolute()
    cursor = absolute
    try:
        while True:
            if cursor.is_symlink():
                raise ValueError("路径不得使用符号链接")
            if cursor.resolve(strict=False) == root:
                break
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        resolved = absolute.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("路径必须位于当前项目内") from None
    return resolved


def _relative_output(root: Path, value: str) -> Path:
    output = _relative_input(root, value)
    if output.exists() and not output.is_dir():
        raise ValueError("输出路径必须是目录")
    return output


def _relative_or_external_input(root: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("输入材料不存在或不是常规文件")
    return candidate


def _slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name).strip()
    slug = re.sub(r"[^\w.-]+", "-", normalized, flags=re.UNICODE).strip("-.")
    if not slug:
        raise ValueError("任务名称无法生成安全标识")
    return slug[:80]


def _display_path(root: Path, path: Path) -> str:
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return Path(path).name


def _summary_text(status: str) -> str:
    if status == "needs_user_input":
        return "流水线已暂停：请按 work_orders/analysis.md 编写并校验正式 Markdown 源稿。"
    if status == "completed":
        return "流水线已完成；逐页视觉验收仍须人工确认。"
    return f"流水线状态：{status}"


def _status_text(summary, task_path: str) -> str:
    lines = [f"任务：{task_path}", f"状态：{summary.status}"]
    lines.extend(f"- {name}: {summary.stage(name).status}" for name in summary.stages)
    return "\n".join(lines)
