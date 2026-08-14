import argparse
import json

from chinese_exam_kit.doctor import inspect_environment, render_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cekit")
    parser.add_argument("--version", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    doctor_parser = subparsers.add_parser("doctor", help="check local capabilities")
    doctor_parser.add_argument("--json", action="store_true", dest="json_output")
    doctor_parser.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print("cekit 0.1.0")
    if args.command == "doctor":
        report = inspect_environment()
        if args.report:
            print(render_report(report, redact=True))
        elif args.json_output:
            print(json.dumps(report.as_dict(), ensure_ascii=False))
        else:
            print(render_report(report, redact=True))
        return 1 if any(not item.available and item.level == "core" for item in report.capabilities) else 0
    return 0
