"""macOS Apple Vision OCR provider with import-safe platform checks."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .providers import CapabilityUnavailable


OCR_FAILURE_MESSAGE = (
    "Apple Vision OCR unavailable; verify the local image and Swift installation, "
    "or provide another local OcrProvider such as a Tesseract adapter"
)


class MacOSVisionOcr:
    """Recognize one page locally through the bundled Apple Vision helper."""

    name = "apple-vision"

    def __init__(self) -> None:
        self.script = Path(__file__).with_name("apple_vision_ocr.swift")

    def available(self) -> bool:
        return platform.system() == "Darwin" and shutil.which("swift") is not None and self.script.is_file()

    def recognize(self, image: Path) -> str:
        if not self.available():
            raise CapabilityUnavailable(OCR_FAILURE_MESSAGE)
        swift = shutil.which("swift")
        if swift is None:  # Defensive against an environment change after available().
            raise CapabilityUnavailable(OCR_FAILURE_MESSAGE)
        try:
            with tempfile.TemporaryDirectory(prefix="cekit-ocr-") as temporary:
                workdir = Path(temporary)
                shutil.copy2(image, workdir / image.name)
                output = workdir / "ocr.md"
                subprocess.run(
                    [swift, self.script, workdir, output],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                if not output.is_file():
                    raise OSError("OCR provider produced no output")
                text = output.read_text(encoding="utf-8")
                recognized = re.sub(r"^<!-- (?:answer-)?page:\d+ -->\s*", "", text).strip()
                if not recognized:
                    raise OSError("OCR provider produced empty output")
                return recognized
        except (OSError, UnicodeError, subprocess.SubprocessError):
            raise CapabilityUnavailable(OCR_FAILURE_MESSAGE) from None
