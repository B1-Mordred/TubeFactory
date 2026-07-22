from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from editorial_core.authorization import Role
from editorial_core.branding import normalize_brand_kit
from editorial_core.channel_workflow import channel_automation_workflow
from editorial_core.explanation_readiness import explanation_policy
from editorial_core.operating_policy import evaluate_operating_policy
from editorial_core.publishing import PublishMode


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AutomaticContinuation(BaseModel):
    state: Literal["started", "completed", "awaiting_input", "not_applicable", "reconciled"]
    action: str
    workflow_id: str | None = None
    message: str


class AnalyticsSnapshotWrite(StrictRequestModel):
    channel_profile_id: UUID
    publication_id: UUID | None = None
    youtube_video_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")
    period_start: datetime
    period_end: datetime
    metrics: dict[str, float]
    dimensions: dict[str, str] = Field(default_factory=dict)
    source: Literal["youtube_analytics", "operator_import", "manual_fixture"] = "operator_import"

    @model_validator(mode="after")
    def valid_period_and_metrics(self) -> "AnalyticsSnapshotWrite":
        if self.period_start.tzinfo is None or self.period_end.tzinfo is None or self.period_end <= self.period_start:
            raise ValueError("analytics period must use timezone-aware increasing timestamps")
        if not self.metrics or any(not key or value < 0 for key, value in self.metrics.items()):
            raise ValueError("analytics metrics must be non-empty and non-negative")
        return self


class BenchmarkCandidateWrite(StrictRequestModel):
    model_id: UUID
    quality_score: float = Field(ge=0, le=100)
    success_rate: float = Field(ge=0, le=1)
    p95_latency_ms: int = Field(gt=0, le=3_600_000)
    mean_cost_usd: Decimal = Field(ge=0, decimal_places=6, max_digits=18)
    policy_eligible: bool = True


class ModelBenchmarkWrite(StrictRequestModel):
    task_type: Literal[
        "script_writer",
        "script_verifier",
        "storyboard",
        "topic_qualifier",
        "research_query_planner",
        "evidence_synthesizer",
        "evidence_reviewer",
    ]
    suite_key: str = Field(min_length=3, max_length=160, pattern=r"^[a-z][a-z0-9_.-]*$")
    suite_version: str = Field(min_length=1, max_length=80)
    candidates: list[BenchmarkCandidateWrite] = Field(min_length=2, max_length=50)
    weights: dict[Literal["quality", "reliability", "latency", "cost"], float] = Field(
        default_factory=lambda: {"quality": 0.65, "reliability": 0.20, "latency": 0.10, "cost": 0.05}
    )

    @model_validator(mode="after")
    def valid_benchmark(self) -> "ModelBenchmarkWrite":
        if len({item.model_id for item in self.candidates}) != len(self.candidates):
            raise ValueError("benchmark candidate models must be unique")
        if set(self.weights) != {"quality", "reliability", "latency", "cost"}:
            raise ValueError("all four benchmark weights are required")
        if any(value < 0 for value in self.weights.values()) or abs(sum(self.weights.values()) - 1) > 0.000001:
            raise ValueError("benchmark weights must be non-negative and sum to one")
        return self


class RecommendationDecisionWrite(StrictRequestModel):
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=10, max_length=4000)


class RecommendationApplyWrite(StrictRequestModel):
    expected_baseline_assignment_id: UUID | None
    comment: str = Field(min_length=10, max_length=2000)


class FreshnessCheckWrite(StrictRequestModel):
    source_snapshot_id: UUID
    maximum_age_seconds: int = Field(gt=0, le=31_536_000)


class OriginalityReportWrite(StrictRequestModel):
    script_version_id: UUID
    comparisons: dict[str, float] = Field(default_factory=dict, max_length=500)
    review_threshold: float = Field(default=0.20, ge=0, le=1)
    block_threshold: float = Field(default=0.35, ge=0, le=1)

    @model_validator(mode="after")
    def valid_originality_thresholds(self) -> "OriginalityReportWrite":
        if self.review_threshold >= self.block_threshold:
            raise ValueError("review threshold must be lower than block threshold")
        if any(not key or not 0 <= value <= 1 for key, value in self.comparisons.items()):
            raise ValueError("comparison overlap must be between zero and one")
        return self


class CorrectionWrite(StrictRequestModel):
    publication_id: UUID | None = None
    script_version_id: UUID | None = None
    source_snapshot_id: UUID | None = None
    change_kind: Literal["changed", "retracted", "corrected", "unavailable"] = "changed"
    severity: Literal["low", "medium", "high", "critical"]
    finding: str = Field(min_length=10, max_length=10000)
    evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def correction_has_target(self) -> "CorrectionWrite":
        if self.publication_id is None and self.script_version_id is None and self.source_snapshot_id is None:
            raise ValueError("a correction requires a publication, script version, or source snapshot target")
        return self


class OperationalEvidenceWrite(StrictRequestModel):
    evidence_kind: Literal["backup", "restore_drill", "sbom", "security_audit", "observability"]
    document: dict[str, Any]
    artifact_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class BudgetPolicyWrite(StrictRequestModel):
    scope: str = Field(min_length=3, max_length=160, pattern=r"^[a-z][a-z0-9_.:-]*$")
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    limit_amount: Decimal = Field(ge=0, decimal_places=6, max_digits=18)
    period: Literal["workflow", "daily", "monthly"]
    comment: str = Field(min_length=10, max_length=2000)


class BudgetUsageWrite(StrictRequestModel):
    scope: str = Field(min_length=3, max_length=160, pattern=r"^[a-z][a-z0-9_.:-]*$")
    idempotency_key: str = Field(min_length=8, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    amount: Decimal = Field(ge=0, decimal_places=6, max_digits=18)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    category: str = Field(min_length=2, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    workflow_id: str | None = Field(default=None, max_length=240)
    details: dict[str, Any] = Field(default_factory=dict)


class PublishingConfigurationWrite(StrictRequestModel):
    real_uploads_enabled: bool = False
    provider: Literal["youtube"] = "youtube"
    comment: str = Field(min_length=10, max_length=2000)


class PublishingConfigurationView(BaseModel):
    id: UUID | None = None
    version_number: int
    real_uploads_enabled: bool
    document: dict[str, Any]
    content_hash: str | None = None
    created_by: UUID | None = None
    created_at: datetime | None = None


class YouTubeOAuthStart(StrictRequestModel):
    channel_profile_id: UUID
    redirect_uri: str = Field(min_length=10, max_length=2000)

    @field_validator("redirect_uri")
    @classmethod
    def safe_redirect_uri(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("redirect_uri must be an absolute HTTP(S) URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("non-local OAuth redirects must use HTTPS")
        return value


class YouTubeOAuthStartView(BaseModel):
    authorization_url: str
    expires_at: datetime


class YouTubeConnectionView(BaseModel):
    id: UUID
    channel_profile_id: UUID
    youtube_channel_id: str
    youtube_channel_title: str
    granted_scopes: list[str]
    status: str
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


class MockYouTubeConnectionWrite(StrictRequestModel):
    channel_profile_id: UUID
    youtube_channel_id: str = Field(min_length=3, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")
    youtube_channel_title: str = Field(min_length=1, max_length=240)


class PublishMetadataWrite(StrictRequestModel):
    render_id: UUID
    expected_render_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=4000)
    sources: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    evidence_url: str | None = Field(default=None, max_length=2000)
    chapters: list[dict[str, Any]] = Field(min_length=1, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=50)
    category_id: str = Field(default="27", pattern=r"^[0-9]+$")
    language: str = Field(default="en", min_length=2, max_length=40)
    made_for_kids: bool
    contains_synthetic_media: bool
    captions: dict[str, Any]
    thumbnail: dict[str, Any]
    comment: str = Field(default="Publishing metadata version", min_length=10, max_length=2000)


class PublishMetadataView(BaseModel):
    id: UUID
    render_id: UUID
    version_number: int
    document: dict[str, Any]
    content_hash: str
    created_by: UUID
    created_at: datetime
    comment: str


class PublicationApprovalWrite(StrictRequestModel):
    purpose: Literal["private_upload", "public_release"]
    render_id: UUID
    expected_render_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_version_id: UUID
    expected_metadata_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approved", "rejected"]
    comment: str = Field(min_length=10, max_length=4000)
    connection_id: UUID | None = None
    mode: PublishMode | None = None
    publish_at: datetime | None = None


class PublicationApprovalView(BaseModel):
    id: UUID
    purpose: str
    render_id: UUID
    render_hash: str
    metadata_version_id: UUID
    metadata_hash: str
    decision: str
    comment: str
    actor_id: UUID
    correlation_id: str
    created_at: datetime
    automatic_continuation: AutomaticContinuation | None = None


class PublicationStart(StrictRequestModel):
    connection_id: UUID
    render_id: UUID
    expected_render_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_version_id: UUID
    expected_metadata_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: PublishMode = PublishMode.DRY_RUN
    idempotency_key: str = Field(min_length=8, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")


class PublicationScheduleWrite(StrictRequestModel):
    publish_at: datetime


class PublicationView(BaseModel):
    id: UUID
    workflow_id: str
    connection_id: UUID
    render_id: UUID
    render_hash: str
    metadata_version_id: UUID
    metadata_hash: str
    mode: str
    state: str
    youtube_video_id: str | None
    uploaded_bytes: int
    processing_status: dict[str, Any]
    caption_status: dict[str, Any]
    thumbnail_status: dict[str, Any]
    failure: dict[str, Any] | None
    correlation_id: str
    version: int
    created_at: datetime
    updated_at: datetime


class BootstrapStatus(BaseModel):
    required: bool


class BootstrapRequest(StrictRequestModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=8, max_length=512)


class LoginRequest(StrictRequestModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=512)
    totp_code: str | None = Field(default=None, min_length=6, max_length=32, pattern=r"^[A-Za-z0-9-]+$")

    @field_validator("username")
    @classmethod
    def normalized_username(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("username cannot be blank")
        return normalized


class TOTPEnrollmentStart(StrictRequestModel):
    current_password: str = Field(min_length=8, max_length=512)


class TOTPEnrollmentView(BaseModel):
    secret: str
    provisioning_uri: str


class TOTPEnrollmentConfirm(StrictRequestModel):
    code: str = Field(min_length=6, max_length=8, pattern=r"^[0-9]+$")


class TOTPRecoveryCodesView(BaseModel):
    recovery_codes: list[str]


class TOTPDisable(StrictRequestModel):
    current_password: str = Field(min_length=8, max_length=512)
    code: str = Field(min_length=6, max_length=32, pattern=r"^[A-Za-z0-9-]+$")


class OIDCStatusView(BaseModel):
    enabled: bool
    configured: bool
    issuer: str | None = None
    version_number: int | None = None


class OIDCConfigurationWrite(StrictRequestModel):
    enabled: bool = True
    issuer: str = Field(min_length=8, max_length=500)
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: str | None = Field(default=None, min_length=8, max_length=4000)
    authorization_endpoint: str = Field(min_length=8, max_length=1000)
    token_endpoint: str = Field(min_length=8, max_length=1000)
    jwks_uri: str = Field(min_length=8, max_length=1000)
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email"], min_length=1, max_length=20)
    username_claim: str = Field(default="preferred_username", min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    display_name_claim: str = Field(default="name", min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    role_claim: str = Field(default="groups", min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    role_mapping: dict[str, Role] = Field(default_factory=dict, max_length=100)
    default_role: Role | None = None
    comment: str = Field(min_length=3, max_length=2000)

    @model_validator(mode="after")
    def valid_oidc_configuration(self) -> "OIDCConfigurationWrite":
        for label, value in {
            "issuer": self.issuer,
            "authorization endpoint": self.authorization_endpoint,
            "token endpoint": self.token_endpoint,
            "JWKS URI": self.jwks_uri,
        }.items():
            parsed = urlsplit(value)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(f"{label} must be an HTTP(S) URL without embedded credentials")
        if "openid" not in self.scopes or len(set(self.scopes)) != len(self.scopes):
            raise ValueError("OIDC scopes must be unique and include openid")
        if not self.role_mapping and self.default_role is None:
            raise ValueError("OIDC requires an explicit role mapping or default role")
        return self


class OIDCConfigurationView(BaseModel):
    id: UUID | None = None
    version_number: int
    enabled: bool
    issuer: str
    client_id: str
    has_client_secret: bool
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    scopes: list[str]
    username_claim: str
    display_name_claim: str
    role_claim: str
    role_mapping: dict[str, Role]
    default_role: Role | None
    content_hash: str | None = None
    created_at: datetime | None = None
    comment: str = ""


class UserCreate(StrictRequestModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=8, max_length=512)
    role: Role


class UserUpdate(StrictRequestModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    role: Role | None = None
    enabled: bool | None = None
    expected_version: int = Field(ge=1)


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    display_name: str
    role: Role
    identity_provider: Literal["local", "oidc"]
    totp_enabled: bool
    enabled: bool
    version: int
    created_at: datetime


class SessionView(BaseModel):
    user: UserView
    csrf_token: str


class ConfigurationWrite(StrictRequestModel):
    namespace: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_.-]*$")
    schema_version: str = Field(min_length=1, max_length=40)
    document: dict[str, Any]
    comment: str = Field(default="", max_length=2000)


class ConfigurationImport(StrictRequestModel):
    namespace: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_.-]*$")
    schema_version: str = Field(min_length=1, max_length=40)
    format: Literal["json", "yaml"]
    content: str = Field(min_length=2, max_length=2_000_000)
    comment: str = Field(default="Imported through API", max_length=2000)


class ConfigurationView(BaseModel):
    id: UUID
    namespace: str
    version: int
    schema_version: str
    document: dict[str, Any]
    document_hash: str
    created_by: UUID
    created_at: datetime
    comment: str
    active: bool


class AuditEventView(BaseModel):
    id: UUID
    occurred_at: datetime
    actor_id: UUID | None
    action: str
    target_type: str | None
    target_id: str | None
    correlation_id: str
    context: dict[str, Any]
    previous_hash: str | None
    event_hash: str


class ProbeStart(StrictRequestModel):
    idempotency_key: str = Field(min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$")


class ProbeView(BaseModel):
    workflow_id: str
    state: str
    started_at: str | None = None
    completed_at: str | None = None
    replay_count: int = 0


class WorkflowSummaryView(BaseModel):
    workflow_id: str
    workflow_type: str
    execution_status: Literal[
        "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TERMINATED", "TIMED_OUT", "UNKNOWN"
    ]
    parent_workflow_id: str | None
    correlation_id: str
    channel_profile_id: UUID | None = None
    channel_name: str | None = None
    subject_profile_id: UUID | None = None
    subject_name: str | None = None
    created_at: datetime


class ChannelProfileWrite(StrictRequestModel):
    slug: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=2, max_length=180)
    enabled: bool = False
    identity: dict[str, Any] = Field(default_factory=dict)
    languages: list[str] = Field(min_length=1)
    audience: dict[str, Any] = Field(default_factory=dict)
    editorial_rules: dict[str, Any] = Field(default_factory=dict)
    brand_kit: dict[str, Any] = Field(default_factory=dict)
    default_render_settings: dict[str, Any] = Field(default_factory=dict)
    default_publish_settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("languages")
    @classmethod
    def normalized_languages(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip().lower() for value in values if value.strip()))
        if not normalized:
            raise ValueError("at least one language is required")
        return normalized

    @field_validator("editorial_rules")
    @classmethod
    def validated_channel_workflow(cls, value: dict[str, Any]) -> dict[str, Any]:
        channel_automation_workflow(value)
        return value

    @model_validator(mode="after")
    def normalized_brand_kit(self) -> "ChannelProfileWrite":
        self.brand_kit = normalize_brand_kit(self.brand_kit, channel_name=self.name)
        return self


class ChannelProfileUpdate(ChannelProfileWrite):
    expected_version: int = Field(ge=1)


class ChannelProfileView(ChannelProfileWrite):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: int
    created_at: datetime
    updated_at: datetime


class ArchivedChannelProfileView(ChannelProfileView):
    archived_at: datetime


class ProfileArchiveView(BaseModel):
    id: UUID
    version: int
    archived_at: datetime


class SubjectSchedulePolicy(StrictRequestModel):
    cron: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str = Field(default="UTC", min_length=1, max_length=100)

    @field_validator("cron")
    @classmethod
    def normalized_cron(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone name") from exc
        return normalized


class SubjectProfileWrite(StrictRequestModel):
    channel_profile_id: UUID
    name: str = Field(min_length=2, max_length=180)
    enabled: bool = False
    topic: str = Field(min_length=2, max_length=4000)
    research_goal: str = Field(min_length=2, max_length=8000)
    excluded_angles: list[str] = Field(default_factory=list)
    seed_queries: list[str] = Field(min_length=1, max_length=100)
    related_concepts: list[str] = Field(default_factory=list, max_length=100)
    negative_keywords: list[str] = Field(default_factory=list, max_length=100)
    languages: list[str] = Field(default_factory=lambda: ["en"], min_length=1)
    regions: list[str] = Field(default_factory=list)
    domain_policy: dict[str, Any] = Field(default_factory=lambda: {"allow": [], "block": []})
    source_requirements: dict[str, Any] = Field(
        default_factory=lambda: {"minimum_independent": 2, "minimum_primary": 1}
    )
    schedule: SubjectSchedulePolicy = Field(default_factory=SubjectSchedulePolicy)
    freshness_policy: dict[str, Any] = Field(
        default_factory=lambda: {"lookback_days": 30, "maximum_source_age_days": 3650}
    )
    format_policy: dict[str, Any] = Field(
        default_factory=lambda: {"target": "standard", "duration_seconds": 600}
    )
    editorial_profile: dict[str, Any] = Field(default_factory=dict)
    risk: Literal["low", "medium", "high"] = "medium"
    budget: dict[str, Any] = Field(
        default_factory=lambda: {"tokens": 0, "gpu_seconds": 0, "currency_minor": 0}
    )
    opportunity_weights: dict[str, float] = Field(default_factory=dict)
    approval_profile: dict[str, Any] = Field(default_factory=dict)

    @field_validator("seed_queries", "languages")
    @classmethod
    def nonempty_strings(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not normalized:
            raise ValueError("at least one non-empty value is required")
        return normalized

    @model_validator(mode="after")
    def validated_operating_profile(self) -> "SubjectProfileWrite":
        profile = dict(self.approval_profile)
        mode = str(profile.get("mode", "assisted"))
        topics = profile.get("sensitive_topics", [])
        if not isinstance(topics, list) or not all(isinstance(value, str) for value in topics):
            raise ValueError("sensitive_topics must be a list of policy categories")
        evaluate_operating_policy(mode=mode, risk=self.risk, sensitive_topics=topics)
        density = profile.get("evidence_density_minimum", 0.0)
        repetition = profile.get("repeated_scene_limit", 1)
        if not isinstance(density, (int, float)) or not 0 <= float(density) <= 100:
            raise ValueError("evidence_density_minimum must be between 0 and 100")
        if not isinstance(repetition, int) or not 0 <= repetition <= 100:
            raise ValueError("repeated_scene_limit must be between 0 and 100")
        self.approval_profile = {
            **profile,
            "mode": mode,
            "sensitive_topics": sorted(set(topics)),
            "evidence_density_minimum": float(density),
            "repeated_scene_limit": repetition,
        }
        policy = explanation_policy(self.format_policy, self.approval_profile)
        self.format_policy = {
            **self.format_policy,
            "duration_seconds": policy.target_duration_seconds,
            "minimum_duration_seconds": policy.minimum_duration_seconds,
            "maximum_duration_seconds": policy.maximum_duration_seconds,
            "speaking_rate_wpm": policy.speaking_rate_wpm,
            "max_enrichment_rounds": policy.maximum_enrichment_rounds,
            "minimum_coverage_units": policy.minimum_coverage_units,
        }
        return self


class SubjectProfileUpdate(SubjectProfileWrite):
    expected_version: int = Field(ge=1)


class SubjectProfileView(SubjectProfileWrite):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: int
    created_at: datetime
    updated_at: datetime


class ArchivedSubjectProfileView(SubjectProfileView):
    archived_at: datetime


class SearchStrategyView(BaseModel):
    purpose: str
    query: str
    language: str
    region: str | None


class SearchPlanView(BaseModel):
    subject_topic: str
    strategies: list[SearchStrategyView]
    expected_primary_source_types: list[str]
    falsification_queries: list[str]


class SubjectScheduleView(BaseModel):
    schedule_id: str
    exists: bool
    paused: bool
    cron: str | None
    timezone: str
    action_count: int = Field(ge=0)
    next_action_times: list[datetime]


class FixtureResearchStart(StrictRequestModel):
    subject_profile_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$")


class LiveDiscoveryStart(FixtureResearchStart):
    pass


class OpportunityAcquisitionStart(StrictRequestModel):
    opportunity_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$")


class OpportunityResearchStart(OpportunityAcquisitionStart):
    pass


class OpportunityDecisionWrite(StrictRequestModel):
    decision: Literal["approved", "rejected", "deferred"]
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=4000)
    editorial_rationale: str | None = Field(default=None, min_length=20, max_length=4000)


class ManualOpportunityWrite(StrictRequestModel):
    subject_profile_id: UUID
    title: str = Field(min_length=3, max_length=300)
    summary: str = Field(min_length=20, max_length=8000)
    editorial_rationale: str = Field(min_length=20, max_length=4000)
    estimated_cost: dict[str, Any] = Field(
        default_factory=lambda: {"tokens": 0, "gpu_seconds": 0, "currency_minor": 0}
    )


class ManualDossierClaimWrite(StrictRequestModel):
    statement: str = Field(min_length=10, max_length=2000)
    claim_type: Literal["fact", "inference", "opinion"] = "fact"
    confidence: int = Field(default=70, ge=0, le=100)
    risk: Literal["low", "medium", "high"] = "medium"
    central: bool = True
    source_snapshot_id: UUID | None = None
    exact_text: str | None = Field(default=None, min_length=10, max_length=4000)
    relationship: Literal["supports", "contradicts", "context"] = "supports"
    source_independent: bool = True
    direct_evidence: bool = True
    primary_source: bool = False
    coverage_unit_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("statement", "exact_text")
    @classmethod
    def stripped_manual_claim_text(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def evidence_pair_is_complete(self) -> "ManualDossierClaimWrite":
        if not self.statement.strip():
            raise ValueError("claim statement must be non-empty")
        has_snapshot = self.source_snapshot_id is not None
        has_excerpt = bool(self.exact_text and self.exact_text.strip())
        if has_snapshot != has_excerpt:
            raise ValueError("source_snapshot_id and exact_text must be supplied together")
        return self


class ManualDossierWrite(StrictRequestModel):
    expected_opportunity_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$")
    executive_summary: str = Field(min_length=20, max_length=8000)
    safe_conclusions: list[str] = Field(min_length=1, max_length=40)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=40)
    alternative_explanations: list[str] = Field(default_factory=list, max_length=40)
    source_quality_notes: list[str] = Field(default_factory=list, max_length=40)
    prohibited_overstatements: list[str] = Field(default_factory=list, max_length=40)
    proposed_angles: list[str] = Field(default_factory=list, max_length=20)
    chronology: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    explanation_plan: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    counterevidence_search_completed: bool = False
    review_note: str = Field(min_length=10, max_length=4000)
    claims: list[ManualDossierClaimWrite] = Field(default_factory=list, max_length=80)

    @field_validator(
        "safe_conclusions",
        "unresolved_questions",
        "alternative_explanations",
        "source_quality_notes",
        "prohibited_overstatements",
        "proposed_angles",
    )
    @classmethod
    def stripped_manual_dossier_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("executive_summary", "review_note")
    @classmethod
    def stripped_manual_dossier_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def has_substantive_manual_content(self) -> "ManualDossierWrite":
        if not self.executive_summary:
            raise ValueError("executive_summary must be non-empty")
        if not self.safe_conclusions:
            raise ValueError("at least one safe conclusion is required")
        if not self.review_note:
            raise ValueError("review_note must be non-empty")
        return self


class OpportunityDecisionView(BaseModel):
    id: UUID
    decision: Literal["pending", "approved", "rejected", "deferred"]
    version: int
    decision_reason: str | None
    decided_at: datetime | None
    automatic_continuation: AutomaticContinuation | None = None


class ScoredOpportunityArchiveWrite(StrictRequestModel):
    subject_profile_id: UUID
    reason: str = Field(min_length=10, max_length=4000)


class ScoredOpportunityArchiveView(BaseModel):
    subject_profile_id: UUID
    archived_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    archived_at: datetime


class AllOpportunityArchiveWrite(StrictRequestModel):
    reason: str = Field(min_length=10, max_length=4000)


class AllOpportunityArchiveView(BaseModel):
    archived_count: int = Field(ge=0)
    archived_at: datetime


class ResearchWorkflowView(BaseModel):
    workflow_id: str
    state: str
    progress: int = Field(ge=0, le=100)
    result: dict[str, Any] | None = None
    execution_status: Literal[
        "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TERMINATED", "TIMED_OUT"
    ]
    retryable: bool
    correlation_id: str | None = None


class ResearchWorkflowCancel(StrictRequestModel):
    reason: str = Field(min_length=10, max_length=2000)


class ResearchWorkflowRetry(StrictRequestModel):
    idempotency_key: str = Field(
        min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$"
    )
    reason: str = Field(min_length=10, max_length=2000)


class ResearchWorkflowLogEntry(BaseModel):
    event_id: int = Field(ge=1)
    occurred_at: datetime
    level: Literal["info", "warning", "error"]
    message: str


ProviderDriver = Literal[
    "fake", "openai_compatible", "ollama", "anthropic", "gemini", "generic_rest"
]
DataPolicy = Literal["local_only", "remote_allowed", "remote_after_redaction"]


class ProviderWrite(StrictRequestModel):
    slug: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=2, max_length=180)
    driver_type: ProviderDriver
    endpoint: str | None = Field(default=None, max_length=2000)
    enabled: bool = False
    location: Literal["local", "remote"]
    authentication_scheme: Literal["none", "bearer", "api_key", "oauth"] = "none"
    secret_reference: str | None = Field(
        default=None, max_length=240, pattern=r"^[a-zA-Z0-9_.:-]+$"
    )
    data_policy: DataPolicy
    residency_policy: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    concurrency_limit: int = Field(default=1, ge=1, le=1000)
    requests_per_minute: int = Field(default=60, ge=1, le=100_000)

    @model_validator(mode="after")
    def validate_endpoint_and_auth(self) -> ProviderWrite:
        if self.driver_type == "fake":
            if self.endpoint is not None or self.location != "local":
                raise ValueError("fake providers are local and have no endpoint")
            if self.authentication_scheme != "none" or self.secret_reference is not None:
                raise ValueError("fake providers cannot use credentials")
            if self.data_policy != "local_only":
                raise ValueError("fake providers must use local_only data policy")
            return self
        if not self.endpoint:
            raise ValueError("non-fake providers require an endpoint")
        parts = urlsplit(self.endpoint)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("provider endpoint must be an absolute HTTP(S) URL without credentials or query")
        if self.location == "remote" and parts.scheme != "https":
            raise ValueError("remote provider endpoints require HTTPS")
        if self.authentication_scheme == "none" and self.secret_reference is not None:
            raise ValueError("credential-free providers cannot set a secret reference")
        if self.authentication_scheme != "none" and not self.secret_reference:
            raise ValueError("authenticated providers require a mounted secret reference")
        if self.location == "remote" and self.data_policy == "local_only":
            raise ValueError("remote providers cannot satisfy local_only data policy")
        return self


class ProviderUpdate(ProviderWrite):
    expected_version: int = Field(ge=1)


class ProviderView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: int
    slug: str
    name: str
    driver_type: ProviderDriver
    endpoint: str | None
    enabled: bool
    location: Literal["local", "remote"]
    authentication_scheme: str
    has_secret: bool
    data_policy: DataPolicy
    residency_policy: dict[str, Any]
    capabilities: dict[str, Any]
    concurrency_limit: int
    requests_per_minute: int
    health_status: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class AIModelWrite(StrictRequestModel):
    provider_id: UUID
    model_name: str = Field(min_length=1, max_length=240)
    display_name: str = Field(min_length=1, max_length=240)
    visible: bool = False
    enabled: bool = False
    model_version: str = Field(min_length=1, max_length=160)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    context_limit: int = Field(ge=256, le=10_000_000)
    output_limit: int = Field(ge=64, le=1_000_000)
    cost_policy: dict[str, Any] = Field(default_factory=dict)
    data_policy_override: DataPolicy | None = None

    @model_validator(mode="after")
    def enabled_models_are_visible(self) -> AIModelWrite:
        if self.enabled and not self.visible:
            raise ValueError("enabled models must be visible")
        return self


class AIModelUpdate(AIModelWrite):
    expected_version: int = Field(ge=1)


class AIModelView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: int
    provider_id: UUID
    model_name: str
    display_name: str
    visible: bool
    enabled: bool
    model_version: str
    capabilities: dict[str, Any]
    context_limit: int
    output_limit: int
    cost_policy: dict[str, Any]
    data_policy_override: DataPolicy | None
    created_at: datetime
    updated_at: datetime


class AIUsageView(BaseModel):
    id: UUID
    workflow_id: str
    activity_id: str
    task_type: str
    provider_id: UUID
    provider_name: str
    model_id: UUID
    model_name: str
    prompt_template_id: UUID
    request_hash: str
    response_hash: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost: dict[str, Any]
    redaction_summary: dict[str, Any]
    correlation_id: str
    created_at: datetime


EditorialTaskType = Literal[
    "script_writer",
    "script_verifier",
    "storyboard",
    "topic_qualifier",
    "research_query_planner",
    "evidence_synthesizer",
    "evidence_reviewer",
]


class ProviderModelDiscoveryView(BaseModel):
    provider_id: UUID
    provider_name: str
    driver_type: str
    models: list[str]
    model_count: int


class TaskModelAssignmentWrite(StrictRequestModel):
    task_type: EditorialTaskType
    primary_model_id: UUID
    fallback_model_ids: list[UUID] = Field(default_factory=list, max_length=5)
    routing_policy: dict[str, Any] = Field(default_factory=dict)
    budget_policy: dict[str, Any] = Field(default_factory=dict)
    comment: str = Field(min_length=3, max_length=2000)

    @model_validator(mode="after")
    def unique_models(self) -> TaskModelAssignmentWrite:
        if self.primary_model_id in self.fallback_model_ids:
            raise ValueError("primary model cannot also be a fallback")
        if len(set(self.fallback_model_ids)) != len(self.fallback_model_ids):
            raise ValueError("fallback models must be unique")
        return self


class TaskModelAssignmentView(BaseModel):
    id: UUID
    task_type: EditorialTaskType
    assignment_version: int
    primary_model_id: UUID
    fallback_model_ids: list[UUID]
    routing_policy: dict[str, Any]
    budget_policy: dict[str, Any]
    created_by: UUID
    created_at: datetime
    comment: str
    active: bool


class PromptTemplateWrite(StrictRequestModel):
    template_key: str = Field(
        min_length=3, max_length=160, pattern=r"^[a-z][a-z0-9_.-]*$"
    )
    task_type: EditorialTaskType
    system_instructions: str = Field(min_length=20, max_length=100_000)
    template: str = Field(min_length=20, max_length=200_000)
    input_schema: dict[str, Any]
    response_schema: dict[str, Any]
    comment: str = Field(min_length=3, max_length=2000)

    @model_validator(mode="after")
    def required_placeholders(self) -> PromptTemplateWrite:
        if "{{structured_input_json}}" not in self.template:
            raise ValueError("prompt template must contain {{structured_input_json}}")
        if "{{response_schema_json}}" not in self.template:
            raise ValueError("prompt template must contain {{response_schema_json}}")
        return self


class PromptTemplateView(BaseModel):
    id: UUID
    template_key: str
    task_type: EditorialTaskType
    template_version: int
    system_instructions: str
    template: str
    input_schema: dict[str, Any]
    response_schema: dict[str, Any]
    content_hash: str
    created_by: UUID
    created_at: datetime
    comment: str
    active: bool


class OpportunityListItem(BaseModel):
    id: UUID
    version: int
    subject_profile_id: UUID
    title: str
    summary: str
    editorial_rationale: str
    policy_snapshot: dict[str, Any]
    decision: Literal["pending", "approved", "rejected", "deferred"]
    grouping_reason: list[str]
    estimated_cost: dict[str, Any]
    score: int | None
    score_version: int | None = None
    score_components: dict[str, float]
    score_penalties: dict[str, float]
    score_weights: dict[str, float] = Field(default_factory=dict)
    score_reasoning: list[str]
    ai_qualification: dict[str, Any] | None = None
    source_count: int = Field(ge=0)
    snapshot_count: int = Field(ge=0)
    research_state: str | None = None
    created_at: datetime

    @field_validator("grouping_reason", mode="before")
    @classmethod
    def normalize_legacy_grouping_reason(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return [f"{key}: {item}" for key, item in sorted(value.items())]
        if isinstance(value, str):
            return [value]
        return value


class ArchivedOpportunityListItem(OpportunityListItem):
    archived_at: datetime


class DossierListItem(BaseModel):
    id: UUID
    opportunity_id: UUID
    dossier_version: int
    version: int
    status: str
    executive_summary: str
    completion_evaluation: dict[str, Any]
    created_at: datetime
    automatic_continuation: AutomaticContinuation | None = None


class ClaimLedgerItem(BaseModel):
    id: UUID
    normalized_statement: str
    claim_type: str
    confidence: int
    status: str
    risk: str
    central: bool
    coverage_unit_ids: list[str] = Field(default_factory=list)
    version: int
    evidence: list[dict[str, Any]]


class DossierDetail(DossierListItem):
    explanation_plan: list[dict[str, Any]] = Field(default_factory=list)
    chronology: list[dict[str, Any]]
    unresolved_questions: list[str]
    alternative_explanations: list[str]
    source_quality_notes: list[str]
    safe_conclusions: list[str]
    prohibited_overstatements: list[str]
    proposed_angles: list[str]
    claims: list[ClaimLedgerItem]
    ai_evidence_assessment: dict[str, Any] | None = None


class SourceSnapshotView(BaseModel):
    id: UUID
    snapshot_number: int
    content_hash: str
    mime_type: str
    byte_size: int
    retrieved_at: datetime
    extraction_metadata: dict[str, Any]
    injection_markers: list[str]
    semantic_chunk_count: int = Field(ge=0)


class SourceRelationshipView(BaseModel):
    id: UUID
    source_document_id: UUID
    related_source_document_id: UUID
    relationship: str
    reason: str
    confidence: int
    created_at: datetime


class SourceBrowserItem(BaseModel):
    id: UUID
    canonical_url: str
    title: str
    author: str | None
    publisher: str | None
    source_type: str
    publication_at: datetime | None
    event_at: datetime | None
    reputation: dict[str, Any]
    domain: str
    snapshots: list[SourceSnapshotView]
    relationships: list[SourceRelationshipView]


class SourcePreview(BaseModel):
    source_document_id: UUID
    source_snapshot_id: UUID
    content_hash: str
    text: str
    truncated: bool
    untrusted: bool = True


class SourceRelationshipWrite(StrictRequestModel):
    source_document_id: UUID
    related_source_document_id: UUID
    relationship: Literal["cites", "derived_from", "repeats", "near_duplicate", "independent"]
    reason: str = Field(min_length=10, max_length=4000)
    confidence: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def distinct_sources(self) -> "SourceRelationshipWrite":
        if self.source_document_id == self.related_source_document_id:
            raise ValueError("source relationship endpoints must be distinct")
        return self


class SourceIndexStart(StrictRequestModel):
    idempotency_key: str = Field(min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$")


class ReviewDecision(StrictRequestModel):
    decision: Literal["approved", "rejected"]
    expected_version: int = Field(ge=1)
    comment: str = Field(min_length=3, max_length=4000)
    override_reason: str | None = Field(default=None, min_length=20, max_length=4000)


class ScriptGenerationStart(StrictRequestModel):
    dossier_id: UUID
    expected_dossier_version: int = Field(ge=1)
    sensitivity: Literal["public", "internal", "sensitive", "restricted"] = "internal"
    idempotency_key: str = Field(
        min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$"
    )


class ExistingResearchScriptImportStart(StrictRequestModel):
    opportunity_id: UUID
    title: str = Field(min_length=1, max_length=300)
    script_text: str = Field(min_length=200, max_length=50_000)
    sensitivity: Literal["public", "internal", "sensitive", "restricted"] = "internal"
    idempotency_key: str = Field(
        min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$"
    )

    @field_validator("title", "script_text")
    @classmethod
    def nonempty_import_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must contain non-whitespace text")
        return normalized


class StoryboardGenerationStart(StrictRequestModel):
    script_version_id: UUID
    sensitivity: Literal["public", "internal", "sensitive", "restricted"] = "internal"
    idempotency_key: str = Field(
        min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$"
    )


class ScriptVerificationStart(StrictRequestModel):
    expected_version: int = Field(ge=1)
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    sensitivity: Literal["public", "internal", "sensitive", "restricted"] = "internal"
    idempotency_key: str = Field(
        min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$"
    )


class ScriptRegenerationStart(StrictRequestModel):
    expected_version: int = Field(ge=1)
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    segment_keys: list[str] = Field(min_length=1, max_length=50)
    instruction: str = Field(min_length=10, max_length=4000)
    sensitivity: Literal["public", "internal", "sensitive", "restricted"] = "internal"
    idempotency_key: str = Field(
        min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$"
    )

    @model_validator(mode="after")
    def unique_segment_keys(self) -> "ScriptRegenerationStart":
        if len(set(self.segment_keys)) != len(self.segment_keys):
            raise ValueError("segment keys must be unique")
        if any(not value or len(value) > 120 for value in self.segment_keys):
            raise ValueError("segment keys must be between 1 and 120 characters")
        return self


class EditorialApprovalWrite(StrictRequestModel):
    expected_version: int = Field(ge=1)
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    comment: str = Field(min_length=10, max_length=4000)


class ScriptAnnotationWrite(StrictRequestModel):
    text: str = Field(min_length=1, max_length=20_000)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=1)
    kind: Literal["fact", "inference", "opinion", "quote", "editorial"]
    claim_ids: list[UUID] = Field(default_factory=list, max_length=100)
    evidence_excerpt_id: UUID | None = None

    @model_validator(mode="after")
    def ordered_offsets(self) -> "ScriptAnnotationWrite":
        if self.end_offset <= self.start_offset:
            raise ValueError("annotation end offset must follow its start offset")
        return self


class ScriptSegmentWrite(StrictRequestModel):
    segment_key: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$")
    segment_type: Literal[
        "hook", "thesis", "context", "evidence", "counterevidence",
        "conclusion", "uncertainty", "call_to_action",
    ]
    narration: str = Field(min_length=1, max_length=20_000)
    presentation_purpose: str = Field(min_length=1, max_length=2000)
    duration_seconds: float = Field(gt=0, le=900)
    citation_display: dict[str, Any]
    annotations: list[ScriptAnnotationWrite] = Field(min_length=1, max_length=200)
    locked: bool = False


class ScriptVersionEdit(StrictRequestModel):
    expected_version: int = Field(ge=1)
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=300)
    segments: list[ScriptSegmentWrite] = Field(min_length=8, max_length=200)
    comment: str = Field(min_length=10, max_length=4000)


class SceneEdit(StrictRequestModel):
    expected_scene_version: int = Field(ge=1)
    expected_storyboard_version: int = Field(ge=1)
    scene_spec: dict[str, Any]
    comment: str = Field(min_length=10, max_length=4000)


class SceneLockWrite(StrictRequestModel):
    expected_version: int = Field(ge=1)
    locked: bool
    comment: str = Field(min_length=10, max_length=4000)


class SceneAlternativeGenerationStart(StrictRequestModel):
    expected_scene_version: int = Field(ge=1)
    expected_storyboard_version: int = Field(ge=1)
    instruction: str = Field(min_length=10, max_length=4000)
    sensitivity: Literal["public", "internal", "sensitive", "restricted"] = "internal"
    idempotency_key: str = Field(
        min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$"
    )


class SceneAlternativeSelect(StrictRequestModel):
    expected_scene_version: int = Field(ge=1)
    expected_storyboard_version: int = Field(ge=1)
    comment: str = Field(min_length=10, max_length=4000)


class SceneAlternativeView(BaseModel):
    id: UUID
    scene_id: UUID
    base_scene_version_id: UUID
    alternative_number: int
    scene_spec: dict[str, Any]
    content_hash: str
    model_id: UUID
    prompt_template_id: UUID
    instruction: str
    workflow_id: str
    created_at: datetime
    current_base: bool


class WorkflowNodeRequirement(StrictRequestModel):
    class_type: str = Field(min_length=1, max_length=240)
    version: str = Field(min_length=1, max_length=160)


class WorkflowModelRequirement(StrictRequestModel):
    name: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkflowTypedInput(StrictRequestModel):
    name: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    node_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_.:-]+$")
    input_name: str = Field(min_length=1, max_length=160)
    value_type: Literal["string", "integer", "number", "boolean", "seed"]
    required: bool = True


class ComfyWorkflowWrite(StrictRequestModel):
    workflow_key: str = Field(min_length=2, max_length=160, pattern=r"^[a-z][a-z0-9_.-]+$")
    purpose: str = Field(min_length=1, max_length=160)
    api_workflow: dict[str, Any]
    required_nodes: list[WorkflowNodeRequirement] = Field(min_length=1, max_length=500)
    required_models: list[WorkflowModelRequirement] = Field(default_factory=list, max_length=200)
    typed_inputs: list[WorkflowTypedInput] = Field(default_factory=list, max_length=100)
    output_contract: dict[str, Any]
    allowed_resolutions: list[dict[str, int]] = Field(min_length=1, max_length=20)
    allowed_durations: dict[str, float]
    approve: bool = False
    comment: str = Field(default="", max_length=2000)


class ComfyWorkflowView(BaseModel):
    id: UUID
    workflow_key: str
    version_number: int
    purpose: str
    api_workflow: dict[str, Any]
    content_hash: str
    required_nodes: list[dict[str, Any]]
    required_models: list[dict[str, Any]]
    typed_inputs: list[dict[str, Any]]
    output_contract: dict[str, Any]
    allowed_resolutions: list[dict[str, int]]
    allowed_durations: dict[str, float]
    approval_state: str
    approved_by: UUID | None
    approved_at: datetime | None
    created_by: UUID
    created_at: datetime
    comment: str
    active: bool


class VoiceProfileWrite(StrictRequestModel):
    profile_key: str = Field(min_length=2, max_length=160, pattern=r"^[a-z][a-z0-9_.-]+$")
    provider_type: Literal["fake", "voicebox_rest", "voicebox_ws"]
    endpoint: str | None = Field(default=None, max_length=2000)
    voice_id: str = Field(min_length=1, max_length=240)
    language: str = Field(min_length=2, max_length=40)
    engine: str = Field(min_length=1, max_length=160)
    model_version: str = Field(min_length=1, max_length=160)
    delivery: dict[str, Any] = Field(default_factory=dict)
    pronunciation: dict[str, Any] = Field(default_factory=dict)
    output_settings: dict[str, Any]
    consent: dict[str, Any]
    enabled: bool = True
    comment: str = Field(default="", max_length=2000)


class VoiceProfileView(VoiceProfileWrite):
    id: UUID
    version_number: int
    content_hash: str
    created_by: UUID
    created_at: datetime
    active: bool


class MediaProductionStart(StrictRequestModel):
    storyboard_version_id: UUID
    expected_storyboard_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_tier: Literal["preview", "full"] = "preview"
    workflow_key: str = Field(default="fixture-scene", min_length=2, max_length=160)
    voice_profile_key: str = Field(default="fixture-narrator", min_length=2, max_length=160)
    width: int = Field(default=854, ge=64, le=3840)
    height: int = Field(default=480, ge=64, le=2160)
    fps: int = Field(default=24, ge=1, le=60)
    idempotency_key: str = Field(min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$")


class MediaRegenerationStart(StrictRequestModel):
    instruction: str = Field(min_length=10, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$")


class MediaAssetView(BaseModel):
    id: UUID
    scene_version_id: UUID | None
    asset_kind: str
    object_key: str
    content_hash: str
    mime_type: str
    byte_size: int
    width: int | None
    height: int | None
    duration_seconds: float | None
    licence: dict[str, Any]
    generation_provenance: dict[str, Any]


class QAFindingView(BaseModel):
    id: UUID
    code: str
    verdict: Literal["pass", "warn", "fail"]
    message: str
    scene_version_id: UUID | None
    timecode_seconds: float | None
    details: dict[str, Any]
    override_policy: Literal["never", "reasoned"]
    overridden: bool
    override_reason: str | None


class MediaProductionView(BaseModel):
    id: UUID
    storyboard_version_id: UUID
    storyboard_hash: str
    workflow_id: str
    render_tier: str
    state: str
    settings: dict[str, Any]
    correlation_id: str
    created_at: datetime
    completed_at: datetime | None
    assets: list[MediaAssetView]
    render: dict[str, Any] | None
    manifest: dict[str, Any] | None
    qa: dict[str, Any] | None
    findings: list[QAFindingView]
    approval: dict[str, Any] | None
    automatic_continuation: AutomaticContinuation | None = None


class QAOverrideWrite(StrictRequestModel):
    reason: str = Field(min_length=10, max_length=4000)


class RenderApprovalWrite(StrictRequestModel):
    expected_render_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approved", "rejected"]
    comment: str = Field(min_length=1, max_length=4000)


class ScriptSummaryView(BaseModel):
    id: UUID
    dossier_id: UUID
    status: str
    version: int
    current_version_id: UUID
    title: str
    content_hash: str
    coverage_percent: int
    created_at: datetime


class ScriptDetailView(ScriptSummaryView):
    verification_report: dict[str, Any]
    segments: list[dict[str, Any]]
    automatic_continuation: AutomaticContinuation | None = None


class StoryboardSummaryView(BaseModel):
    id: UUID
    script_id: UUID
    status: str
    version: int
    current_version_id: UUID
    content_hash: str
    scene_count: int
    created_at: datetime


class StoryboardDetailView(StoryboardSummaryView):
    script_version_id: UUID
    scenes: list[dict[str, Any]]
    automatic_continuation: AutomaticContinuation | None = None
