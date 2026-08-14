"""Local, privacy-safe checks for optional teaching-kit capabilities."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib.util import find_spec
import platform as platform_module
import re
import shutil
import sys
from typing import Literal


CapabilityLevel = Literal["core", "enhanced", "experimental"]


@dataclass(frozen=True)
class Capability:
    name: str
    available: bool
    level: CapabilityLevel
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    platform: str
    python: str
    capabilities: tuple[Capability, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "python": self.python,
            "capabilities": [asdict(capability) for capability in self.capabilities],
        }


def _module_capability(name: str, module: str, level: CapabilityLevel) -> Capability:
    available = find_spec(module) is not None
    detail = f"{module} {'available' if available else 'not installed'}"
    return Capability(name, available, level, detail)


def _command_capability(name: str, command: str, level: CapabilityLevel) -> Capability:
    available = shutil.which(command) is not None
    detail = f"{command} {'available' if available else 'not found'}"
    return Capability(name, available, level, detail)


def inspect_environment() -> DoctorReport:
    """Inspect local tools only; this function never downloads or contacts a service."""
    transcription_modules = ("whisper", "faster_whisper", "speech_recognition")
    transcription_available = any(find_spec(module) is not None for module in transcription_modules)
    capabilities = (
        Capability("python", sys.version_info >= (3, 11), "core", f"Python {sys.version.split()[0]}"),
        _module_capability("word", "docx", "core"),
        _module_capability("pdf_text", "pypdf", "core"),
        _command_capability("pdf_render", "pdftoppm", "enhanced"),
        _command_capability("ocr", "tesseract", "enhanced"),
        _command_capability("video", "ffmpeg", "enhanced"),
        _command_capability("libreoffice", "libreoffice", "experimental"),
        _command_capability("swift", "swift", "experimental"),
        Capability(
            "transcription",
            transcription_available,
            "experimental",
            "optional transcription module " + ("available" if transcription_available else "not installed"),
        ),
    )
    return DoctorReport(platform_module.system(), sys.version.split()[0], capabilities)


def _redact(text: str) -> str:
    """Replace filesystem paths so reports are safe to share in issue trackers."""
    windows_path = r"(?<![\w.-])[A-Za-z]:\\(?:[^\\\r\n;]+\\)*[^\\\r\n;]+"
    posix_path = r"(?<![\w.-])/(?:[^/\r\n;]+/)*[^/\r\n;]+"
    text = re.sub(windows_path, "[redacted-path]", text)
    return re.sub(posix_path, "[redacted-path]", text)


def render_report(report: DoctorReport, redact: bool = True) -> str:
    """Render a readable local capability report without exposing local paths."""
    lines = ["cekit environment report", f"Platform: {report.platform}", f"Python: {report.python}", "Capabilities:"]
    for item in report.capabilities:
        status = "available" if item.available else "unavailable"
        lines.append(f"- {item.name} [{item.level}]: {status} — {item.detail}")
    missing_enhancements = [item.name for item in report.capabilities if not item.available and item.level != "core"]
    if missing_enhancements:
        lines.append("Degraded mode: optional capabilities unavailable: " + ", ".join(missing_enhancements))
    text = "\n".join(lines)
    return _redact(text) if redact else text
