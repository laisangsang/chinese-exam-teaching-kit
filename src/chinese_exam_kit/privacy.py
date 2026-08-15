"""Shared detection for host-specific path locators in public text."""

from __future__ import annotations

import re


HOST_LOCATOR_PATTERNS = (
    re.compile(r"file://", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"),
    re.compile(r"(?<![\\/])(?:\\\\|//)[^\\/\s]"),
    re.compile(r"(?<![A-Za-z0-9_\u3400-\u9fff])/(?!/)[^\s'\">)\]]"),
)


def contains_host_locator(value: str) -> bool:
    """Return true for POSIX, drive, UNC or file-URI absolute locators."""
    return any(pattern.search(value) for pattern in HOST_LOCATOR_PATTERNS)
