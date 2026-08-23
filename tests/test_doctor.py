import json

from chinese_exam_kit.cli import main
from chinese_exam_kit.doctor import Capability, DoctorReport, inspect_environment, render_report
from chinese_exam_kit.extract.providers import UnavailableOcr, default_ocr_provider
from tests._host_samples import posix_path, windows_path


def test_report_redacts_home_and_username(tmp_path):
    report = DoctorReport(
        platform="macOS",
        python="3.12.4",
        capabilities=(Capability("word", True, "core", str(tmp_path / "local-user")),),
    )
    text = render_report(report, redact=True)
    assert str(tmp_path) not in text
    assert "local-user" not in text


def test_report_redacts_windows_paths_with_spaces_without_erasing_context():
    report = DoctorReport(
        platform="Windows",
        python="3.12.4",
        capabilities=(
            Capability(
                "word",
                True,
                "core",
                "stored at "
                + windows_path("Users", "sampleuser", "Exam Files", "final report.docx")
                + "; ready for use",
            ),
        ),
    )
    text = render_report(report, redact=True)
    for secret in ("sampleuser", "Exam Files", "final report.docx", windows_path("Users")):
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
                "stored at "
                + posix_path("Users", "sampleuser", "Exam Files", "final report.docx")
                + "; ready for use",
            ),
        ),
    )
    text = render_report(report, redact=True)
    for secret in ("sampleuser", "Exam", "Files", "final", "report.docx", posix_path("Users")):
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
    assert posix_path("Users") + "/" not in output


def test_default_ocr_provider_is_apple_vision_on_darwin_and_unavailable_elsewhere():
    darwin = default_ocr_provider(platform_name="Darwin")
    linux = default_ocr_provider(platform_name="Linux")

    assert darwin.name == "apple-vision"
    assert isinstance(linux, UnavailableOcr)
    assert linux.available() is False


def test_doctor_uses_the_same_default_ocr_provider_as_extraction(monkeypatch):
    class FakeOcr:
        name = "apple-vision"

        def available(self):
            return True

    monkeypatch.setattr("chinese_exam_kit.doctor.default_ocr_provider", FakeOcr)

    capability = next(
        item for item in inspect_environment().capabilities if item.name == "ocr"
    )

    assert capability.available is True
    assert "apple-vision" in capability.detail


def test_tesseract_presence_does_not_claim_a_builtin_ocr_provider(monkeypatch):
    monkeypatch.setattr(
        "chinese_exam_kit.doctor.default_ocr_provider",
        lambda: UnavailableOcr("default OCR unavailable"),
    )
    monkeypatch.setattr(
        "chinese_exam_kit.doctor.shutil.which",
        lambda command: "/usr/bin/tesseract" if command == "tesseract" else None,
    )

    capability = next(
        item for item in inspect_environment().capabilities if item.name == "ocr"
    )

    assert capability.available is False
    assert "tesseract" not in capability.detail.casefold()
