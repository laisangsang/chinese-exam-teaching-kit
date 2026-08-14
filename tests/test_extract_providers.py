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

    with pytest.raises(RuntimeError) as captured:
        module.MacOSVisionOcr().recognize(image)

    rendered = "".join(traceback.format_exception(captured.value))
    assert str(tmp_path) not in rendered
