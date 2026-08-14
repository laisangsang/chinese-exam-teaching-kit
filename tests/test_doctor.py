import json

from chinese_exam_kit.cli import main
from chinese_exam_kit.doctor import Capability, DoctorReport, inspect_environment, render_report


def test_report_redacts_home_and_username(tmp_path):
    report = DoctorReport(
        platform="macOS",
        python="3.12.4",
        capabilities=(Capability("word", True, "core", str(tmp_path / "liyuxiang")),),
    )
    text = render_report(report, redact=True)
    assert str(tmp_path) not in text
    assert "liyuxiang" not in text


def test_report_redacts_windows_paths_with_spaces_without_erasing_context():
    report = DoctorReport(
        platform="Windows",
        python="3.12.4",
        capabilities=(
            Capability(
                "word",
                True,
                "core",
                r"stored at C:\Users\sampleuser\Exam Files\final report.docx; ready for use",
            ),
        ),
    )
    text = render_report(report, redact=True)
    for secret in ("sampleuser", "Exam Files", "final report.docx", r"C:\Users"):
        assert secret not in text
    assert "ready for use" in text


def test_report_redacts_posix_paths_with_spaces_without_erasing_context():
    report = DoctorReport(
        platform="Linux",
        python="3.12.4",
        capabilities=(
            Capability(
                "word",
                True,
                "core",
                "stored at /Users/sampleuser/Exam Files/final report.docx; ready for use",
            ),
        ),
    )
    text = render_report(report, redact=True)
    for secret in ("sampleuser", "Exam", "Files", "final", "report.docx", "/Users"):
        assert secret not in text
    assert "ready for use" in text


def test_doctor_lists_core_and_optional_capabilities():
    names = {item.name for item in inspect_environment().capabilities}
    assert {"python", "word", "pdf_text", "pdf_render", "ocr", "video"} <= names


def test_doctor_json_output_is_machine_readable(capsys):
    assert main(["doctor", "--json"]) in {0, 1}
    payload = json.loads(capsys.readouterr().out)
    assert {"platform", "python", "capabilities"} <= payload.keys()
    assert all({"name", "available", "level", "detail"} <= item.keys() for item in payload["capabilities"])


def test_doctor_report_output_redacts_local_paths(capsys):
    assert main(["doctor", "--report"]) in {0, 1}
    output = capsys.readouterr().out
    assert "/Users/" not in output
