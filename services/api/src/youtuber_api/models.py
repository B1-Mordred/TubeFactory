from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Float, ForeignKey, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from editorial_core.authorization import Role
from youtuber_api.db import Base


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class UserRole(str, enum.Enum):
    ADMIN = Role.ADMIN.value
    OPERATOR = Role.OPERATOR.value
    EDITOR = Role.EDITOR.value
    REVIEWER = Role.REVIEWER.value
    VIEWER = Role.VIEWER.value


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    display_name: Mapped[str] = mapped_column(String(160))
    password_hash: Mapped[str | None] = mapped_column(Text)
    identity_provider: Mapped[str] = mapped_column(String(20), default="local")
    oidc_subject: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", values_callable=lambda values: [v.value for v in values])
    )
    totp_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_recovery_hashes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthenticationRateLimitModel(Base):
    __tablename__ = "authentication_rate_limits"

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OIDCConfigurationVersionModel(Base):
    __tablename__ = "oidc_configuration_versions"
    __table_args__ = (UniqueConstraint("content_hash", name="uq_oidc_configuration_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    version_number: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean)
    issuer: Mapped[str] = mapped_column(String(500))
    client_id: Mapped[str] = mapped_column(String(500))
    client_secret_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    authorization_endpoint: Mapped[str] = mapped_column(String(1000))
    token_endpoint: Mapped[str] = mapped_column(String(1000))
    jwks_uri: Mapped[str] = mapped_column(String(1000))
    scopes: Mapped[list[str]] = mapped_column(JSONB)
    username_claim: Mapped[str] = mapped_column(String(120))
    display_name_claim: Mapped[str] = mapped_column(String(120))
    role_claim: Mapped[str] = mapped_column(String(120))
    role_mapping: Mapped[dict[str, str]] = mapped_column(JSONB)
    default_role: Mapped[str | None] = mapped_column(String(20))
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str] = mapped_column(Text)


class OIDCConfigurationHeadModel(Base):
    __tablename__ = "oidc_configuration_heads"

    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True)
    active_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("oidc_configuration_versions.id", ondelete="RESTRICT")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OIDCAuthenticationStateModel(Base):
    __tablename__ = "oidc_authentication_states"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_verifier_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    configuration_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("oidc_configuration_versions.id", ondelete="CASCADE")
    )
    redirect_uri: Mapped[str] = mapped_column(String(1000))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditEventModel(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(160))
    target_type: Mapped[str | None] = mapped_column(String(120))
    target_id: Mapped[str | None] = mapped_column(String(160))
    correlation_id: Mapped[str] = mapped_column(String(160), index=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)


class ConfigurationVersionModel(Base):
    __tablename__ = "configuration_versions"
    __table_args__ = (
        UniqueConstraint("namespace", "version", name="uq_config_namespace_version"),
        UniqueConstraint("namespace", "document_hash", name="uq_config_namespace_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    namespace: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[str] = mapped_column(String(40))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    document_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str] = mapped_column(Text, default="")


class ConfigurationHeadModel(Base):
    __tablename__ = "configuration_heads"

    namespace: Mapped[str] = mapped_column(String(120), primary_key=True)
    active_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("configuration_versions.id", ondelete="RESTRICT")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active_version: Mapped[ConfigurationVersionModel] = relationship(lazy="selectin")


class IdempotencyRecordModel(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("scope", "idempotency_key", name="uq_idempotency_scope_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    scope: Mapped[str] = mapped_column(String(160))
    idempotency_key: Mapped[str] = mapped_column(String(240))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(240))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkflowControlRecordModel(Base):
    """Immutable provenance needed to control or retry a Temporal workflow safely."""

    __tablename__ = "workflow_control_records"

    workflow_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    workflow_type: Mapped[str] = mapped_column(String(120))
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parent_workflow_id: Mapped[str | None] = mapped_column(String(240), index=True)
    correlation_id: Mapped[str] = mapped_column(String(160), index=True)
    started_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VersionedModelMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChannelProfileModel(VersionedModelMixin, Base):
    __tablename__ = "channel_profiles"

    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(180))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    identity: Mapped[dict[str, Any]] = mapped_column(JSONB)
    languages: Mapped[list[str]] = mapped_column(JSONB)
    audience: Mapped[dict[str, Any]] = mapped_column(JSONB)
    editorial_rules: Mapped[dict[str, Any]] = mapped_column(JSONB)
    brand_kit: Mapped[dict[str, Any]] = mapped_column(JSONB)
    default_render_settings: Mapped[dict[str, Any]] = mapped_column(JSONB)
    default_publish_settings: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class SubjectProfileModel(VersionedModelMixin, Base):
    __tablename__ = "subject_profiles"

    channel_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("channel_profiles.id"))
    name: Mapped[str] = mapped_column(String(180))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    topic: Mapped[str] = mapped_column(Text)
    research_goal: Mapped[str] = mapped_column(Text)
    excluded_angles: Mapped[list[str]] = mapped_column(JSONB)
    seed_queries: Mapped[list[str]] = mapped_column(JSONB)
    related_concepts: Mapped[list[str]] = mapped_column(JSONB)
    negative_keywords: Mapped[list[str]] = mapped_column(JSONB)
    languages: Mapped[list[str]] = mapped_column(JSONB)
    regions: Mapped[list[str]] = mapped_column(JSONB)
    domain_policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_requirements: Mapped[dict[str, Any]] = mapped_column(JSONB)
    schedule: Mapped[dict[str, Any]] = mapped_column(JSONB)
    freshness_policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    format_policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    editorial_profile: Mapped[dict[str, Any]] = mapped_column(JSONB)
    risk: Mapped[str] = mapped_column(String(20))
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB)
    opportunity_weights: Mapped[dict[str, Any]] = mapped_column(JSONB)
    approval_profile: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class OpportunityModel(VersionedModelMixin, Base):
    __tablename__ = "opportunities"

    subject_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("subject_profiles.id"))
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text)
    editorial_rationale: Mapped[str] = mapped_column(Text, default="")
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    decision: Mapped[str] = mapped_column(String(20), default="pending")
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    grouping_reason: Mapped[list[str]] = mapped_column(JSONB)
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("opportunities.id"))
    estimated_cost: Mapped[dict[str, Any]] = mapped_column(JSONB)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)


class OpportunityScoreModel(Base):
    __tablename__ = "opportunity_scores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("opportunities.id"))
    score_version: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    positive_components: Mapped[dict[str, float]] = mapped_column(JSONB)
    penalties: Mapped[dict[str, float]] = mapped_column(JSONB)
    weights: Mapped[dict[str, float]] = mapped_column(JSONB)
    reasoning: Mapped[list[str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OpportunityAIQualificationModel(Base):
    __tablename__ = "opportunity_ai_qualifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("opportunities.id"))
    qualification_version: Mapped[int] = mapped_column(Integer)
    workflow_id: Mapped[str] = mapped_column(String(240))
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id"))
    prompt_template_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("prompt_templates.id"))
    rubric_version: Mapped[str] = mapped_column(String(80))
    dimensions: Mapped[dict[str, float]] = mapped_column(JSONB)
    confidence: Mapped[float] = mapped_column(Float)
    abstained: Mapped[bool] = mapped_column(Boolean)
    rationale: Mapped[list[str]] = mapped_column(JSONB)
    uncertainty: Mapped[list[str]] = mapped_column(JSONB)
    resulting_score_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchRunModel(VersionedModelMixin, Base):
    __tablename__ = "research_runs"

    opportunity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("opportunities.id"))
    workflow_id: Mapped[str] = mapped_column(String(240), unique=True)
    state: Mapped[str] = mapped_column(String(40))
    research_plan: Mapped[dict[str, Any]] = mapped_column(JSONB)
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB)
    correlation_id: Mapped[str] = mapped_column(String(160))
    started_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceDocumentModel(VersionedModelMixin, Base):
    __tablename__ = "source_documents"

    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(300))
    publisher: Mapped[str | None] = mapped_column(String(300))
    source_type: Mapped[str] = mapped_column(String(60))
    publication_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reputation: Mapped[dict[str, Any]] = mapped_column(JSONB)
    domain: Mapped[str] = mapped_column(String(253))


class SourceSnapshotModel(Base):
    __tablename__ = "source_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("source_documents.id"))
    snapshot_number: Mapped[int] = mapped_column(Integer)
    raw_object_key: Mapped[str] = mapped_column(String(1024))
    normalized_object_key: Mapped[str] = mapped_column(String(1024))
    screenshot_object_key: Mapped[str | None] = mapped_column(String(1024))
    content_hash: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(255))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    redirect_chain: Mapped[list[str]] = mapped_column(JSONB)
    extraction_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB)
    injection_markers: Mapped[list[str]] = mapped_column(JSONB)


class EvidenceExcerptModel(Base):
    __tablename__ = "evidence_excerpts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("source_snapshots.id"))
    exact_text: Mapped[str] = mapped_column(Text)
    prefix_text: Mapped[str] = mapped_column(Text, default="")
    suffix_text: Mapped[str] = mapped_column(Text, default="")
    location_anchor: Mapped[str] = mapped_column(String(500))
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    excerpt_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SemanticChunkModel(Base):
    __tablename__ = "semantic_chunks"
    __table_args__ = (
        UniqueConstraint("source_snapshot_id", "chunk_number", name="uq_semantic_chunk_number"),
        UniqueConstraint("source_snapshot_id", "chunk_hash", "location_anchor", name="uq_semantic_chunk_anchor"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_snapshots.id", ondelete="RESTRICT")
    )
    chunk_number: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    location_anchor: Mapped[str] = mapped_column(String(500))
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    chunk_hash: Mapped[str] = mapped_column(String(64))
    token_count: Mapped[int] = mapped_column(Integer)
    embedding_model: Mapped[str] = mapped_column(String(120))
    embedding: Mapped[list[float]] = mapped_column(Vector(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceRelationshipModel(Base):
    __tablename__ = "source_relationships"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("source_documents.id"))
    related_source_document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("source_documents.id"))
    relationship: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)
    confidence: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OpportunitySourceModel(Base):
    __tablename__ = "opportunity_sources"
    __table_args__ = (
        UniqueConstraint("opportunity_id", "source_document_id", name="uq_opportunity_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE")
    )
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_documents.id", ondelete="RESTRICT")
    )
    search_purpose: Mapped[str] = mapped_column(String(80))
    search_query: Mapped[str] = mapped_column(Text)
    result_rank: Mapped[int] = mapped_column(Integer)
    snippet: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchDossierModel(VersionedModelMixin, Base):
    __tablename__ = "research_dossiers"

    opportunity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("opportunities.id"))
    dossier_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    executive_summary: Mapped[str] = mapped_column(Text)
    chronology: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    unresolved_questions: Mapped[list[str]] = mapped_column(JSONB)
    alternative_explanations: Mapped[list[str]] = mapped_column(JSONB)
    source_quality_notes: Mapped[list[str]] = mapped_column(JSONB)
    safe_conclusions: Mapped[list[str]] = mapped_column(JSONB)
    prohibited_overstatements: Mapped[list[str]] = mapped_column(JSONB)
    proposed_angles: Mapped[list[str]] = mapped_column(JSONB)
    explanation_plan: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    completion_evaluation: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_comment: Mapped[str | None] = mapped_column(Text)


class ClaimModel(VersionedModelMixin, Base):
    __tablename__ = "claims"

    research_dossier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("research_dossiers.id"))
    normalized_statement: Mapped[str] = mapped_column(Text)
    claim_type: Mapped[str] = mapped_column(String(20))
    scope: Mapped[str] = mapped_column(Text)
    relevant_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entities: Mapped[list[str]] = mapped_column(JSONB)
    coverage_unit_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    confidence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    risk: Mapped[str] = mapped_column(String(20))
    central: Mapped[bool] = mapped_column(Boolean)
    review_comment: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClaimEvidenceModel(Base):
    __tablename__ = "claim_evidence"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("claims.id"))
    evidence_excerpt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("evidence_excerpts.id"))
    relationship: Mapped[str] = mapped_column(String(20))
    source_independent: Mapped[bool] = mapped_column(Boolean)
    direct_evidence: Mapped[bool] = mapped_column(Boolean)
    primary_source: Mapped[bool] = mapped_column(Boolean)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkflowTransitionModel(Base):
    __tablename__ = "workflow_transitions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    aggregate_type: Mapped[str] = mapped_column(String(80))
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    from_stage: Mapped[str | None] = mapped_column(String(50))
    to_stage: Mapped[str] = mapped_column(String(50))
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    correlation_id: Mapped[str] = mapped_column(String(160))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProviderModel(VersionedModelMixin, Base):
    __tablename__ = "providers"

    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(180))
    driver_type: Mapped[str] = mapped_column(String(40))
    endpoint: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    location: Mapped[str] = mapped_column(String(20))
    authentication_scheme: Mapped[str] = mapped_column(String(30))
    secret_reference: Mapped[str | None] = mapped_column(String(240))
    data_policy: Mapped[str] = mapped_column(String(40))
    residency_policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB)
    concurrency_limit: Mapped[int] = mapped_column(Integer)
    requests_per_minute: Mapped[int] = mapped_column(Integer)
    health_status: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class DossierAIAssessmentModel(Base):
    __tablename__ = "dossier_ai_assessments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    research_dossier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("research_dossiers.id"))
    assessment_version: Mapped[int] = mapped_column(Integer)
    workflow_id: Mapped[str] = mapped_column(String(240))
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id"))
    prompt_template_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("prompt_templates.id"))
    rubric_version: Mapped[str] = mapped_column(String(80))
    claim_assessments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    source_assessments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    methodological_limits: Mapped[list[str]] = mapped_column(JSONB)
    counterevidence_gaps: Mapped[list[str]] = mapped_column(JSONB)
    confidence: Mapped[float] = mapped_column(Float)
    abstained: Mapped[bool] = mapped_column(Boolean)
    uncertainty: Mapped[list[str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AIModelModel(VersionedModelMixin, Base):
    __tablename__ = "models"
    __table_args__ = (
        UniqueConstraint("provider_id", "model_name", name="uq_provider_model_name"),
    )

    provider_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("providers.id"))
    model_name: Mapped[str] = mapped_column(String(240))
    display_name: Mapped[str] = mapped_column(String(240))
    visible: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    model_version: Mapped[str] = mapped_column(String(160))
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB)
    context_limit: Mapped[int] = mapped_column(Integer)
    output_limit: Mapped[int] = mapped_column(Integer)
    cost_policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    data_policy_override: Mapped[str | None] = mapped_column(String(40))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class TaskModelAssignmentModel(Base):
    __tablename__ = "task_model_assignments"
    __table_args__ = (
        UniqueConstraint("task_type", "assignment_version", name="uq_task_assignment_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    task_type: Mapped[str] = mapped_column(String(80))
    assignment_version: Mapped[int] = mapped_column(Integer)
    primary_model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id"))
    fallback_model_ids: Mapped[list[str]] = mapped_column(JSONB)
    routing_policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    budget_policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str] = mapped_column(Text)


class TaskModelAssignmentHeadModel(Base):
    __tablename__ = "task_model_assignment_heads"

    task_type: Mapped[str] = mapped_column(String(80), primary_key=True)
    active_assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_model_assignments.id")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PromptTemplateModel(Base):
    __tablename__ = "prompt_templates"
    __table_args__ = (
        UniqueConstraint("template_key", "template_version", name="uq_prompt_template_version"),
        UniqueConstraint("template_key", "content_hash", name="uq_prompt_template_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    template_key: Mapped[str] = mapped_column(String(160))
    task_type: Mapped[str] = mapped_column(String(80))
    template_version: Mapped[int] = mapped_column(Integer)
    system_instructions: Mapped[str] = mapped_column(Text)
    template: Mapped[str] = mapped_column(Text)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB)
    response_schema: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str] = mapped_column(Text)


class PromptTemplateHeadModel(Base):
    __tablename__ = "prompt_template_heads"

    template_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    active_template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prompt_templates.id")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AIUsageRecordModel(Base):
    __tablename__ = "ai_usage_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    workflow_id: Mapped[str] = mapped_column(String(240), index=True)
    activity_id: Mapped[str] = mapped_column(String(240))
    task_type: Mapped[str] = mapped_column(String(80))
    provider_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("providers.id"))
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id"))
    prompt_template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prompt_templates.id")
    )
    request_hash: Mapped[str] = mapped_column(String(64))
    response_hash: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    cost: Mapped[dict[str, Any]] = mapped_column(JSONB)
    redaction_summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    correlation_id: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScriptModel(VersionedModelMixin, Base):
    __tablename__ = "scripts"

    opportunity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("opportunities.id"))
    research_dossier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("research_dossiers.id"), unique=True
    )
    status: Mapped[str] = mapped_column(String(30))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("script_versions.id", ondelete="RESTRICT", use_alter=True),
    )
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class ScriptVersionModel(Base):
    __tablename__ = "script_versions"
    __table_args__ = (
        UniqueConstraint("script_id", "version_number", name="uq_script_version_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    script_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scripts.id"))
    version_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(300))
    total_duration_seconds: Mapped[float] = mapped_column(Float)
    writer_model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id"))
    verifier_model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id"))
    writer_prompt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("prompt_templates.id"))
    verifier_prompt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("prompt_templates.id"))
    verification_report: Mapped[dict[str, Any]] = mapped_column(JSONB)
    coverage_percent: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("script_versions.id"))
    workflow_id: Mapped[str] = mapped_column(String(240))
    correlation_id: Mapped[str] = mapped_column(String(160))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScriptSegmentModel(Base):
    __tablename__ = "script_segments"
    __table_args__ = (
        UniqueConstraint("script_version_id", "segment_key", name="uq_script_segment_key"),
        UniqueConstraint("script_version_id", "segment_order", name="uq_script_segment_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    script_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("script_versions.id"))
    segment_key: Mapped[str] = mapped_column(String(120))
    segment_order: Mapped[int] = mapped_column(Integer)
    segment_type: Mapped[str] = mapped_column(String(40))
    narration: Mapped[str] = mapped_column(Text)
    presentation_purpose: Mapped[str] = mapped_column(Text)
    duration_seconds: Mapped[float] = mapped_column(Float)
    citation_display: Mapped[dict[str, Any]] = mapped_column(JSONB)
    annotations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    locked: Mapped[bool] = mapped_column(Boolean)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SegmentClaimModel(Base):
    __tablename__ = "segment_claims"
    __table_args__ = (
        UniqueConstraint(
            "script_segment_id", "claim_id", "start_offset", "end_offset",
            name="uq_segment_claim_statement",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    script_segment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("script_segments.id"))
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("claims.id"))
    evidence_excerpt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("evidence_excerpts.id"))
    statement_text: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    statement_kind: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StoryboardModel(VersionedModelMixin, Base):
    __tablename__ = "storyboards"

    script_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scripts.id"), unique=True)
    status: Mapped[str] = mapped_column(String(30))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("storyboard_versions.id", ondelete="RESTRICT", use_alter=True),
    )
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class StoryboardVersionModel(Base):
    __tablename__ = "storyboard_versions"
    __table_args__ = (
        UniqueConstraint("storyboard_id", "version_number", name="uq_storyboard_version_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    storyboard_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("storyboards.id"))
    script_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("script_versions.id"))
    version_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    content_hash: Mapped[str] = mapped_column(String(64))
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("storyboard_versions.id"))
    workflow_id: Mapped[str] = mapped_column(String(240))
    correlation_id: Mapped[str] = mapped_column(String(160))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SceneModel(VersionedModelMixin, Base):
    __tablename__ = "scenes"
    __table_args__ = (
        UniqueConstraint("storyboard_id", "scene_key", name="uq_storyboard_scene_key"),
    )

    storyboard_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("storyboards.id"))
    scene_key: Mapped[str] = mapped_column(String(120))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scene_versions.id", ondelete="RESTRICT", use_alter=True),
    )
    locked: Mapped[bool] = mapped_column(Boolean)


class SceneVersionModel(Base):
    __tablename__ = "scene_versions"
    __table_args__ = (
        UniqueConstraint("scene_id", "version_number", name="uq_scene_version_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    scene_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scenes.id"))
    storyboard_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("storyboard_versions.id"))
    version_number: Mapped[int] = mapped_column(Integer)
    scene_order: Mapped[int] = mapped_column(Integer)
    duration_seconds: Mapped[float] = mapped_column(Float)
    visual_type: Mapped[str] = mapped_column(String(40))
    scene_spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scene_versions.id"))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SceneAlternativeModel(Base):
    __tablename__ = "scene_alternatives"
    __table_args__ = (
        UniqueConstraint(
            "scene_id",
            "base_scene_version_id",
            "alternative_number",
            name="uq_scene_alternative_number",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    scene_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scenes.id"))
    base_scene_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scene_versions.id")
    )
    alternative_number: Mapped[int] = mapped_column(Integer)
    scene_spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id"))
    prompt_template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prompt_templates.id")
    )
    workflow_id: Mapped[str] = mapped_column(String(240), unique=True)
    instruction: Mapped[str] = mapped_column(Text)
    correlation_id: Mapped[str] = mapped_column(String(160))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApprovalModel(Base):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    target_type: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    target_version: Mapped[int] = mapped_column(Integer)
    target_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str] = mapped_column(Text)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    supersedes_approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("approvals.id"))
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    correlation_id: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ComfyWorkflowVersionModel(Base):
    __tablename__ = "comfy_workflow_versions"
    __table_args__ = (
        UniqueConstraint("workflow_key", "version_number", name="uq_comfy_workflow_version"),
        UniqueConstraint("workflow_key", "content_hash", name="uq_comfy_workflow_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    workflow_key: Mapped[str] = mapped_column(String(160))
    version_number: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(160))
    api_workflow: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    required_nodes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    required_models: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    typed_inputs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    output_contract: Mapped[dict[str, Any]] = mapped_column(JSONB)
    allowed_resolutions: Mapped[list[dict[str, int]]] = mapped_column(JSONB)
    allowed_durations: Mapped[dict[str, float]] = mapped_column(JSONB)
    approval_state: Mapped[str] = mapped_column(String(20))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str] = mapped_column(Text)


class ComfyWorkflowHeadModel(Base):
    __tablename__ = "comfy_workflow_heads"

    workflow_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    active_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("comfy_workflow_versions.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VoiceProfileVersionModel(Base):
    __tablename__ = "voice_profile_versions"
    __table_args__ = (
        UniqueConstraint("profile_key", "version_number", name="uq_voice_profile_version"),
        UniqueConstraint("profile_key", "content_hash", name="uq_voice_profile_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    profile_key: Mapped[str] = mapped_column(String(160))
    version_number: Mapped[int] = mapped_column(Integer)
    provider_type: Mapped[str] = mapped_column(String(40))
    endpoint: Mapped[str | None] = mapped_column(Text)
    voice_id: Mapped[str] = mapped_column(String(240))
    language: Mapped[str] = mapped_column(String(40))
    engine: Mapped[str] = mapped_column(String(160))
    model_version: Mapped[str] = mapped_column(String(160))
    delivery: Mapped[dict[str, Any]] = mapped_column(JSONB)
    pronunciation: Mapped[dict[str, Any]] = mapped_column(JSONB)
    output_settings: Mapped[dict[str, Any]] = mapped_column(JSONB)
    consent: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str] = mapped_column(Text)


class VoiceProfileHeadModel(Base):
    __tablename__ = "voice_profile_heads"

    profile_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    active_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("voice_profile_versions.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MediaProductionModel(Base):
    __tablename__ = "media_productions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    storyboard_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("storyboard_versions.id"))
    storyboard_hash: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str] = mapped_column(String(240), unique=True)
    render_tier: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(40))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB)
    correlation_id: Mapped[str] = mapped_column(String(160))
    started_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaAssetModel(Base):
    __tablename__ = "media_assets"
    __table_args__ = (UniqueConstraint("production_id", "object_key", name="uq_media_asset_object"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    production_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("media_productions.id"))
    scene_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scene_versions.id"))
    asset_kind: Mapped[str] = mapped_column(String(40))
    object_key: Mapped[str] = mapped_column(String(1024))
    content_hash: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(255))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    licence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    generation_provenance: Mapped[dict[str, Any]] = mapped_column(JSONB)
    cache_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NarrationSegmentModel(Base):
    __tablename__ = "narration_segments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    production_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("media_productions.id"))
    script_segment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("script_segments.id"))
    segment_order: Mapped[int] = mapped_column(Integer)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    original_asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("media_assets.id"))
    mastered_asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("media_assets.id"))
    duration_seconds: Mapped[float] = mapped_column(Float)
    sample_rate: Mapped[int] = mapped_column(Integer)
    word_alignment: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    parent_segment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("narration_segments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProductionManifestModel(Base):
    __tablename__ = "production_manifests"
    __table_args__ = (
        UniqueConstraint("production_id", "manifest_version", name="uq_production_manifest_version"),
        UniqueConstraint("production_id", "content_hash", name="uq_production_manifest_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    production_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("media_productions.id"))
    manifest_version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProductionRenderModel(Base):
    __tablename__ = "production_renders"
    __table_args__ = (UniqueConstraint("production_id", "render_number", name="uq_production_render_number"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    production_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("media_productions.id"))
    render_number: Mapped[int] = mapped_column(Integer)
    tier: Mapped[str] = mapped_column(String(20))
    video_asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("media_assets.id"))
    manifest_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("production_manifests.id"))
    render_engine: Mapped[str] = mapped_column(String(80))
    render_engine_version: Mapped[str] = mapped_column(String(80))
    composition: Mapped[str] = mapped_column(String(160))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB)
    probe: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QAReportModel(Base):
    __tablename__ = "qa_reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    render_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("production_renders.id"), unique=True)
    verdict: Mapped[str] = mapped_column(String(10))
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QAFindingModel(Base):
    __tablename__ = "qa_findings"
    __table_args__ = (
        UniqueConstraint("report_id", "finding_order", name="uq_qa_finding_order"),
        UniqueConstraint("report_id", "code", "scene_version_id", name="uq_qa_finding_code_scene"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("qa_reports.id"))
    finding_order: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(120))
    verdict: Mapped[str] = mapped_column(String(10))
    message: Mapped[str] = mapped_column(Text)
    scene_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scene_versions.id"))
    timecode_seconds: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)
    override_policy: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QAOverrideModel(Base):
    __tablename__ = "qa_overrides"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    finding_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("qa_findings.id"), unique=True)
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    correlation_id: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class YouTubeConnectionModel(Base):
    __tablename__ = "youtube_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("channel_profiles.id"), unique=True)
    youtube_channel_id: Mapped[str] = mapped_column(String(160))
    youtube_channel_title: Mapped[str] = mapped_column(String(240))
    refresh_token_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    token_fingerprint: Mapped[str] = mapped_column(String(64))
    granted_scopes: Mapped[list[str]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(30))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OAuthStateModel(Base):
    __tablename__ = "youtube_oauth_states"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("channel_profiles.id"))
    initiated_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    redirect_uri: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PublishingConfigurationVersionModel(Base):
    __tablename__ = "publishing_configuration_versions"
    __table_args__ = (
        UniqueConstraint("version_number", name="uq_publishing_config_version"),
        UniqueConstraint("content_hash", name="uq_publishing_config_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    version_number: Mapped[int] = mapped_column(Integer)
    real_uploads_enabled: Mapped[bool] = mapped_column(Boolean)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str] = mapped_column(Text)


class PublishingConfigurationHeadModel(Base):
    __tablename__ = "publishing_configuration_head"

    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True)
    active_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("publishing_configuration_versions.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PublishMetadataVersionModel(Base):
    __tablename__ = "publish_metadata_versions"
    __table_args__ = (
        UniqueConstraint("render_id", "version_number", name="uq_publish_metadata_version"),
        UniqueConstraint("render_id", "content_hash", name="uq_publish_metadata_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    render_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("production_renders.id"))
    version_number: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str] = mapped_column(Text)


class PublicationApprovalModel(Base):
    __tablename__ = "publication_approvals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    purpose: Mapped[str] = mapped_column(String(30))
    render_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("production_renders.id"))
    render_hash: Mapped[str] = mapped_column(String(64))
    metadata_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("publish_metadata_versions.id"))
    metadata_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    correlation_id: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PublicationModel(Base):
    __tablename__ = "publications"
    __table_args__ = (
        UniqueConstraint("upload_idempotency_key", name="uq_publication_upload_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    workflow_id: Mapped[str] = mapped_column(String(240), unique=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("youtube_connections.id"))
    render_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("production_renders.id"))
    render_hash: Mapped[str] = mapped_column(String(64))
    metadata_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("publish_metadata_versions.id"))
    metadata_hash: Mapped[str] = mapped_column(String(64))
    upload_approval_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("publication_approvals.id"))
    configuration_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("publishing_configuration_versions.id"))
    mode: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(40))
    upload_idempotency_key: Mapped[str] = mapped_column(String(64))
    youtube_video_id: Mapped[str | None] = mapped_column(String(160))
    resumable_session_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    processing_status: Mapped[dict[str, Any]] = mapped_column(JSONB)
    caption_status: Mapped[dict[str, Any]] = mapped_column(JSONB)
    thumbnail_status: Mapped[dict[str, Any]] = mapped_column(JSONB)
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    correlation_id: Mapped[str] = mapped_column(String(160))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PublicationScheduleModel(Base):
    __tablename__ = "publication_schedules"
    __table_args__ = (UniqueConstraint("schedule_idempotency_key", name="uq_publication_schedule_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    publication_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("publications.id"))
    approval_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("publication_approvals.id"))
    publish_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    schedule_idempotency_key: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30))
    provider_response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    correlation_id: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AnalyticsMetricSnapshotModel(Base):
    __tablename__ = "analytics_metric_snapshots"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "youtube_video_id", "period_start", "period_end", "content_hash", name="uq_analytics_snapshot_content"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("channel_profiles.id"))
    publication_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("publications.id"))
    youtube_video_id: Mapped[str] = mapped_column(String(160))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(String(64))
    ingested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModelBenchmarkRunModel(Base):
    __tablename__ = "model_benchmark_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    task_type: Mapped[str] = mapped_column(String(80))
    suite_key: Mapped[str] = mapped_column(String(160))
    suite_version: Mapped[str] = mapped_column(String(80))
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    weights: Mapped[dict[str, Any]] = mapped_column(JSONB)
    input_hash: Mapped[str] = mapped_column(String(64))
    baseline_assignment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("task_model_assignments.id"))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    correlation_id: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModelRecommendationModel(Base):
    __tablename__ = "model_recommendations"
    __table_args__ = (UniqueConstraint("benchmark_run_id", name="uq_benchmark_recommendation"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    benchmark_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("model_benchmark_runs.id"))
    task_type: Mapped[str] = mapped_column(String(80))
    recommended_model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id"))
    recommended_fallback_model_ids: Mapped[list[str]] = mapped_column(JSONB)
    baseline_assignment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("task_model_assignments.id"))
    score: Mapped[float] = mapped_column(Float)
    reasoning: Mapped[list[str]] = mapped_column(JSONB)
    recommendation_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModelRecommendationDecisionModel(Base):
    __tablename__ = "model_recommendation_decisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    recommendation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("model_recommendations.id"))
    decision: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    correlation_id: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModelRecommendationApplicationModel(Base):
    __tablename__ = "model_recommendation_applications"
    __table_args__ = (UniqueConstraint("recommendation_id", name="uq_recommendation_application"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    recommendation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("model_recommendations.id"))
    decision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("model_recommendation_decisions.id"))
    previous_assignment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("task_model_assignments.id"))
    new_assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("task_model_assignments.id"))
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    correlation_id: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceFreshnessCheckModel(Base):
    __tablename__ = "source_freshness_checks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("source_snapshots.id"))
    maximum_age_seconds: Mapped[int] = mapped_column(BigInteger)
    age_seconds: Mapped[int] = mapped_column(BigInteger)
    verdict: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(160))
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    checked_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OriginalityReportModel(Base):
    __tablename__ = "originality_reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    script_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("script_versions.id"))
    comparisons: Mapped[dict[str, Any]] = mapped_column(JSONB)
    maximum_overlap: Mapped[float] = mapped_column(Float)
    verdict: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(160))
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    checked_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CorrectionRecordModel(Base):
    __tablename__ = "correction_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    publication_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("publications.id"))
    script_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("script_versions.id"))
    source_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("source_snapshots.id"))
    severity: Mapped[str] = mapped_column(String(20))
    finding: Mapped[str] = mapped_column(Text)
    required_action: Mapped[str] = mapped_column(String(80))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    affected_claims: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    affected_timecodes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    correlation_id: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BudgetPolicyVersionModel(Base):
    __tablename__ = "budget_policy_versions"
    __table_args__ = (
        UniqueConstraint("scope", "version_number", name="uq_budget_policy_version"),
        UniqueConstraint("scope", "content_hash", name="uq_budget_policy_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    scope: Mapped[str] = mapped_column(String(160))
    version_number: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    period: Mapped[str] = mapped_column(String(20))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str] = mapped_column(Text)


class BudgetPolicyHeadModel(Base):
    __tablename__ = "budget_policy_heads"

    scope: Mapped[str] = mapped_column(String(160), primary_key=True)
    active_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("budget_policy_versions.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BudgetUsageRecordModel(Base):
    __tablename__ = "budget_usage_records"
    __table_args__ = (UniqueConstraint("scope", "idempotency_key", name="uq_budget_usage_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    scope: Mapped[str] = mapped_column(String(160))
    policy_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("budget_policy_versions.id"))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    currency: Mapped[str] = mapped_column(String(3))
    category: Mapped[str] = mapped_column(String(80))
    workflow_id: Mapped[str | None] = mapped_column(String(240))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)
    recorded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OperationalEvidenceModel(Base):
    __tablename__ = "operational_evidence"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    evidence_kind: Mapped[str] = mapped_column(String(40))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    artifact_hash: Mapped[str | None] = mapped_column(String(64))
    recorded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    correlation_id: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
