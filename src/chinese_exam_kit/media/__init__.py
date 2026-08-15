"""Optional, local-only media learning interfaces."""

from .learning import (
    CommandExecutor,
    FrameReference,
    LocalMediaProvider,
    MediaChapter,
    MediaLearningResult,
    UnavailableMediaProvider,
    write_media_index,
)

__all__ = [
    "CommandExecutor",
    "FrameReference",
    "LocalMediaProvider",
    "MediaChapter",
    "MediaLearningResult",
    "UnavailableMediaProvider",
    "write_media_index",
]
