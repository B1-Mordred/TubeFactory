from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Sequence


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
YOUTUBE_SCOPES = (
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.upload",
)


class PublishingContractError(ValueError):
    pass


class PublishMode(StrEnum):
    DRY_RUN = "dry_run"
    REAL = "real"


class UploadState(StrEnum):
    READY = "ready"
    UPLOADING = "uploading"
    UPLOADED_PRIVATE = "uploaded_private"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


@dataclass(frozen=True)
class ReleaseBinding:
    render_id: str
    render_hash: str
    metadata_version_id: str
    metadata_hash: str

    def validate(self) -> None:
        if not self.render_id or not self.metadata_version_id:
            raise PublishingContractError("release binding IDs are required")
        if not _SHA256.fullmatch(self.render_hash) or not _SHA256.fullmatch(self.metadata_hash):
            raise PublishingContractError("release binding must use SHA-256 hashes")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def validate_metadata(document: Mapping[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    title = document.get("title")
    description = document.get("description")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
        errors.append("title must contain 1 to 100 characters")
    if not isinstance(description, str) or not 1 <= len(description) <= 5000:
        errors.append("description must contain 1 to 5000 characters")
    tags = document.get("tags")
    if not isinstance(tags, list) or len(tags) > 50 or any(not isinstance(v, str) or not v.strip() for v in tags):
        errors.append("tags must be a list of at most 50 non-empty strings")
    if not isinstance(document.get("category_id"), str) or not str(document.get("category_id", "")).isdigit():
        errors.append("category_id must be a YouTube numeric category string")
    language = document.get("language")
    if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language):
        errors.append("language must be a BCP-47 language tag")
    if not isinstance(document.get("made_for_kids"), bool):
        errors.append("made_for_kids must be explicit")
    if not isinstance(document.get("contains_synthetic_media"), bool):
        errors.append("contains_synthetic_media must be explicit")
    sources = document.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("at least one description source is required")
    else:
        for source in sources:
            if not isinstance(source, Mapping) or not source.get("title") or not source.get("url"):
                errors.append("each source requires title and URL")
                break
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        errors.append("at least one chapter is required")
    else:
        starts: list[int] = []
        for chapter in chapters:
            if not isinstance(chapter, Mapping) or not isinstance(chapter.get("start_seconds"), int) or not chapter.get("title"):
                errors.append("each chapter requires an integer start_seconds and title")
                break
            starts.append(chapter["start_seconds"])
        if starts and (starts[0] != 0 or starts != sorted(set(starts))):
            errors.append("chapters must start at zero and have unique ascending timestamps")
    captions = document.get("captions")
    if not isinstance(captions, Mapping) or not captions.get("asset_id") or not captions.get("language"):
        errors.append("an approved caption asset and language are required")
    thumbnail = document.get("thumbnail")
    if not isinstance(thumbnail, Mapping) or not thumbnail.get("asset_id"):
        errors.append("an approved thumbnail asset is required")
    return tuple(errors)


def youtube_insert_body(document: Mapping[str, Any]) -> dict[str, Any]:
    errors = validate_metadata(document)
    if errors:
        raise PublishingContractError("; ".join(errors))
    description = str(document["description"])
    chapter_lines = [
        f"{int(item['start_seconds']) // 60:02d}:{int(item['start_seconds']) % 60:02d} {item['title']}"
        for item in document["chapters"]
    ]
    source_lines = [f"- {item['title']}: {item['url']}" for item in document["sources"]]
    evidence_url = document.get("evidence_url")
    sections = [description, "Chapters\n" + "\n".join(chapter_lines), "Sources\n" + "\n".join(source_lines)]
    if evidence_url:
        sections.append(f"Evidence dossier: {evidence_url}")
    full_description = "\n\n".join(sections)
    if len(full_description) > 5000:
        raise PublishingContractError("composed YouTube description exceeds 5000 characters")
    return {
        "snippet": {
            "title": str(document["title"]).strip(),
            "description": full_description,
            "tags": list(document["tags"]),
            "categoryId": document["category_id"],
            "defaultLanguage": document["language"],
            "defaultAudioLanguage": document["language"],
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": document["made_for_kids"],
            "containsSyntheticMedia": document["contains_synthetic_media"],
        },
    }


def upload_idempotency_key(
    binding: ReleaseBinding, channel_connection_id: str, mode: PublishMode = PublishMode.REAL
) -> str:
    binding.validate()
    if not channel_connection_id:
        raise PublishingContractError("channel connection ID is required")
    return canonical_hash({"operation": "youtube-private-upload-v1", "mode": mode.value, "channel": channel_connection_id, **binding.__dict__})


def schedule_idempotency_key(binding: ReleaseBinding, video_id: str, publish_at: datetime) -> str:
    binding.validate()
    normalized = require_future_schedule(publish_at)
    if not video_id:
        raise PublishingContractError("YouTube video ID is required")
    return canonical_hash({
        "operation": "youtube-schedule-v1",
        "video_id": video_id,
        "publish_at": normalized.isoformat().replace("+00:00", "Z"),
        **binding.__dict__,
    })


def approval_matches(approved: ReleaseBinding, current: ReleaseBinding) -> bool:
    approved.validate()
    current.validate()
    return approved == current


def require_future_schedule(value: datetime, *, now: datetime | None = None) -> datetime:
    if value.tzinfo is None:
        raise PublishingContractError("publish_at must include a timezone")
    normalized = value.astimezone(timezone.utc)
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if normalized <= reference:
        raise PublishingContractError("publish_at must be in the future")
    return normalized


def map_processing_state(resource: Mapping[str, Any]) -> UploadState:
    status = resource.get("status") if isinstance(resource.get("status"), Mapping) else {}
    processing = resource.get("processingDetails") if isinstance(resource.get("processingDetails"), Mapping) else {}
    upload_status = status.get("uploadStatus")
    processing_status = processing.get("processingStatus")
    if upload_status in {"failed", "rejected", "deleted"} or processing_status in {"failed", "terminated"}:
        return UploadState.FAILED
    if upload_status == "processed" or processing_status == "succeeded":
        return UploadState.PROCESSED
    if upload_status in {"uploaded", "processing"} or processing_status:
        return UploadState.PROCESSING
    return UploadState.UPLOADED_PRIVATE


def validate_granted_scopes(scopes: Sequence[str]) -> None:
    missing = set(YOUTUBE_SCOPES) - set(scopes)
    if missing:
        raise PublishingContractError(f"OAuth grant is missing required scopes: {', '.join(sorted(missing))}")
