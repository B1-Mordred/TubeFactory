from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from editorial_core.authorization import Permission
from youtuber_api.audit import append_audit
from youtuber_api.db import get_session
from youtuber_api.models import ConfigurationHeadModel, ConfigurationVersionModel, UserModel
from youtuber_api.schemas import ConfigurationImport, ConfigurationView, ConfigurationWrite
from youtuber_api.security import require

router = APIRouter(prefix="/configuration", tags=["configuration"])
AdminPolicyUser = Annotated[UserModel, Depends(require(Permission.MANAGE_POLICIES))]


def _hash_document(document: dict[str, Any]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _view(model: ConfigurationVersionModel, active_id=None) -> ConfigurationView:
    return ConfigurationView(
        id=model.id,
        namespace=model.namespace,
        version=model.version,
        schema_version=model.schema_version,
        document=model.document,
        document_hash=model.document_hash,
        created_by=model.created_by,
        created_at=model.created_at,
        comment=model.comment,
        active=active_id == model.id,
    )


async def _write_configuration(
    payload: ConfigurationWrite,
    request: Request,
    actor: UserModel,
    session: AsyncSession,
) -> ConfigurationView:
    # Namespace-scoped serialization preserves monotonic versions.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:namespace))"), {"namespace": payload.namespace}
    )
    document_hash = _hash_document(payload.document)
    existing = await session.scalar(
        select(ConfigurationVersionModel).where(
            ConfigurationVersionModel.namespace == payload.namespace,
            ConfigurationVersionModel.document_hash == document_hash,
        )
    )
    now = datetime.now(timezone.utc)
    if existing is None:
        current_max = await session.scalar(
            select(func.max(ConfigurationVersionModel.version)).where(
                ConfigurationVersionModel.namespace == payload.namespace
            )
        )
        existing = ConfigurationVersionModel(
            namespace=payload.namespace,
            version=(current_max or 0) + 1,
            schema_version=payload.schema_version,
            document=payload.document,
            document_hash=document_hash,
            created_by=actor.id,
            created_at=now,
            comment=payload.comment,
        )
        session.add(existing)
        await session.flush()
    head = await session.get(ConfigurationHeadModel, payload.namespace, with_for_update=True)
    if head is None:
        head = ConfigurationHeadModel(
            namespace=payload.namespace, active_version_id=existing.id, updated_at=now
        )
        session.add(head)
    else:
        head.active_version_id = existing.id
        head.updated_at = now
    await append_audit(
        session,
        action="configuration.activated",
        actor_id=actor.id,
        target_type="configuration_version",
        target_id=str(existing.id),
        correlation_id=request.state.correlation_id,
        context={
            "namespace": payload.namespace,
            "version": existing.version,
            "document_hash": document_hash,
        },
    )
    await session.commit()
    return _view(existing, existing.id)


@router.get("", response_model=list[ConfigurationView])
async def list_active_configuration(
    _: Annotated[UserModel, Depends(require(Permission.VIEW))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ConfigurationView]:
    heads = list(await session.scalars(select(ConfigurationHeadModel).order_by(ConfigurationHeadModel.namespace)))
    return [_view(head.active_version, head.active_version_id) for head in heads]


@router.get("/{namespace}/versions", response_model=list[ConfigurationView])
async def list_configuration_versions(
    namespace: str,
    _: Annotated[UserModel, Depends(require(Permission.VIEW))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ConfigurationView]:
    head = await session.get(ConfigurationHeadModel, namespace)
    versions = list(
        await session.scalars(
            select(ConfigurationVersionModel)
            .where(ConfigurationVersionModel.namespace == namespace)
            .order_by(ConfigurationVersionModel.version.desc())
        )
    )
    return [_view(version, head.active_version_id if head else None) for version in versions]


@router.put("", response_model=ConfigurationView)
async def write_configuration(
    payload: ConfigurationWrite,
    request: Request,
    actor: AdminPolicyUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConfigurationView:
    return await _write_configuration(payload, request, actor, session)


@router.post("/import", response_model=ConfigurationView)
async def import_configuration(
    payload: ConfigurationImport,
    request: Request,
    actor: AdminPolicyUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConfigurationView:
    try:
        document = json.loads(payload.content) if payload.format == "json" else yaml.safe_load(payload.content)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid {payload.format} document") from exc
    if not isinstance(document, dict):
        raise HTTPException(status_code=422, detail="Configuration root must be an object")
    return await _write_configuration(
        ConfigurationWrite(
            namespace=payload.namespace,
            schema_version=payload.schema_version,
            document=document,
            comment=payload.comment,
        ),
        request,
        actor,
        session,
    )


@router.get("/{namespace}/export")
async def export_configuration(
    namespace: str,
    _: Annotated[UserModel, Depends(require(Permission.VIEW))],
    session: Annotated[AsyncSession, Depends(get_session)],
    format: str = Query(default="yaml", pattern="^(yaml|json)$"),
) -> Response:
    head = await session.get(ConfigurationHeadModel, namespace)
    if head is None:
        raise HTTPException(status_code=404, detail="Configuration namespace not found")
    envelope = {
        "schema_version": head.active_version.schema_version,
        "namespace": namespace,
        "version": head.active_version.version,
        "document": head.active_version.document,
    }
    if format == "json":
        body = json.dumps(envelope, indent=2, ensure_ascii=False) + "\n"
        return Response(body, media_type="application/json")
    body = yaml.safe_dump(envelope, sort_keys=False, allow_unicode=True)
    return Response(body, media_type="application/yaml")
