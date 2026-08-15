import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chinese_exam_kit.delivery import (
    DeliveryManifest,
    load_delivery_manifest,
    mark_visual_review,
    write_delivery_manifest,
)


def test_automatic_manifest_never_claims_visual_pass_and_is_json_safe():
    manifest = DeliveryManifest.automatic(
        outputs=("output/docx/01_reading.docx",),
        evidence=("tmp/render/page-1.png",),
    )

    assert manifest.visual_status == "evidence_ready"
    assert manifest.reviewed_by is None
    assert manifest.reviewed_at is None
    assert manifest.to_dict()["outputs"] == ["output/docx/01_reading.docx"]
    json.dumps(manifest.to_dict(), ensure_ascii=False)

    with pytest.raises(ValueError, match="manual review"):
        DeliveryManifest(outputs=("guide.docx",), visual_status="passed")


@pytest.mark.parametrize(
    ("status", "evidence"),
    (("not_run", ()), ("evidence_ready", ("tmp/page-1.png",)), ("failed", ())),
)
def test_manifest_allows_only_non_passed_automatic_states(status, evidence):
    manifest = DeliveryManifest(
        outputs=("guide.docx",), visual_status=status, evidence=evidence
    )
    assert manifest.visual_status == status


def test_manifest_rejects_unknown_status_and_absolute_or_parent_paths(tmp_path):
    with pytest.raises(ValueError, match="visual_status"):
        DeliveryManifest(outputs=("guide.docx",), visual_status="looks_good")
    with pytest.raises(ValueError, match="relative"):
        DeliveryManifest(outputs=(str(tmp_path / "guide.docx"),))
    with pytest.raises(ValueError, match="relative"):
        DeliveryManifest(outputs=("../guide.docx",))


@pytest.mark.parametrize("value", ("guide.docx", b"guide.docx"))
def test_manifest_rejects_scalar_path_iterables(value):
    with pytest.raises(ValueError, match="iterable"):
        DeliveryManifest(outputs=value)


@pytest.mark.parametrize("value", ("tmp/page.png", b"tmp/page.png"))
def test_automatic_manifest_rejects_scalar_evidence_before_status_inference(value):
    with pytest.raises(ValueError, match="evidence.*iterable"):
        DeliveryManifest.automatic(outputs=("guide.docx",), evidence=value)


def test_manual_review_requires_signature_time_and_evidence():
    manifest = DeliveryManifest.automatic(
        outputs=("output/guide.docx",), evidence=("tmp/render/page-1.png",)
    )
    reviewed_at = datetime(2026, 8, 15, 10, 30, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="signature"):
        mark_visual_review(manifest, signature=" ", reviewed_at=reviewed_at)
    with pytest.raises(ValueError, match="timezone"):
        mark_visual_review(
            manifest,
            signature="李老师",
            reviewed_at=datetime(2026, 8, 15, 10, 30),
        )
    with pytest.raises(ValueError, match="evidence"):
        mark_visual_review(
            DeliveryManifest(outputs=("output/guide.docx",)),
            signature="李老师",
            reviewed_at=reviewed_at,
        )

    passed = mark_visual_review(manifest, signature="李老师", reviewed_at=reviewed_at)
    assert passed.visual_status == "passed"
    assert passed.reviewed_by == "李老师"
    assert passed.reviewed_at == "2026-08-15T10:30:00+00:00"


def test_json_loading_cannot_upgrade_to_passed_without_complete_review_record(tmp_path):
    payload = {
        "schema_version": 1,
        "outputs": ["output/guide.docx"],
        "visual_status": "passed",
        "evidence": ["tmp/render/page-1.png"],
        "reviewed_by": "",
        "reviewed_at": "2026-08-15T10:30:00+00:00",
    }
    path = tmp_path / "delivery.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="review record"):
        load_delivery_manifest(path)


def test_loading_a_complete_passed_record_requires_matching_review_confirmation(tmp_path):
    reviewed = mark_visual_review(
        DeliveryManifest.automatic(
            outputs=("output/guide.docx",), evidence=("tmp/render/page-1.png",)
        ),
        signature="李老师",
        reviewed_at=datetime(2026, 8, 15, 10, 30, tzinfo=timezone.utc),
    )
    path = write_delivery_manifest(reviewed, tmp_path / "delivery.json")

    with pytest.raises(ValueError, match="manual review API"):
        DeliveryManifest.from_dict(reviewed.to_dict())

    with pytest.raises(ValueError, match="confirmation"):
        load_delivery_manifest(path)
    with pytest.raises(ValueError, match="confirmation"):
        load_delivery_manifest(path, review_signature="另一位教师")
    assert load_delivery_manifest(path, review_signature="李老师") == reviewed


def test_manifest_write_is_deterministic_atomic_and_path_safe(tmp_path, monkeypatch):
    manifest = DeliveryManifest.automatic(
        outputs=("output/b.docx", "output/a.docx"),
        evidence=("tmp/page-2.png", "tmp/page-1.png"),
    )
    destination = tmp_path / "delivery.json"
    first = write_delivery_manifest(manifest, destination)
    first_bytes = first.read_bytes()
    write_delivery_manifest(manifest, destination)
    assert destination.read_bytes() == first_bytes
    assert str(tmp_path).encode() not in first_bytes
    assert json.loads(first_bytes)["outputs"] == ["output/a.docx", "output/b.docx"]

    destination.write_bytes(b"old-manifest")

    def fail_replace(source_path, destination_path):
        raise OSError("private absolute path")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError) as error:
        write_delivery_manifest(manifest, destination)
    assert str(tmp_path) not in str(error.value)
    assert destination.read_bytes() == b"old-manifest"
    assert list(tmp_path.glob(".*.partial.json")) == []


def test_manifest_writer_rejects_symlink_destination(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("unchanged", encoding="utf-8")
    destination = tmp_path / "delivery.json"
    try:
        destination.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ValueError, match="symlink"):
        write_delivery_manifest(DeliveryManifest(outputs=("guide.docx",)), destination)
    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_manifest_loader_rejects_symlinked_parent_directory(tmp_path):
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    path = write_delivery_manifest(
        DeliveryManifest(outputs=("guide.docx",)), real_directory / "delivery.json"
    )
    linked_directory = tmp_path / "linked"
    try:
        linked_directory.symlink_to(real_directory, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ValueError, match="symlink"):
        load_delivery_manifest(linked_directory / path.name)


@pytest.mark.parametrize(
    "payload",
    (
        [],
        {},
        {"schema_version": 1, "visual_status": "not_run"},
        {"schema_version": 1, "visual_status": "not_run", "outputs": 42},
        {
            "schema_version": 1,
            "visual_status": "evidence_ready",
            "outputs": ["guide.docx"],
            "evidence": 42,
        },
    ),
)
def test_manifest_loader_rejects_malformed_roots_and_fields_stably(tmp_path, payload):
    path = tmp_path / "malformed.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="delivery manifest") as error:
        load_delivery_manifest(path)

    assert str(tmp_path) not in str(error.value)
