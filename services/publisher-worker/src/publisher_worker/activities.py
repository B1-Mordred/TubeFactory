from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
from cryptography.fernet import Fernet, InvalidToken
from minio import Minio
from temporalio import activity
from temporalio.exceptions import ApplicationError

from editorial_core.publishing import map_processing_state, youtube_insert_body
from publisher_worker.config import Settings
from publisher_worker.db import append_audit
from publisher_worker.youtube import FakeYouTubeProvider, YouTubeAPI, YouTubeProviderError


_fake = FakeYouTubeProvider()


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _crypt(settings: Settings) -> Fernet:
    return Fernet(settings.encryption_key.encode())


def _decrypt(settings: Settings, value: bytes) -> str:
    try:
        return _crypt(settings).decrypt(value).decode()
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise ApplicationError("publishing secret could not be decrypted", non_retryable=True) from exc


def _minio(settings: Settings) -> Minio:
    return Minio(
        settings.minio_endpoint, access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key, secure=False,
    )


async def _asset_bytes(client: Minio, bucket: str, key: str) -> bytes:
    def get() -> bytes:
        response = client.get_object(bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
    return await asyncio.to_thread(get)


async def _load_context(connection: asyncpg.Connection, publication_id: UUID):
    row = await connection.fetchrow(
        """SELECT p.*,c.refresh_token_encrypted,c.token_fingerprint,c.status AS connection_status,
                  c.enabled AS connection_enabled,m.document AS metadata_document,
                  m.content_hash AS current_metadata_hash,r.content_hash AS current_render_hash,
                  r.render_number,r.video_asset_id,va.object_key AS video_object_key,
                  va.mime_type AS video_mime_type,va.byte_size AS video_byte_size,
                  pc.real_uploads_enabled,ph.active_version_id AS active_config_id,
                  pa.decision AS upload_decision,pa.purpose AS upload_purpose,
                  pa.render_hash AS approved_render_hash,pa.metadata_hash AS approved_metadata_hash
           FROM publications p
           JOIN youtube_connections c ON c.id=p.connection_id
           JOIN publish_metadata_versions m ON m.id=p.metadata_version_id
           JOIN production_renders r ON r.id=p.render_id
           JOIN media_assets va ON va.id=r.video_asset_id
           JOIN publishing_configuration_versions pc ON pc.id=p.configuration_version_id
           JOIN publishing_configuration_head ph ON ph.singleton=true
           JOIN publication_approvals pa ON pa.id=p.upload_approval_id
           WHERE p.id=$1""",
        publication_id,
    )
    if row is None:
        raise ApplicationError("publication does not exist", non_retryable=True)
    if (
        row["connection_status"] != "active" or not row["connection_enabled"]
        or row["upload_decision"] != "approved" or row["upload_purpose"] != "private_upload"
        or row["render_hash"] != row["current_render_hash"]
        or row["render_hash"] != row["approved_render_hash"]
        or row["metadata_hash"] != row["current_metadata_hash"]
        or row["metadata_hash"] != row["approved_metadata_hash"]
    ):
        raise ApplicationError("publication binding or authorization changed", non_retryable=True)
    final_approval = await connection.fetchval(
        """SELECT id FROM approvals WHERE target_type='production_render' AND target_id=$1
           AND target_version=$2 AND target_hash=$3 AND decision='approved'
           ORDER BY created_at DESC LIMIT 1""",
        row["render_id"], row["render_number"], row["render_hash"],
    )
    if final_approval is None:
        raise ApplicationError("exact render final approval is missing", non_retryable=True)
    if row["mode"] == "real" and (not row["real_uploads_enabled"] or row["active_config_id"] != row["configuration_version_id"]):
        raise ApplicationError("real upload configuration is disabled or superseded", non_retryable=True)
    return row


async def _attachment(connection: asyncpg.Connection, document: dict, key: str):
    try:
        asset_id = UUID(str(document[key]["asset_id"]))
    except (ValueError, KeyError) as exc:
        raise ApplicationError(f"metadata {key} asset is invalid", non_retryable=True) from exc
    row = await connection.fetchrow("SELECT object_key,mime_type,content_hash FROM media_assets WHERE id=$1", asset_id)
    if row is None or row["content_hash"] != document[key].get("content_hash"):
        raise ApplicationError(f"metadata {key} asset changed or is missing", non_retryable=True)
    return row


@activity.defn(name="perform-youtube-private-upload")
async def perform_private_upload(request: dict) -> dict:
    settings, publication_id = Settings(), UUID(str(request["publication_id"]))
    connection = await asyncpg.connect(settings.database_dsn)
    api: YouTubeAPI | None = None
    try:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"publication:{publication_id}")
            row = await _load_context(connection, publication_id)
            document = _json(row["metadata_document"])
            caption = await _attachment(connection, document, "captions")
            thumbnail = await _attachment(connection, document, "thumbnail")
            await connection.execute(
                "UPDATE publications SET state='uploading',updated_at=$2,version=version+1,failure=NULL WHERE id=$1",
                publication_id, datetime.now(timezone.utc),
            )
        body = youtube_insert_body(document)
        client = _minio(settings)
        if row["mode"] == "dry_run":
            result = await _fake.upload_private(idempotency_key=row["upload_idempotency_key"], body=body)
        else:
            if not settings.youtube_client_id or not settings.youtube_client_secret:
                raise ApplicationError("YouTube OAuth client secrets are unavailable to publisher", non_retryable=True)
            refresh_token = _decrypt(settings, row["refresh_token_encrypted"])
            access_token = await YouTubeAPI.refresh_token(refresh_token, settings.youtube_client_id, settings.youtube_client_secret)
            api = YouTubeAPI(access_token)
            if row["youtube_video_id"]:
                result = None
                video_id = row["youtube_video_id"]
            else:
                session_url = _decrypt(settings, row["resumable_session_encrypted"]) if row["resumable_session_encrypted"] else None
                if session_url is None:
                    session_url = await api.initiate_resumable(body, byte_size=row["video_byte_size"], mime=row["video_mime_type"])
                    encrypted_session = _crypt(settings).encrypt(session_url.encode())
                    await connection.execute(
                        "UPDATE publications SET resumable_session_encrypted=$2,updated_at=$3,version=version+1 WHERE id=$1 AND youtube_video_id IS NULL",
                        publication_id, encrypted_session, datetime.now(timezone.utc),
                    )
                source_url = await asyncio.to_thread(
                    client.presigned_get_object, settings.minio_bucket, row["video_object_key"], timedelta(hours=2)
                )
                result = await api.upload_from_url(
                    session_url, source_url, byte_size=row["video_byte_size"], mime=row["video_mime_type"]
                )
                video_id = result.video_id
        if row["mode"] == "dry_run":
            video_id = result.video_id
        await connection.execute(
            """UPDATE publications SET youtube_video_id=$2,state='uploaded_private',uploaded_bytes=$3,
               processing_status=$4::jsonb,updated_at=$5,version=version+1 WHERE id=$1""",
            publication_id, video_id, row["video_byte_size"],
            json.dumps(result.resource if result else _json(row["processing_status"])), datetime.now(timezone.utc),
        )
        caption_state = _json(row["caption_status"])
        if caption_state.get("status") != "completed":
            caption_body = await _asset_bytes(client, settings.minio_bucket, caption["object_key"])
            caption_name = f"TubeFactory {document['captions']['language']} {row['metadata_hash'][:8]}"
            caption_result = await (_fake if row["mode"] == "dry_run" else api).upload_caption(
                video_id, document["captions"]["language"], caption_name,
                caption_body, caption["mime_type"],
            )
            await connection.execute(
                "UPDATE publications SET caption_status=$2::jsonb,updated_at=$3,version=version+1 WHERE id=$1",
                publication_id, json.dumps({"status": "completed", "provider": caption_result}), datetime.now(timezone.utc),
            )
        thumbnail_state = _json(row["thumbnail_status"])
        if thumbnail_state.get("status") != "completed":
            thumbnail_body = await _asset_bytes(client, settings.minio_bucket, thumbnail["object_key"])
            thumbnail_result = await (_fake if row["mode"] == "dry_run" else api).set_thumbnail(
                video_id, thumbnail_body, thumbnail["mime_type"],
            )
            await connection.execute(
                "UPDATE publications SET thumbnail_status=$2::jsonb,updated_at=$3,version=version+1 WHERE id=$1",
                publication_id, json.dumps({"status": "completed", "provider": thumbnail_result}), datetime.now(timezone.utc),
            )
        async with connection.transaction():
            await append_audit(
                connection, action="publishing.private_upload_completed", actor_id=row["started_by"],
                target_type="publication", target_id=str(publication_id), correlation_id=row["correlation_id"],
                context={"mode": row["mode"], "youtube_video_id": video_id, "privacy_status": "private", "render_hash": row["render_hash"], "metadata_hash": row["metadata_hash"]},
            )
        return {"publication_id": str(publication_id), "video_id": video_id, "state": "uploaded_private"}
    except ApplicationError:
        raise
    except (YouTubeProviderError, Exception) as exc:
        if connection and not connection.is_closed():
            await connection.execute(
                "UPDATE publications SET state='failed',failure=$2::jsonb,updated_at=$3,version=version+1 WHERE id=$1",
                publication_id, json.dumps({"code": "publishing_activity_failed", "type": type(exc).__name__, "message": str(exc)[:500]}), datetime.now(timezone.utc),
            )
        if isinstance(exc, YouTubeProviderError):
            raise ApplicationError(str(exc), type="youtube_provider_error") from exc
        raise
    finally:
        if api:
            await api.close()
        await connection.close()


@activity.defn(name="reconcile-youtube-publication")
async def reconcile_publication(request: dict) -> dict:
    settings, publication_id = Settings(), UUID(str(request["publication_id"]))
    connection = await asyncpg.connect(settings.database_dsn)
    api: YouTubeAPI | None = None
    try:
        row = await _load_context(connection, publication_id)
        if not row["youtube_video_id"]:
            raise ApplicationError("publication has no video ID", non_retryable=True)
        if row["mode"] == "dry_run":
            resource = _json(row["processing_status"])
        else:
            access = await YouTubeAPI.refresh_token(_decrypt(settings, row["refresh_token_encrypted"]), settings.youtube_client_id, settings.youtube_client_secret)
            api = YouTubeAPI(access)
            resource = await api.reconcile(row["youtube_video_id"])
        state = map_processing_state(resource).value
        await connection.execute(
            "UPDATE publications SET state=$2,processing_status=$3::jsonb,updated_at=$4,version=version+1 WHERE id=$1",
            publication_id, state, json.dumps(resource), datetime.now(timezone.utc),
        )
        return {"publication_id": str(publication_id), "state": state, "video_id": row["youtube_video_id"]}
    finally:
        if api:
            await api.close()
        await connection.close()


@activity.defn(name="schedule-youtube-publication")
async def schedule_publication(request: dict) -> dict:
    settings, publication_id = Settings(), UUID(str(request["publication_id"]))
    publish_at = datetime.fromisoformat(str(request["publish_at"]))
    connection = await asyncpg.connect(settings.database_dsn)
    api: YouTubeAPI | None = None
    try:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"schedule:{request['schedule_idempotency_key']}")
            existing = await connection.fetchrow("SELECT * FROM publication_schedules WHERE schedule_idempotency_key=$1", request["schedule_idempotency_key"])
            if existing:
                return {"schedule_id": str(existing["id"]), "state": existing["state"]}
            row = await _load_context(connection, publication_id)
            approval = await connection.fetchrow("SELECT * FROM publication_approvals WHERE id=$1", UUID(str(request["approval_id"])))
            if (
                row["mode"] != "real" or not row["youtube_video_id"] or approval is None
                or approval["purpose"] != "public_release" or approval["decision"] != "approved"
                or approval["render_hash"] != row["render_hash"] or approval["metadata_hash"] != row["metadata_hash"]
            ):
                raise ApplicationError("exact public-release approval is missing", non_retryable=True)
        access = await YouTubeAPI.refresh_token(_decrypt(settings, row["refresh_token_encrypted"]), settings.youtube_client_id, settings.youtube_client_secret)
        api = YouTubeAPI(access)
        current = await api.reconcile(row["youtube_video_id"])
        desired = publish_at.isoformat().replace("+00:00", "Z")
        if current.get("status", {}).get("publishAt") == desired and current.get("status", {}).get("privacyStatus") == "private":
            result = current
        else:
            result = await api.schedule(row["youtube_video_id"], publish_at, current.get("status", {}))
        schedule_id = uuid4()
        async with connection.transaction():
            await connection.execute(
                """INSERT INTO publication_schedules
                   (id,publication_id,approval_id,publish_at,schedule_idempotency_key,state,provider_response,created_by,correlation_id,created_at)
                   VALUES($1,$2,$3,$4,$5,'scheduled',$6::jsonb,$7,$8,$9) ON CONFLICT(schedule_idempotency_key) DO NOTHING""",
                schedule_id, publication_id, UUID(str(request["approval_id"])), publish_at,
                request["schedule_idempotency_key"], json.dumps(result), UUID(str(request["actor_id"])),
                request["correlation_id"], datetime.now(timezone.utc),
            )
            await append_audit(
                connection, action="publishing.video_scheduled", actor_id=UUID(str(request["actor_id"])),
                target_type="publication", target_id=str(publication_id), correlation_id=request["correlation_id"],
                context={"youtube_video_id": row["youtube_video_id"], "publish_at": desired, "render_hash": row["render_hash"], "metadata_hash": row["metadata_hash"]},
            )
        return {"schedule_id": str(schedule_id), "state": "scheduled", "publish_at": desired}
    finally:
        if api:
            await api.close()
        await connection.close()
