import json
from pathlib import Path

import pytest

from chinese_exam_kit.media.learning import (
    CommandExecutor,
    FrameReference,
    MediaChapter,
    MediaLearningResult,
    LocalMediaProvider,
    write_media_index,
)


def test_media_index_is_structured_deterministic_and_path_safe(tmp_path):
    result = MediaLearningResult.completed(
        chapters=(
            MediaChapter(
                chapter_id="chapter-1",
                title="第1题讲解",
                start_ms=1200,
                end_ms=9300,
                section="reading_1",
                question_numbers=(1,),
                transcript_path="media/transcripts/chapter-1.txt",
                frames=(FrameReference(4200, "media/frames/chapter-1-4200.png"),),
            ),
        )
    )

    destination = write_media_index(result, tmp_path / "media" / "index.json")
    payload = json.loads(destination.read_text(encoding="utf-8"))

    assert payload["chapters"][0]["time_range"] == {"start_ms": 1200, "end_ms": 9300}
    assert payload["chapters"][0]["question_numbers"] == [1]
    assert str(tmp_path) not in destination.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ("/tmp/transcript.txt", "../escape.txt", "C:/secret.txt"))
def test_media_chapter_rejects_absolute_or_escaping_paths(path):
    with pytest.raises(ValueError, match="relative"):
        MediaChapter(
            chapter_id="chapter-1",
            title="原创讲解",
            start_ms=0,
            end_ms=1,
            transcript_path=path,
        )


def test_command_executor_never_uses_a_shell(monkeypatch):
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return object()

    monkeypatch.setattr("chinese_exam_kit.media.learning.subprocess.run", fake_run)
    CommandExecutor().run(("ffmpeg", "-version"))

    assert observed["command"] == ("ffmpeg", "-version")
    assert observed["shell"] is False


def test_degraded_media_result_contains_no_host_path():
    result = MediaLearningResult.degraded("ffmpeg unavailable")

    assert result.status == "degraded"
    assert result.chapters == ()
    assert "/" not in result.message


@pytest.mark.parametrize(
    "message",
    (
        "failed at /private/var/folders/job.tmp",
        r"failed at C:\\Users\\teacher\\job.tmp",
        "failed at /home/teacher/job.tmp",
    ),
)
def test_media_result_rejects_any_host_absolute_path(message):
    with pytest.raises(ValueError, match="host path"):
        MediaLearningResult.degraded(message)


def test_missing_frame_tool_degrades_even_when_transcription_is_available(tmp_path):
    chapter = MediaChapter(
        chapter_id="chapter-1",
        title="原创讲解",
        start_ms=0,
        end_ms=1000,
        transcript_path="media/transcripts/chapter-1.txt",
    )
    provider = LocalMediaProvider(
        transcriber=lambda source, output: (chapter,),
        frame_extractor=None,
        command_finder=lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
    )

    result = provider.process((tmp_path / "lesson.mp4",), tmp_path / "media")

    assert result.status == "degraded"
    assert result.chapters == (chapter,)
