from __future__ import annotations

import asyncio
import hashlib
import io
import json
from datetime import datetime, timezone
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import aiohttp
import asyncpg
from minio import Minio
from temporalio import activity
from temporalio.exceptions import ApplicationError

from editorial_core.research import chunk_semantic_text
from research_worker.acquisition import (
    DomainPolicy,
    DomainThrottle,
    RobotsDenied,
    acquire_public_source,
)
from research_worker.config import Settings
from research_worker.extraction import extract_source_document
from research_worker.firecrawl import render_public_html


_settings = Settings()
_domain_throttle = DomainThrottle(_settings.acquisition_min_domain_interval_seconds)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _vector_literal(values: tuple[float, ...]) -> str:
    return "[" + ",".join(f"{value:.10f}" for value in values) + "]"


async def _put_object(
    client: Minio, bucket: str, key: str, body: bytes, content_type: str
) -> None:
    await asyncio.to_thread(
        client.put_object,
        bucket,
        key,
        io.BytesIO(body),
        len(body),
        content_type=content_type,
    )


@activity.defn(name="load-approved-opportunity-sources")
async def load_approved_opportunity_sources(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        opportunity = await connection.fetchrow(
            """SELECT o.id, o.subject_profile_id, o.decision, s.domain_policy
               FROM opportunities o
               JOIN subject_profiles s ON s.id=o.subject_profile_id
               WHERE o.id=$1 AND o.deleted_at IS NULL AND s.deleted_at IS NULL""",
            UUID(str(request["opportunity_id"])),
        )
        if opportunity is None:
            raise ApplicationError("opportunity does not exist", non_retryable=True)
        if opportunity["decision"] != "approved":
            raise ApplicationError(
                "opportunity must be approved before source acquisition", non_retryable=True
            )
        rows = await connection.fetch(
            """SELECT os.source_document_id, sd.canonical_url, sd.title
               FROM opportunity_sources os
               JOIN source_documents sd ON sd.id=os.source_document_id
               WHERE os.opportunity_id=$1
               ORDER BY os.result_rank, sd.canonical_url
               LIMIT 20""",
            opportunity["id"],
        )
        if not rows:
            raise ApplicationError(
                "approved opportunity has no linked source candidates", non_retryable=True
            )
        return {
            "opportunity_id": str(opportunity["id"]),
            "subject_profile_id": str(opportunity["subject_profile_id"]),
            "domain_policy": _json(opportunity["domain_policy"]) or {"allow": [], "block": []},
            "sources": [
                {
                    "source_document_id": str(row["source_document_id"]),
                    "url": row["canonical_url"],
                    "title": row["title"],
                }
                for row in rows
            ],
        }
    finally:
        await connection.close()


@activity.defn(name="acquire-public-source")
async def acquire_source_candidate(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    source_document_id = UUID(str(request["source_document_id"]))
    idempotency_key = f"{request['workflow_id']}:{source_document_id}"
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        existing = await connection.fetchval(
            """SELECT result FROM idempotency_records
               WHERE scope='research.source_acquisition' AND idempotency_key=$1
                 AND status='succeeded'""",
            idempotency_key,
        )
        if existing:
            return _json(existing)
    finally:
        await connection.close()

    policy = DomainPolicy.from_mapping(request.get("domain_policy"))
    try:
        acquired = await acquire_public_source(
            str(request["url"]),
            policy=policy,
            throttle=_domain_throttle,
            user_agent=settings.acquisition_user_agent,
            maximum_bytes=settings.acquisition_max_bytes,
        )
    except (RobotsDenied, ValueError) as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    except aiohttp.ClientError as exc:
        raise ApplicationError(f"source request failed: {type(exc).__name__}") from exc

    try:
        extracted = await asyncio.to_thread(
            extract_source_document,
            acquired.body,
            acquired.content_type,
            acquired.final_url,
        )
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    rendered_body: bytes | None = None
    browser_fallback_attempted = False
    browser_fallback_error: str | None = None
    if (
        acquired.content_type in {"text/html", "application/xhtml+xml"}
        and len(extracted.text) < 200
        and settings.firecrawl_endpoint
    ):
        browser_fallback_attempted = True
        try:
            rendered = await render_public_html(
                settings.firecrawl_endpoint,
                url=acquired.final_url,
                policy=policy,
                user_agent=settings.acquisition_user_agent,
                maximum_html_bytes=settings.acquisition_max_bytes,
            )
            rendered_extraction = await asyncio.to_thread(
                extract_source_document,
                rendered.body,
                "text/html",
                rendered.final_url,
            )
            if len(rendered_extraction.text) > len(extracted.text):
                rendered_body = rendered.body
                extracted = replace(
                    rendered_extraction,
                    extractor=f"firecrawl-rendered+{rendered_extraction.extractor}",
                    metadata={
                        **rendered_extraction.metadata,
                        "render_status_code": rendered.status_code,
                    },
                )
        except (aiohttp.ClientError, ValueError, asyncio.TimeoutError) as exc:
            # The direct immutable source remains useful and reviewable. A best-effort
            # browser failure is recorded rather than turning an approved HTTP source
            # into an endlessly retrying activity.
            browser_fallback_error = f"{type(exc).__name__}: {str(exc)[:300]}"

    normalized = extracted.text.encode("utf-8")
    semantic_chunks = chunk_semantic_text(extracted.text)
    raw_hash = hashlib.sha256(acquired.body).hexdigest()
    rendered_hash = hashlib.sha256(rendered_body).hexdigest() if rendered_body else None
    if rendered_body:
        content_hash = hashlib.sha256(
            b"raw\x00" + acquired.body + b"rendered\x00" + rendered_body
        ).hexdigest()
    else:
        content_hash = raw_hash
    base_key = f"research/snapshots/{content_hash}"
    raw_key = f"{base_key}/raw"
    normalized_key = f"{base_key}/normalized.txt"
    rendered_key = f"{base_key}/rendered.html" if rendered_body else None
    minio = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    await _put_object(
        minio, settings.minio_bucket, raw_key, acquired.body, acquired.content_type
    )
    await _put_object(
        minio, settings.minio_bucket, normalized_key, normalized, "text/plain; charset=utf-8"
    )
    if rendered_body and rendered_key:
        await _put_object(
            minio, settings.minio_bucket, rendered_key, rendered_body, "text/html; charset=utf-8"
        )

    now = datetime.now(timezone.utc)
    request_hash = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"source:{source_document_id}"
            )
            existing = await connection.fetchval(
                """SELECT result FROM idempotency_records
                   WHERE scope='research.source_acquisition' AND idempotency_key=$1
                     AND status='succeeded'""",
                idempotency_key,
            )
            if existing:
                return _json(existing)
            snapshot_id = await connection.fetchval(
                """SELECT id FROM source_snapshots
                   WHERE source_document_id=$1 AND content_hash=$2""",
                source_document_id,
                content_hash,
            )
            if snapshot_id is None:
                snapshot_number = await connection.fetchval(
                    """SELECT COALESCE(MAX(snapshot_number),0)+1
                       FROM source_snapshots WHERE source_document_id=$1""",
                    source_document_id,
                )
                snapshot_id = uuid4()
                await connection.execute(
                    """INSERT INTO source_snapshots
                    (id,source_document_id,snapshot_number,raw_object_key,normalized_object_key,
                     screenshot_object_key,content_hash,mime_type,byte_size,retrieved_at,
                     redirect_chain,extraction_metadata,injection_markers)
                    VALUES($1,$2,$3,$4,$5,NULL,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12::jsonb)""",
                    snapshot_id,
                    source_document_id,
                    snapshot_number,
                    raw_key,
                    normalized_key,
                    content_hash,
                    acquired.content_type,
                    len(acquired.body),
                    now,
                    json.dumps(acquired.redirect_chain),
                    json.dumps(
                        {
                            "extractor": extracted.extractor,
                            "active_elements_removed": extracted.removed_active_elements,
                            "document_metadata": extracted.metadata,
                            "raw_content_hash": raw_hash,
                            "rendered_content_hash": rendered_hash,
                            "rendered_object_key": rendered_key,
                            "browser_fallback_attempted": browser_fallback_attempted,
                            "browser_fallback_used": rendered_body is not None,
                            "browser_fallback_error": browser_fallback_error,
                            "robots_checked": True,
                            "robots_cache_hit": acquired.robots_cache_hit,
                            "dns_connection_pinned": True,
                            "tls_verified": acquired.final_url.startswith("https://"),
                        }
                    ),
                    json.dumps(extracted.injection_markers),
                )
                metadata = extracted.metadata
                publication_date = metadata.get("publication_date")
                await connection.execute(
                    """UPDATE source_documents
                       SET title=COALESCE(title,$2), author=COALESCE(author,$3),
                           publisher=COALESCE(publisher,$4),
                           publication_at=COALESCE(publication_at,
                             CASE WHEN $5::text ~ '^\\d{4}-\\d{2}-\\d{2}$'
                                  THEN ($5::text || 'T00:00:00Z')::timestamptz
                                  ELSE NULL END),
                           updated_at=$6, version=version+1
                       WHERE id=$1""",
                    source_document_id,
                    metadata.get("title"),
                    metadata.get("author"),
                    metadata.get("publisher"),
                    publication_date,
                    now,
                )
            for chunk_number, chunk in enumerate(semantic_chunks, start=1):
                await connection.execute(
                    """INSERT INTO semantic_chunks
                    (id,source_snapshot_id,chunk_number,text,location_anchor,start_offset,
                     end_offset,chunk_hash,token_count,embedding_model,embedding,created_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'feature-hash-v1',$10::vector,$11)
                    ON CONFLICT (source_snapshot_id,chunk_number) DO NOTHING""",
                    uuid4(),
                    snapshot_id,
                    chunk_number,
                    chunk.text,
                    chunk.location_anchor,
                    chunk.start_offset,
                    chunk.end_offset,
                    chunk.chunk_hash,
                    chunk.token_count,
                    _vector_literal(chunk.embedding),
                    now,
                )
            result = {
                "source_document_id": str(source_document_id),
                "source_snapshot_id": str(snapshot_id),
                "content_hash": content_hash,
                "content_type": acquired.content_type,
                "byte_size": len(acquired.body),
                "final_url": acquired.final_url,
                "injection_markers": list(extracted.injection_markers),
                "semantic_chunk_count": len(semantic_chunks),
                "extractor": extracted.extractor,
                "browser_fallback_used": rendered_body is not None,
            }
            await connection.execute(
                """INSERT INTO idempotency_records
                (id,scope,idempotency_key,request_hash,status,external_id,result,created_at,updated_at)
                VALUES($1,'research.source_acquisition',$2,$3,'succeeded',$4,$5::jsonb,$6,$6)""",
                uuid4(),
                idempotency_key,
                request_hash,
                str(snapshot_id),
                json.dumps(result),
                now,
            )
        return result
    finally:
        await connection.close()


@activity.defn(name="complete-source-acquisition")
async def complete_source_acquisition(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    now = datetime.now(timezone.utc)
    opportunity_id = UUID(str(request["opportunity_id"]))
    actor_id = UUID(str(request["actor_id"]))
    workflow_id = str(request["workflow_id"])
    snapshots = request.get("snapshots", [])
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"acquisition:{workflow_id}"
            )
            existing = await connection.fetchrow(
                "SELECT id, progress FROM research_runs WHERE workflow_id=$1", workflow_id
            )
            if existing:
                return {
                    "research_run_id": str(existing["id"]),
                    "snapshot_count": len(_json(existing["progress"]).get("snapshot_ids", [])),
                }
            decision = await connection.fetchval(
                "SELECT decision FROM opportunities WHERE id=$1 AND deleted_at IS NULL",
                opportunity_id,
            )
            if decision != "approved":
                raise ApplicationError(
                    "opportunity approval was withdrawn before acquisition completed",
                    non_retryable=True,
                )
            research_run_id = uuid4()
            await connection.execute(
                """INSERT INTO research_runs
                (id,version,created_at,updated_at,deleted_at,opportunity_id,workflow_id,state,
                 research_plan,progress,correlation_id,started_by,completed_at)
                VALUES($1,1,$2,$2,NULL,$3,$4,'RESEARCHING',$5::jsonb,$6::jsonb,$7,$8,NULL)""",
                research_run_id,
                now,
                opportunity_id,
                workflow_id,
                json.dumps(
                    {
                        "phase": "evidence extraction",
                        "source_snapshot_ids": [item["source_snapshot_id"] for item in snapshots],
                    }
                ),
                json.dumps(
                    {
                        "percent": 25,
                        "sources_acquired": len(snapshots),
                        "snapshot_ids": [item["source_snapshot_id"] for item in snapshots],
                    }
                ),
                str(request["correlation_id"]),
                actor_id,
            )
            await connection.execute(
                """INSERT INTO workflow_transitions
                (id,aggregate_type,aggregate_id,from_stage,to_stage,reason,actor_id,
                 correlation_id,occurred_at)
                VALUES($1,'opportunity',$2,'SHORTLISTED','RESEARCHING',
                       'approved sources acquired into immutable snapshots',$3,$4,$5)""",
                uuid4(),
                opportunity_id,
                actor_id,
                str(request["correlation_id"]),
                now,
            )
        return {"research_run_id": str(research_run_id), "snapshot_count": len(snapshots)}
    finally:
        await connection.close()
