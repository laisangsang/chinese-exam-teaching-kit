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
from tests._host_samples import file_uri, posix_path, unc_path, windows_path


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


@pytest.mark.parametrize(
    "path",
    (posix_path("tmp", "transcript.txt"), "../escape.txt", "C:" + "/secret.txt"),
)
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
        "failed at " + posix_path("private", "var", "folders", "job.tmp"),
        "failed at " + windows_path("Users", "teacher", "job.tmp"),
        "failed at " + posix_path("home", "teacher", "job.tmp"),
    ),
)
def test_media_result_rejects_any_host_absolute_path(message):
    with pytest.raises(ValueError, match="host path"):
        MediaLearningResult.degraded(message)


@pytest.mark.parametrize("chapters", ("chapter", b"chapter", (object(),)))
def test_media_result_rejects_string_or_non_chapter_iterables(chapters):
    with pytest.raises((TypeError, ValueError), match="chapter"):
        MediaLearningResult.completed(chapters=chapters)


def test_media_result_rejects_non_string_status_stably():
    with pytest.raises(TypeError, match="status"):
        MediaLearningResult([], (), "")


@pytest.mark.parametrize("frames", ("frame", b"frame", (object(),)))
def test_media_chapter_rejects_string_or_non_frame_iterables(frames):
    with pytest.raises((TypeError, ValueError), match="frame"):
        MediaChapter(
            chapter_id="chapter-1",
            title="原创讲解",
            start_ms=0,
            end_ms=1,
            frames=frames,
        )


@pytest.mark.parametrize(
    "field,value",
    (
        ("chapter_id", file_uri("Users", "person-a", "chapter")),
        ("title", windows_path("Users", "person-a", "exam.pdf")),
        ("section", unc_path("server", "share", "exam.pdf")),
        ("title", "材料位于 '" + posix_path("Users", "person-a", "exam.pdf") + "'"),
    ),
)
def test_media_chapter_rejects_host_paths_in_all_free_text(field, value):
    kwargs = {
        "chapter_id": "chapter-1",
        "title": "原创讲解",
        "section": "reading_1",
        "start_ms": 0,
        "end_ms": 1,
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match="host path"):
        MediaChapter(**kwargs)


def test_media_chapter_allows_normal_chinese_slash_expression():
    chapter = MediaChapter(
        chapter_id="chapter-1",
        title="输入/输出关系",
        section="阅读/表达",
        start_ms=0,
        end_ms=1,
    )

    assert chapter.title == "输入/输出关系"


@pytest.mark.parametrize(
    "start,end",
    ((True, 2), (0, False), (0.5, 2), (0, 2.5), (-1, 2), (2, 2)),
)
def test_media_chapter_rejects_non_integer_or_invalid_time_ranges(start, end):
    with pytest.raises((TypeError, ValueError), match="time"):
        MediaChapter("chapter-1", "原创讲解", start, end)


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


def test_invalid_injected_transcriber_output_degrades_instead_of_raising(tmp_path):
    provider = LocalMediaProvider(
        transcriber=lambda source, output: "not chapters",
        frame_extractor=lambda source, output, chapters: chapters,
        command_finder=lambda name: "/usr/bin/ffmpeg",
    )

    result = provider.process((tmp_path / "lesson.mp4",), tmp_path / "media")

    assert result.status == "degraded"
    assert result.chapters == ()


def test_media_receipt_status_must_match_persisted_stage(tmp_path):
    from chinese_exam_kit.pipeline.runner import _valid_media_receipt

    index = tmp_path / "index.json"
    index.write_text('{"status":"degraded"}', encoding="utf-8")
    import hashlib

    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fingerprint": "f" * 64,
                "status": "degraded",
                "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    assert _valid_media_receipt(receipt, index, "f" * 64) == {"status": "degraded"}
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["status"] = "completed"
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    assert _valid_media_receipt(receipt, index, "f" * 64) is None
