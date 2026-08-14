import importlib
import subprocess
import traceback

import pytest

from chinese_exam_kit.extract import CapabilityUnavailable


def test_macos_provider_module_is_import_safe():
    module = importlib.import_module("chinese_exam_kit.extract.macos")

    assert module.MacOSVisionOcr.__name__ == "MacOSVisionOcr"


def test_macos_provider_explains_local_alternative_when_unavailable(monkeypatch, tmp_path):
    module = importlib.import_module("chinese_exam_kit.extract.macos")
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    provider = module.MacOSVisionOcr()

    assert provider.available() is False
    with pytest.raises(CapabilityUnavailable, match=r"OCR.*OcrProvider"):
        provider.recognize(tmp_path / "page.png")


def test_macos_provider_runs_swift_without_a_shell_and_returns_clean_text(monkeypatch, tmp_path):
    module = importlib.import_module("chinese_exam_kit.extract.macos")
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda command: "/usr/bin/swift" if command == "swift" else None,
    )
    image = tmp_path / "page.png"
    image.write_bytes(b"image fixture")
    calls = []

    def fake_run(command, *, check, capture_output, text):
        calls.append(command)
        output = command[3]
        output.write_text("<!-- page:1 -->\n\n原创识别结果\n", encoding="utf-8")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    text = module.MacOSVisionOcr().recognize(image)

    assert text == "原创识别结果"
    assert len(calls) == 1
    assert isinstance(calls[0], list)
    assert calls[0][0] == "/usr/bin/swift"


def test_macos_provider_failure_does_not_echo_local_paths(monkeypatch, tmp_path):
    module = importlib.import_module("chinese_exam_kit.extract.macos")
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/swift")
    image = tmp_path / "sensitive-page.png"
    image.write_bytes(b"image fixture")

    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="local failure")

    monkeypatch.setattr(module.subprocess, "run", fail)

    with pytest.raises(CapabilityUnavailable, match=r"OCR.*OcrProvider") as captured:
        module.MacOSVisionOcr().recognize(image)

    rendered = "".join(traceback.format_exception(captured.value))
    assert str(tmp_path) not in rendered


def test_macos_provider_copy_failure_is_sanitized_and_actionable(monkeypatch, tmp_path):
    module = importlib.import_module("chinese_exam_kit.extract.macos")
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/swift")
    image = tmp_path / "sensitive-page.png"
    image.write_bytes(b"image fixture")

    def fail_copy(*_):
        raise OSError(f"cannot copy {image}")

    monkeypatch.setattr(module.shutil, "copy2", fail_copy)

    with pytest.raises(CapabilityUnavailable, match=r"OCR.*OcrProvider") as captured:
        module.MacOSVisionOcr().recognize(image)

    assert str(tmp_path) not in str(captured.value)


def test_macos_provider_missing_output_is_sanitized_and_actionable(monkeypatch, tmp_path):
    module = importlib.import_module("chinese_exam_kit.extract.macos")
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/swift")
    image = tmp_path / "sensitive-page.png"
    image.write_bytes(b"image fixture")
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: None)

    with pytest.raises(CapabilityUnavailable, match=r"OCR.*OcrProvider") as captured:
        module.MacOSVisionOcr().recognize(image)

    assert str(tmp_path) not in str(captured.value)


def test_macos_provider_empty_output_is_treated_as_unavailable(monkeypatch, tmp_path):
    module = importlib.import_module("chinese_exam_kit.extract.macos")
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/swift")
    image = tmp_path / "page.png"
    image.write_bytes(b"image fixture")

    def create_empty_output(command, **_kwargs):
        command[3].write_text("", encoding="utf-8")

    monkeypatch.setattr(module.subprocess, "run", create_empty_output)

    with pytest.raises(CapabilityUnavailable, match=r"OCR.*OcrProvider"):
        module.MacOSVisionOcr().recognize(image)


def test_macos_provider_output_read_failure_is_sanitized_and_actionable(monkeypatch, tmp_path):
    module = importlib.import_module("chinese_exam_kit.extract.macos")
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/swift")
    image = tmp_path / "sensitive-page.png"
    image.write_bytes(b"image fixture")
    original_read_text = module.Path.read_text

    def create_output(command, **_kwargs):
        command[3].write_bytes(b"\xff")

    def fail_for_output(path, *args, **kwargs):
        if path.name == "ocr.md":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", create_output)
    monkeypatch.setattr(module.Path, "read_text", fail_for_output)

    with pytest.raises(CapabilityUnavailable, match=r"OCR.*OcrProvider") as captured:
        module.MacOSVisionOcr().recognize(image)

    assert str(tmp_path) not in str(captured.value)


def test_macos_provider_output_io_failure_is_sanitized_and_actionable(monkeypatch, tmp_path):
    module = importlib.import_module("chinese_exam_kit.extract.macos")
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/swift")
    image = tmp_path / "sensitive-page.png"
    image.write_bytes(b"image fixture")

    def create_output(command, **_kwargs):
        command[3].write_text("原创识别结果", encoding="utf-8")

    def fail_read(path, *args, **kwargs):
        raise OSError(f"cannot read {path}")

    monkeypatch.setattr(module.subprocess, "run", create_output)
    monkeypatch.setattr(module.Path, "read_text", fail_read)

    with pytest.raises(CapabilityUnavailable, match=r"OCR.*OcrProvider") as captured:
        module.MacOSVisionOcr().recognize(image)

    assert str(tmp_path) not in str(captured.value)
