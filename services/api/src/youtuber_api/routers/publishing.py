from __future__ import annotations

import hashlib
import base64
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from editorial_core.authorization import Permission
from editorial_core.publishing import (
    YOUTUBE_SCOPES, PublishMode, PublishingContractError, ReleaseBinding,
    canonical_hash, require_future_schedule, schedule_idempotency_key,
    upload_idempotency_key, validate_granted_scopes, validate_metadata,
)
from youtuber_api.audit import append_audit
from youtuber_api.config import get_settings
from youtuber_api.db import get_session
from youtuber_api.models import (
    ApprovalModel, ChannelProfileModel, MediaAssetModel, MediaProductionModel, OAuthStateModel,
    OpportunityModel, OriginalityReportModel, ProductionManifestModel, ProductionRenderModel, PublicationApprovalModel, PublicationModel,
    PublicationScheduleModel, PublishMetadataVersionModel,
    PublishingConfigurationHeadModel, PublishingConfigurationVersionModel,
    QAReportModel, ScriptModel, SourceFreshnessCheckModel, SourceSnapshotModel, StoryboardModel, StoryboardVersionModel,
    SubjectProfileModel, UserModel, WorkflowControlRecordModel, YouTubeConnectionModel,
)
from youtuber_api.publishing_crypto import encrypt_publication_secret, secret_fingerprint
from youtuber_api.publishing_gateway import TemporalPublishingGateway
from youtuber_api.schemas import (
    AutomaticContinuation, MockYouTubeConnectionWrite, PublicationApprovalView, PublicationApprovalWrite,
    PublicationScheduleWrite, PublicationStart, PublicationView,
    PublishMetadataView, PublishMetadataWrite, PublishingConfigurationView,
    PublishingConfigurationWrite, YouTubeConnectionView, YouTubeOAuthStart,
    YouTubeOAuthStartView,
)
from youtuber_api.security import require


router = APIRouter(prefix="/publishing", tags=["publishing"])
Viewer = Annotated[UserModel, Depends(require(Permission.VIEW))]
Editor = Annotated[UserModel, Depends(require(Permission.EDIT_EDITORIAL))]
Operator = Annotated[UserModel, Depends(require(Permission.UPLOAD_PRIVATE))]
ReleaseAuthorizer = Annotated[UserModel, Depends(require(Permission.AUTHORIZE_PUBLICATION))]
Admin = Annotated[UserModel, Depends(require(Permission.MANAGE_SYSTEM))]
SecretAdmin = Annotated[UserModel, Depends(require(Permission.MANAGE_SECRETS))]


def _connection_view(item: YouTubeConnectionModel) -> YouTubeConnectionView:
    return YouTubeConnectionView.model_validate(item, from_attributes=True)


def _metadata_view(item: PublishMetadataVersionModel) -> PublishMetadataView:
    return PublishMetadataView.model_validate(item, from_attributes=True)


def _publication_view(item: PublicationModel) -> PublicationView:
    return PublicationView.model_validate(item, from_attributes=True)


async def _commit(session: AsyncSession, detail: str) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=detail) from exc


async def _require_publisher_gateway() -> None:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{settings.publisher_gateway_endpoint}/health")
        if response.status_code != 200:
            raise RuntimeError("publisher is not ready")
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Publishing profile is not running; start the publisher-worker profile first",
        ) from exc


async def _render_channel_id(
    session: AsyncSession, render: ProductionRenderModel
) -> UUID | None:
    production = await session.get(MediaProductionModel, render.production_id)
    storyboard_version = (
        await session.get(StoryboardVersionModel, production.storyboard_version_id)
        if production else None
    )
    storyboard = (
        await session.get(StoryboardModel, storyboard_version.storyboard_id)
        if storyboard_version else None
    )
    script = await session.get(ScriptModel, storyboard.script_id) if storyboard else None
    opportunity = (
        await session.get(OpportunityModel, script.opportunity_id) if script else None
    )
    subject = (
        await session.get(SubjectProfileModel, opportunity.subject_profile_id)
        if opportunity else None
    )
    return subject.channel_profile_id if subject else None


async def _require_matching_channel_lineage(
    session: AsyncSession,
    render: ProductionRenderModel,
    connection: YouTubeConnectionModel,
) -> None:
    render_channel_id = await _render_channel_id(session, render)
    if render_channel_id is None or render_channel_id != connection.channel_profile_id:
        raise HTTPException(
            status_code=409,
            detail="The selected render does not belong to the connected channel",
        )


async def _active_config(session: AsyncSession) -> PublishingConfigurationVersionModel | None:
    head = await session.get(PublishingConfigurationHeadModel, True)
    return await session.get(PublishingConfigurationVersionModel, head.active_version_id) if head else None


async def _exact_render_is_approved(session: AsyncSession, render: ProductionRenderModel) -> bool:
    qa = await session.scalar(select(QAReportModel).where(QAReportModel.render_id == render.id))
    approval = await session.scalar(select(ApprovalModel.id).where(
        ApprovalModel.target_type == "production_render",
        ApprovalModel.target_id == render.id,
        ApprovalModel.target_version == render.render_number,
        ApprovalModel.target_hash == render.content_hash,
        ApprovalModel.decision == "approved",
    ))
    return qa is not None and qa.verdict != "fail" and approval is not None


async def _binding(
    session: AsyncSession, render_id: UUID, expected_render_hash: str,
    metadata_version_id: UUID, expected_metadata_hash: str,
) -> tuple[ProductionRenderModel, PublishMetadataVersionModel, ReleaseBinding]:
    render = await session.get(ProductionRenderModel, render_id)
    metadata = await session.get(PublishMetadataVersionModel, metadata_version_id)
    if (
        render is None or metadata is None or metadata.render_id != render.id
        or render.content_hash != expected_render_hash
        or metadata.content_hash != expected_metadata_hash
    ):
        raise HTTPException(status_code=409, detail="Use the exact render and publishing metadata versions and hashes")
    return render, metadata, ReleaseBinding(str(render.id), render.content_hash, str(metadata.id), metadata.content_hash)


async def _approval(session: AsyncSession, binding: ReleaseBinding, purpose: str) -> PublicationApprovalModel | None:
    return await session.scalar(select(PublicationApprovalModel).where(
        PublicationApprovalModel.purpose == purpose,
        PublicationApprovalModel.render_id == UUID(binding.render_id),
        PublicationApprovalModel.render_hash == binding.render_hash,
        PublicationApprovalModel.metadata_version_id == UUID(binding.metadata_version_id),
        PublicationApprovalModel.metadata_hash == binding.metadata_hash,
        PublicationApprovalModel.decision == "approved",
    ).order_by(PublicationApprovalModel.created_at.desc()).limit(1))


async def _require_current_publication_evidence(session: AsyncSession, render: ProductionRenderModel) -> dict[str, object]:
    production = await session.get(MediaProductionModel, render.production_id)
    manifest = await session.get(ProductionManifestModel, render.manifest_id)
    storyboard = await session.get(StoryboardVersionModel, production.storyboard_version_id) if production else None
    if production is None or manifest is None or storyboard is None:
        raise HTTPException(status_code=409, detail="Publication provenance is incomplete")
    source_hashes = {
        str(item.get("snapshot_hash")) for item in manifest.document.get("sources", [])
        if isinstance(item, dict) and item.get("snapshot_hash")
    }
    if not source_hashes:
        raise HTTPException(status_code=409, detail="Public release requires source snapshot provenance")
    snapshots = list(await session.scalars(select(SourceSnapshotModel).where(SourceSnapshotModel.content_hash.in_(source_hashes))))
    if {item.content_hash for item in snapshots} != source_hashes:
        raise HTTPException(status_code=409, detail="A manifest source snapshot is unavailable")
    now = datetime.now(timezone.utc)
    freshness_ids: list[str] = []
    for snapshot in snapshots:
        check = await session.scalar(
            select(SourceFreshnessCheckModel)
            .where(SourceFreshnessCheckModel.source_snapshot_id == snapshot.id)
            .order_by(SourceFreshnessCheckModel.created_at.desc()).limit(1)
        )
        if check is None or check.verdict != "pass" or (now - snapshot.retrieved_at).total_seconds() > check.maximum_age_seconds:
            raise HTTPException(
                status_code=409,
                detail=f"Source freshness must be rechecked before public release ({snapshot.content_hash[:12]})",
            )
        freshness_ids.append(str(check.id))
    originality = await session.scalar(
        select(OriginalityReportModel)
        .where(OriginalityReportModel.script_version_id == storyboard.script_version_id)
        .order_by(OriginalityReportModel.created_at.desc()).limit(1)
    )
    if originality is None or originality.verdict != "pass":
        raise HTTPException(status_code=409, detail="A passing originality report is required before public release")
    return {
        "source_freshness_check_ids": freshness_ids,
        "originality_report_id": str(originality.id),
        "script_version_id": str(storyboard.script_version_id),
    }


@router.get("/configuration", response_model=PublishingConfigurationView)
async def get_configuration(_: Viewer, session: Annotated[AsyncSession, Depends(get_session)]):
    item = await _active_config(session)
    if item is None:
        return PublishingConfigurationView(version_number=0, real_uploads_enabled=False, document={"provider": "youtube", "default_mode": "dry_run"})
    return PublishingConfigurationView.model_validate(item, from_attributes=True)


@router.post("/configuration", response_model=PublishingConfigurationView)
async def configure_publishing(
    payload: PublishingConfigurationWrite, request: Request, actor: Admin,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('publishing-configuration'))"))
    document = {"provider": payload.provider, "default_mode": "dry_run", "real_uploads_enabled": payload.real_uploads_enabled}
    content_hash = canonical_hash(document)
    now = datetime.now(timezone.utc)
    item = await session.scalar(
        select(PublishingConfigurationVersionModel).where(
            PublishingConfigurationVersionModel.content_hash == content_hash
        )
    )
    reused = item is not None
    if item is None:
        number = (await session.scalar(select(func.max(PublishingConfigurationVersionModel.version_number))) or 0) + 1
        item = PublishingConfigurationVersionModel(
            id=uuid4(), version_number=number, real_uploads_enabled=payload.real_uploads_enabled,
            document=document, content_hash=content_hash, created_by=actor.id,
            created_at=now, comment=payload.comment,
        )
        session.add(item)
        await session.flush()
    head = await session.get(PublishingConfigurationHeadModel, True, with_for_update=True)
    if head is None:
        session.add(PublishingConfigurationHeadModel(singleton=True, active_version_id=item.id, updated_at=now))
    else:
        head.active_version_id, head.updated_at = item.id, now
    await append_audit(
        session, action="publishing.configuration_reactivated" if reused else "publishing.configuration_activated", actor_id=actor.id,
        target_type="publishing_configuration_version", target_id=str(item.id),
        correlation_id=request.state.correlation_id,
        context={"version": item.version_number, "real_uploads_enabled": payload.real_uploads_enabled, "content_hash": item.content_hash, "reused": reused, "comment": payload.comment},
    )
    await _commit(session, "This publishing configuration version already exists")
    return PublishingConfigurationView.model_validate(item, from_attributes=True)


@router.post("/oauth/start", response_model=YouTubeOAuthStartView)
async def start_oauth(
    payload: YouTubeOAuthStart, actor: SecretAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await _require_publisher_gateway()
    settings = get_settings()
    if not settings.youtube_oauth_client_id or settings.youtube_oauth_client_id.startswith("replace-"):
        raise HTTPException(status_code=503, detail="YouTube OAuth client ID is not configured")
    if await session.get(ChannelProfileModel, payload.channel_profile_id) is None:
        raise HTTPException(status_code=404, detail="Channel profile not found")
    state = secrets.token_urlsafe(48)
    now, expires_at = datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(minutes=10)
    session.add(OAuthStateModel(
        state_hash=hashlib.sha256(state.encode()).hexdigest(), channel_profile_id=payload.channel_profile_id,
        initiated_by=actor.id, redirect_uri=payload.redirect_uri, expires_at=expires_at,
        consumed_at=None, created_at=now,
    ))
    await session.commit()
    query = urlencode({
        "client_id": settings.youtube_oauth_client_id, "redirect_uri": payload.redirect_uri,
        "response_type": "code", "scope": " ".join(YOUTUBE_SCOPES), "access_type": "offline",
        "include_granted_scopes": "true", "prompt": "consent", "state": state,
    })
    return YouTubeOAuthStartView(authorization_url=f"https://accounts.google.com/o/oauth2/v2/auth?{query}", expires_at=expires_at)


@router.get("/oauth/callback", response_model=YouTubeConnectionView)
async def oauth_callback(
    request: Request, state: Annotated[str, Query(min_length=20, max_length=512)],
    code: Annotated[str, Query(min_length=3, max_length=4096)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    row = await session.get(OAuthStateModel, hashlib.sha256(state.encode()).hexdigest(), with_for_update=True)
    now = datetime.now(timezone.utc)
    if row is None or row.consumed_at is not None or row.expires_at <= now:
        raise HTTPException(status_code=400, detail="OAuth state is invalid, expired, or already used")
    row.consumed_at = now
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.publisher_gateway_endpoint}/oauth/exchange",
            headers={"X-Publisher-Gateway-Token": settings.publisher_gateway_token},
            json={"code": code, "redirect_uri": row.redirect_uri},
        )
        if response.status_code != 200:
            await session.rollback()
            raise HTTPException(status_code=502, detail="Publisher OAuth gateway exchange failed")
        tokens = response.json()
        scopes = tokens.get("scopes", [])
        try:
            validate_granted_scopes(scopes)
        except PublishingContractError as exc:
            await session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not tokens.get("refresh_token_encrypted") or not tokens.get("youtube_channel_id"):
            await session.rollback()
            raise HTTPException(status_code=502, detail="Publisher OAuth gateway returned an incomplete grant")
        channel = {"id": tokens["youtube_channel_id"], "snippet": {"title": tokens["youtube_channel_title"]}}
    existing = await session.scalar(select(YouTubeConnectionModel).where(YouTubeConnectionModel.channel_profile_id == row.channel_profile_id).with_for_update())
    encrypted = base64.b64decode(tokens["refresh_token_encrypted"], validate=True)
    if existing is None:
        existing = YouTubeConnectionModel(
            id=uuid4(), channel_profile_id=row.channel_profile_id, created_by=row.initiated_by,
            created_at=now, version=1, youtube_channel_id=channel["id"],
            youtube_channel_title=channel["snippet"]["title"], refresh_token_encrypted=encrypted,
            token_fingerprint=tokens["token_fingerprint"], granted_scopes=scopes,
            status="active", enabled=True, updated_at=now,
        )
        session.add(existing)
    else:
        existing.youtube_channel_id, existing.youtube_channel_title = channel["id"], channel["snippet"]["title"]
        existing.refresh_token_encrypted, existing.token_fingerprint = encrypted, tokens["token_fingerprint"]
        existing.granted_scopes, existing.status, existing.enabled = scopes, "active", True
        existing.version, existing.updated_at = existing.version + 1, now
    await append_audit(
        session, action="publishing.youtube_connected", actor_id=row.initiated_by,
        target_type="youtube_connection", target_id=str(existing.id), correlation_id=request.state.correlation_id,
        context={"channel_profile_id": str(row.channel_profile_id), "youtube_channel_id": channel["id"], "scopes": scopes},
    )
    await session.commit()
    return _connection_view(existing)


@router.post("/connections/mock", response_model=YouTubeConnectionView)
async def create_mock_connection(
    payload: MockYouTubeConnectionWrite, request: Request, actor: SecretAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    if get_settings().app_environment == "production":
        raise HTTPException(status_code=404, detail="Not found")
    if await session.get(ChannelProfileModel, payload.channel_profile_id) is None:
        raise HTTPException(status_code=404, detail="Channel profile not found")
    now, token = datetime.now(timezone.utc), "mock-refresh-token"
    existing = await session.scalar(select(YouTubeConnectionModel).where(YouTubeConnectionModel.channel_profile_id == payload.channel_profile_id).with_for_update())
    if existing is None:
        existing = YouTubeConnectionModel(
            id=uuid4(), channel_profile_id=payload.channel_profile_id, created_by=actor.id,
            created_at=now, version=1, youtube_channel_id=payload.youtube_channel_id,
            youtube_channel_title=payload.youtube_channel_title,
            refresh_token_encrypted=encrypt_publication_secret(token), token_fingerprint=secret_fingerprint(token),
            granted_scopes=list(YOUTUBE_SCOPES), status="active", enabled=True, updated_at=now,
        )
        session.add(existing)
    else:
        existing.youtube_channel_id, existing.youtube_channel_title = payload.youtube_channel_id, payload.youtube_channel_title
        existing.refresh_token_encrypted, existing.token_fingerprint = encrypt_publication_secret(token), secret_fingerprint(token)
        existing.granted_scopes, existing.status, existing.enabled = list(YOUTUBE_SCOPES), "active", True
        existing.version, existing.updated_at = existing.version + 1, now
    await append_audit(session, action="publishing.mock_connection_configured", actor_id=actor.id, target_type="youtube_connection", target_id=str(existing.id), correlation_id=request.state.correlation_id, context={"channel_profile_id": str(payload.channel_profile_id)})
    await _commit(session, "A connection already exists for this channel")
    return _connection_view(existing)


@router.get("/connections", response_model=list[YouTubeConnectionView])
async def list_connections(_: Viewer, session: Annotated[AsyncSession, Depends(get_session)]):
    return [_connection_view(item) for item in await session.scalars(select(YouTubeConnectionModel).order_by(YouTubeConnectionModel.created_at))]


@router.post("/metadata", response_model=PublishMetadataView)
async def create_metadata(
    payload: PublishMetadataWrite, request: Request, actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    render = await session.get(ProductionRenderModel, payload.render_id)
    if render is None or render.content_hash != payload.expected_render_hash or not await _exact_render_is_approved(session, render):
        raise HTTPException(status_code=409, detail="Publishing metadata requires the exact approved render")
    document = payload.model_dump(mode="json", exclude={"render_id", "expected_render_hash", "comment"})
    errors = validate_metadata(document)
    if errors:
        raise HTTPException(status_code=422, detail=list(errors))
    for key, expected_kind in (("captions", {"caption_vtt", "caption_srt"}), ("thumbnail", {"thumbnail"})):
        try:
            asset_id = UUID(str(document[key]["asset_id"]))
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=f"{key} must reference an asset UUID") from exc
        asset = await session.get(MediaAssetModel, asset_id)
        if asset is None or asset.production_id != render.production_id or asset.asset_kind not in expected_kind:
            raise HTTPException(status_code=409, detail=f"{key} must reference an approved render-production asset")
        if key == "captions" and asset.byte_size > 100 * 1024 * 1024:
            raise HTTPException(status_code=422, detail="caption asset exceeds YouTube's 100MB limit")
        if key == "thumbnail" and (
            asset.byte_size > 2 * 1024 * 1024
            or asset.mime_type not in {"image/jpeg", "image/png", "application/octet-stream"}
        ):
            raise HTTPException(status_code=422, detail="thumbnail must be JPEG/PNG and no larger than 2MB")
        document[key]["content_hash"] = asset.content_hash
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"publish-metadata:{render.id}"})
    number = (await session.scalar(select(func.max(PublishMetadataVersionModel.version_number)).where(PublishMetadataVersionModel.render_id == render.id)) or 0) + 1
    now = datetime.now(timezone.utc)
    item = PublishMetadataVersionModel(
        id=uuid4(), render_id=render.id, version_number=number, document=document,
        content_hash=canonical_hash(document), created_by=actor.id, created_at=now, comment=payload.comment,
    )
    session.add(item)
    await append_audit(session, action="publishing.metadata_version_created", actor_id=actor.id, target_type="publish_metadata_version", target_id=str(item.id), correlation_id=request.state.correlation_id, context={"render_id": str(render.id), "render_hash": render.content_hash, "metadata_hash": item.content_hash, "version": number})
    await _commit(session, "This exact metadata version already exists")
    return _metadata_view(item)


@router.get("/metadata", response_model=list[PublishMetadataView])
async def list_metadata(_: Viewer, session: Annotated[AsyncSession, Depends(get_session)]):
    statement = (
        select(PublishMetadataVersionModel)
        .join(ProductionRenderModel, ProductionRenderModel.id == PublishMetadataVersionModel.render_id)
        .join(MediaProductionModel, MediaProductionModel.id == ProductionRenderModel.production_id)
        .join(StoryboardVersionModel, StoryboardVersionModel.id == MediaProductionModel.storyboard_version_id)
        .join(StoryboardModel, StoryboardModel.id == StoryboardVersionModel.storyboard_id)
        .join(ScriptModel, ScriptModel.id == StoryboardModel.script_id)
        .join(OpportunityModel, OpportunityModel.id == ScriptModel.opportunity_id)
        .where(
            StoryboardModel.deleted_at.is_(None),
            ScriptModel.deleted_at.is_(None),
            OpportunityModel.deleted_at.is_(None),
        )
        .order_by(PublishMetadataVersionModel.created_at.desc())
        .limit(100)
    )
    return [_metadata_view(item) for item in await session.scalars(statement)]


@router.post("/approvals", response_model=PublicationApprovalView)
async def approve_release(
    payload: PublicationApprovalWrite, request: Request, actor: ReleaseAuthorizer,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    render, metadata, _ = await _binding(session, payload.render_id, payload.expected_render_hash, payload.metadata_version_id, payload.expected_metadata_hash)
    if not await _exact_render_is_approved(session, render):
        raise HTTPException(status_code=409, detail="The bound render does not have an effective exact-hash final approval")
    release_evidence = await _require_current_publication_evidence(session, render) if payload.purpose == "public_release" and payload.decision == "approved" else {}
    item = PublicationApprovalModel(
        id=uuid4(), purpose=payload.purpose, render_id=render.id, render_hash=render.content_hash,
        metadata_version_id=metadata.id, metadata_hash=metadata.content_hash, decision=payload.decision,
        comment=payload.comment, actor_id=actor.id, correlation_id=request.state.correlation_id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(item)
    await append_audit(session, action=f"publishing.{payload.purpose}_{payload.decision}", actor_id=actor.id, target_type="publication_approval", target_id=str(item.id), correlation_id=request.state.correlation_id, context={"render_hash": render.content_hash, "metadata_hash": metadata.content_hash, **release_evidence})
    continuation: AutomaticContinuation | None = None
    if payload.decision == "approved" and payload.purpose == "private_upload":
        connection = await session.get(YouTubeConnectionModel, payload.connection_id) if payload.connection_id else None
        if connection is None:
            channel_id = await _render_channel_id(session, render)
            connections = list(
                await session.scalars(
                    select(YouTubeConnectionModel).where(
                        YouTubeConnectionModel.channel_profile_id == channel_id,
                        YouTubeConnectionModel.enabled.is_(True),
                        YouTubeConnectionModel.status == "active",
                    )
                )
            )
            connection = connections[0] if len(connections) == 1 else None
        if connection is None:
            continuation = AutomaticContinuation(
                state="awaiting_input",
                action="private_upload",
                message="Private-upload approval is recorded, but exactly one active channel connection must be selected.",
            )
        else:
            selected_mode = payload.mode or PublishMode.DRY_RUN
            publication = await start_upload(
                PublicationStart(
                    connection_id=connection.id,
                    render_id=render.id,
                    expected_render_hash=render.content_hash,
                    metadata_version_id=metadata.id,
                    expected_metadata_hash=metadata.content_hash,
                    mode=selected_mode,
                    idempotency_key=f"approval-{item.id.hex}",
                ),
                request,
                actor,
                session,
            )
            continuation = AutomaticContinuation(
                state="started",
                action="private_upload",
                workflow_id=publication.workflow_id,
                message=f"{selected_mode.value.replace('_', '-').title()} private upload started automatically.",
            )
    elif payload.decision == "approved" and payload.purpose == "public_release":
        publication = await session.scalar(
            select(PublicationModel)
            .where(
                PublicationModel.render_id == render.id,
                PublicationModel.render_hash == render.content_hash,
                PublicationModel.metadata_version_id == metadata.id,
                PublicationModel.metadata_hash == metadata.content_hash,
                PublicationModel.mode == "real",
                PublicationModel.youtube_video_id.is_not(None),
            )
            .order_by(PublicationModel.created_at.desc())
            .limit(1)
        )
        if publication is None or payload.publish_at is None:
            continuation = AutomaticContinuation(
                state="awaiting_input",
                action="publication_schedule",
                message=(
                    "Public-release approval is recorded, but a completed real private upload and future publication time are required."
                ),
            )
        else:
            scheduled = await schedule_publication(
                publication.id,
                PublicationScheduleWrite(publish_at=payload.publish_at),
                request,
                actor,
                session,
            )
            continuation = AutomaticContinuation(
                state="started",
                action="publication_schedule",
                workflow_id=str(scheduled["workflow_id"]),
                message="The approved public release was scheduled automatically for the selected time.",
            )
    if continuation:
        await append_audit(
            session,
            action=f"automation.publication_{continuation.state}",
            actor_id=actor.id,
            target_type="publication_approval",
            target_id=str(item.id),
            correlation_id=request.state.correlation_id,
            context=continuation.model_dump(mode="json"),
        )
    await session.commit()
    view = PublicationApprovalView.model_validate(item, from_attributes=True)
    return view.model_copy(update={"automatic_continuation": continuation})


@router.post("/uploads", response_model=PublicationView, status_code=202)
async def start_upload(
    payload: PublicationStart, request: Request, actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await _require_publisher_gateway()
    render, metadata, binding = await _binding(session, payload.render_id, payload.expected_render_hash, payload.metadata_version_id, payload.expected_metadata_hash)
    if not await _exact_render_is_approved(session, render):
        raise HTTPException(status_code=409, detail="The exact render is not approved")
    approval = await _approval(session, binding, "private_upload")
    if approval is None:
        raise HTTPException(status_code=409, detail="Private upload requires a separate exact-version approval")
    connection = await session.get(YouTubeConnectionModel, payload.connection_id)
    if connection is None or not connection.enabled or connection.status != "active":
        raise HTTPException(status_code=409, detail="An active YouTube channel connection is required")
    await _require_matching_channel_lineage(session, render, connection)
    config = await _active_config(session)
    if config is None:
        raise HTTPException(status_code=409, detail="An admin must activate publishing configuration")
    if payload.mode is PublishMode.REAL:
        if not config.real_uploads_enabled:
            raise HTTPException(status_code=403, detail="Real uploads are disabled by active admin configuration")
        if connection.token_fingerprint == secret_fingerprint("mock-refresh-token"):
            raise HTTPException(status_code=409, detail="A mock connection cannot perform a real upload")
    provider_key = upload_idempotency_key(binding, str(connection.id), payload.mode)
    existing = await session.scalar(select(PublicationModel).where(PublicationModel.upload_idempotency_key == provider_key))
    if existing is not None:
        return _publication_view(existing)
    workflow_id, now = f"youtube-private-upload-{provider_key}", datetime.now(timezone.utc)
    item = PublicationModel(
        id=uuid4(), workflow_id=workflow_id, connection_id=connection.id, render_id=render.id,
        render_hash=render.content_hash, metadata_version_id=metadata.id, metadata_hash=metadata.content_hash,
        upload_approval_id=approval.id, configuration_version_id=config.id, mode=payload.mode.value,
        state="queued", upload_idempotency_key=provider_key, youtube_video_id=None,
        resumable_session_encrypted=None, uploaded_bytes=0, processing_status={}, caption_status={},
        thumbnail_status={}, failure=None, started_by=actor.id, correlation_id=request.state.correlation_id,
        version=1, created_at=now, updated_at=now,
    )
    session.add(item)
    session.add(WorkflowControlRecordModel(
        workflow_id=workflow_id, workflow_type="youtube-private-upload",
        request_payload={"publication_id": str(item.id), "workflow_id": workflow_id},
        parent_workflow_id=None, correlation_id=request.state.correlation_id, started_by=actor.id, created_at=now,
    ))
    await append_audit(session, action="publishing.private_upload_queued", actor_id=actor.id, target_type="publication", target_id=str(item.id), correlation_id=request.state.correlation_id, context={"mode": payload.mode.value, "render_hash": render.content_hash, "metadata_hash": metadata.content_hash, "upload_idempotency_key": provider_key, "client_idempotency_key": payload.idempotency_key})
    await _commit(session, "This exact upload is already queued")
    try:
        await TemporalPublishingGateway(request.app.state.temporal_client, get_settings()).start("youtube-private-upload", {"publication_id": str(item.id), "workflow_id": workflow_id})
    except Exception as exc:
        item.state, item.failure = "failed", {"code": "workflow_start_failed", "type": type(exc).__name__}
        item.updated_at, item.version = datetime.now(timezone.utc), item.version + 1
        await session.commit()
        raise HTTPException(status_code=503, detail="Publishing workflow could not be started") from exc
    return _publication_view(item)


@router.get("/uploads", response_model=list[PublicationView])
async def list_uploads(_: Viewer, session: Annotated[AsyncSession, Depends(get_session)]):
    statement = (
        select(PublicationModel)
        .join(ProductionRenderModel, ProductionRenderModel.id == PublicationModel.render_id)
        .join(MediaProductionModel, MediaProductionModel.id == ProductionRenderModel.production_id)
        .join(StoryboardVersionModel, StoryboardVersionModel.id == MediaProductionModel.storyboard_version_id)
        .join(StoryboardModel, StoryboardModel.id == StoryboardVersionModel.storyboard_id)
        .join(ScriptModel, ScriptModel.id == StoryboardModel.script_id)
        .join(OpportunityModel, OpportunityModel.id == ScriptModel.opportunity_id)
        .where(
            StoryboardModel.deleted_at.is_(None),
            ScriptModel.deleted_at.is_(None),
            OpportunityModel.deleted_at.is_(None),
        )
        .order_by(PublicationModel.created_at.desc())
        .limit(100)
    )
    return [_publication_view(item) for item in await session.scalars(statement)]


@router.get("/uploads/{publication_id}", response_model=PublicationView)
async def get_upload(publication_id: UUID, _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]):
    item = await session.get(PublicationModel, publication_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    return _publication_view(item)


@router.post("/uploads/{publication_id}/reconcile", status_code=202)
async def reconcile_upload(publication_id: UUID, request: Request, _: Operator, session: Annotated[AsyncSession, Depends(get_session)]):
    await _require_publisher_gateway()
    item = await session.get(PublicationModel, publication_id)
    if item is None or item.youtube_video_id is None:
        raise HTTPException(status_code=409, detail="Publication has no uploaded video to reconcile")
    workflow_id = f"youtube-reconcile-{item.id}-v{item.version}"
    await TemporalPublishingGateway(request.app.state.temporal_client, get_settings()).start("youtube-reconcile", {"publication_id": str(item.id), "workflow_id": workflow_id})
    return {"workflow_id": workflow_id, "state": "queued"}


@router.post("/uploads/{publication_id}/schedule", status_code=202)
async def schedule_publication(
    publication_id: UUID, payload: PublicationScheduleWrite, request: Request, actor: ReleaseAuthorizer,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await _require_publisher_gateway()
    item = await session.get(PublicationModel, publication_id)
    if item is None or item.mode != "real" or not item.youtube_video_id or item.state not in {"uploaded_private", "processing", "processed"}:
        raise HTTPException(status_code=409, detail="Only a real, successfully private-uploaded video can be scheduled")
    binding = ReleaseBinding(str(item.render_id), item.render_hash, str(item.metadata_version_id), item.metadata_hash)
    approval = await _approval(session, binding, "public_release")
    if approval is None:
        raise HTTPException(status_code=409, detail="Scheduling requires a separate exact-version public-release approval")
    render = await session.get(ProductionRenderModel, item.render_id)
    if render is None:
        raise HTTPException(status_code=409, detail="The approved render is unavailable")
    await _require_current_publication_evidence(session, render)
    try:
        publish_at = require_future_schedule(payload.publish_at)
    except PublishingContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    key = schedule_idempotency_key(binding, item.youtube_video_id, publish_at)
    existing = await session.scalar(select(PublicationScheduleModel).where(PublicationScheduleModel.schedule_idempotency_key == key))
    if existing is not None:
        return {"id": str(existing.id), "workflow_id": f"youtube-schedule-{key}", "state": existing.state}
    workflow_id = f"youtube-schedule-{key}"
    await append_audit(session, action="publishing.schedule_queued", actor_id=actor.id, target_type="publication", target_id=str(item.id), correlation_id=request.state.correlation_id, context={"publish_at": publish_at.isoformat(), "schedule_idempotency_key": key, "approval_id": str(approval.id)})
    await session.commit()
    await TemporalPublishingGateway(request.app.state.temporal_client, get_settings()).start("youtube-schedule", {
        "publication_id": str(item.id), "approval_id": str(approval.id), "publish_at": publish_at.isoformat(),
        "schedule_idempotency_key": key, "workflow_id": workflow_id, "actor_id": str(actor.id),
        "correlation_id": request.state.correlation_id,
    })
    return {"workflow_id": workflow_id, "state": "queued"}
