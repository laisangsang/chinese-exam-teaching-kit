"""macOS Apple Vision OCR provider with import-safe platform checks."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .providers import CapabilityUnavailable


class MacOSVisionOcr:
    """Recognize one page locally through the bundled Apple Vision helper."""

    name = "apple-vision"

    def __init__(self) -> None:
        self.script = Path(__file__).with_name("apple_vision_ocr.swift")

    def available(self) -> bool:
        return platform.system() == "Darwin" and shutil.which("swift") is not None and self.script.is_file()

    def recognize(self, image: Path) -> str:
        if not self.available():
            raise CapabilityUnavailable(
                "Apple Vision OCR unavailable; provide a local OcrProvider "
                "(for example a Tesseract adapter) or run on macOS with Swift installed"
            )
        swift = shutil.which("swift")
        if swift is None:  # Defensive against an environment change after available().
            raise CapabilityUnavailable("OCR unavailable; provide a local OcrProvider")
        with tempfile.TemporaryDirectory(prefix="cekit-ocr-") as temporary:
            workdir = Path(temporary)
            shutil.copy2(image, workdir / image.name)
            output = workdir / "ocr.md"
            try:
                subprocess.run(
                    [swift, self.script, workdir, output],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (OSError, subprocess.CalledProcessError):
                raise RuntimeError(
                    "Apple Vision OCR failed; retry with another local OcrProvider"
                ) from None
            text = output.read_text(encoding="utf-8")
        return re.sub(r"^<!-- (?:answer-)?page:\d+ -->\s*", "", text).strip()
