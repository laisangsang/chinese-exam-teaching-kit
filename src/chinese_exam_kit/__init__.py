"""Public package metadata."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("chinese-exam-teaching-kit")
except PackageNotFoundError:  # Source tree imported without an installed distribution.
    __version__ = "0+unknown"
