from datetime import datetime, timedelta, timezone

import pytest

from editorial_core.publishing import (
    PublishMode,
    PublishingContractError,
    ReleaseBinding,
    UploadState,
    approval_matches,
    map_processing_state,
    schedule_idempotency_key,
    upload_idempotency_key,
    validate_metadata,
    youtube_insert_body,
)


def metadata(**overrides):
    value = {
        "title": "A carefully sourced title",
        "description": "What the evidence actually supports.",
        "sources": [{"title": "Primary record", "url": "https://example.test/source"}],
        "evidence_url": "https://example.test/evidence/1",
        "chapters": [{"start_seconds": 0, "title": "Introduction"}],
        "tags": ["evidence"],
        "category_id": "27",
        "language": "en",
        "made_for_kids": False,
        "contains_synthetic_media": True,
        "captions": {"asset_id": "captions", "language": "en", "name": "English"},
        "thumbnail": {"asset_id": "thumbnail"},
    }
    value.update(overrides)
    return value


def binding(metadata_hash="b" * 64):
    return ReleaseBinding("render", "a" * 64, "metadata", metadata_hash)


def test_metadata_is_complete_and_always_uploads_private():
    assert validate_metadata(metadata()) == ()
    body = youtube_insert_body(metadata())
    assert body["status"] == {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": True,
    }
    assert "00:00 Introduction" in body["snippet"]["description"]
    assert "Primary record" in body["snippet"]["description"]


def test_metadata_rejects_missing_release_material():
    errors = validate_metadata(metadata(sources=[], chapters=[], captions={}, thumbnail={}))
    assert {message.split()[0] for message in errors} == {"at", "an"}


def test_exact_version_approval_is_invalidated_by_metadata_change():
    assert approval_matches(binding(), binding())
    assert not approval_matches(binding(), binding("c" * 64))


def test_upload_and_schedule_keys_are_stable_and_input_bound():
    first = upload_idempotency_key(binding(), "channel-1")
    assert first == upload_idempotency_key(binding(), "channel-1")
    assert first != upload_idempotency_key(binding(), "channel-2")
    assert first != upload_idempotency_key(binding(), "channel-1", PublishMode.DRY_RUN)
    publish_at = datetime.now(timezone.utc) + timedelta(days=1)
    assert schedule_idempotency_key(binding(), "video-1", publish_at) == schedule_idempotency_key(binding(), "video-1", publish_at)
    with pytest.raises(PublishingContractError, match="future"):
        schedule_idempotency_key(binding(), "video-1", datetime.now(timezone.utc) - timedelta(seconds=1))


def test_processing_status_mapping():
    assert map_processing_state({"status": {"uploadStatus": "uploaded"}, "processingDetails": {"processingStatus": "processing"}}) is UploadState.PROCESSING
    assert map_processing_state({"status": {"uploadStatus": "processed"}}) is UploadState.PROCESSED
    assert map_processing_state({"status": {"uploadStatus": "rejected"}}) is UploadState.FAILED
    assert PublishMode.DRY_RUN.value == "dry_run"
