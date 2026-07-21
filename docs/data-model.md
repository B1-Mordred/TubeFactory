# Data model

All identifiers are UUIDs and timestamps are UTC. Mutable aggregates use optimistic version numbers. Recoverable records use `deleted_at`; immutable records never do. Large byte bodies are addressed by object key plus SHA-256 hash rather than stored in PostgreSQL.

## Foundation tables

- `users`: local/OIDC identity, Argon2id hash, role, optional encrypted TOTP secret, enabled state, and optimistic version.
- `audit_events`: append-only actor/action/target/correlation records with redacted JSON context and a hash-chain link.
- `configuration_versions`: immutable versioned YAML/JSON-compatible configuration documents, schema version, author, and activation status.
- `idempotency_records`: operation scope/key, request hash, status, external identity, and replay-safe result.

## Discovery and evidence tables

- `channel_profiles`, `subject_profiles`: versioned, soft-deletable editorial identity and monitoring policy.
- `opportunities`, `opportunity_scores`: required editorial rationale and deterministic policy snapshot, review decision, plus append-only component-level score versions.
- `research_runs`: Temporal identity, structured research plan, progress and correlation identity.
- `source_documents`, `source_snapshots`: canonical source identity and immutable content-addressed acquisition versions.
- `evidence_excerpts`, `source_relationships`: exact anchored quotes and explicit source lineage.
- `research_dossiers`, `claims`, `claim_evidence`: versioned review aggregates and append-only support/contradiction/context links.
- `workflow_transitions`: append-only persisted state changes with actor, reason and correlation identity.

Database triggers reject `UPDATE` and `DELETE` against audit events, configuration versions, opportunity scores, source snapshots, semantic chunks, evidence excerpts, claim-evidence links and workflow transitions.

## Editorial production tables

- `providers`, `models`: versioned endpoint/model configuration with only mounted secret references, location/data policy, visible/enabled tasks, capacity and health metadata.
- `task_model_assignments`, `task_model_assignment_heads`: immutable routing versions plus the explicit active head; primary/fallback choice never changes automatically.
- `prompt_templates`, `prompt_template_heads`: immutable prompt instructions/templates/input and response schemas plus the explicit active head.
- `ai_usage_records`: append-only request/response hashes, model/prompt identity, token/latency/cost metadata, deterministic redaction summary and correlation identity.
- `scripts`, `script_versions`, `script_segments`, `segment_claims`: mutable aggregate head over immutable cited drafts, exact parent lineage, writer/verifier/prompt identity, sentence offsets, evidence excerpt links, coverage and verification reports.
- `storyboards`, `storyboard_versions`, `scenes`, `scene_versions`: mutable aggregate heads over immutable complete storyboards and strict SceneSpec versions.
- `scene_alternatives`: immutable model-generated candidates tied to the exact base scene version; selection creates a new complete storyboard/scene version set rather than mutating the candidate.
- `approvals`: append-only decisions bound to exact target UUID, version number and SHA-256 hash.

Database triggers reject changes to routing/prompt/usage/script/segment/claim-link/storyboard/scene-version/alternative/approval records. Script and storyboard aggregate status is mutable; immutable version and approval records are the authority for downstream gates.

## Media production tables

- `comfy_workflow_versions`, `voice_profile_versions`: immutable, reviewed provider contracts plus explicit active heads.
- `media_productions`, `media_assets`, `narration_segments`: durable production aggregate and content-addressed visual/audio/caption/thumbnail artifacts with licence and generation provenance.
- `production_manifests`, `production_renders`: immutable scene/source/timing manifests and exact Remotion/FFmpeg render identities.
- `qa_reports`, `qa_findings`, `qa_overrides`: deterministic media checks, policy snapshots, timecodes and reasoned reviewer exceptions.

## Publishing tables

- `youtube_connections`: channel identity, least-privilege scopes, status and version; refresh tokens are stored only as Fernet ciphertext plus a non-secret fingerprint.
- `youtube_oauth_states`: short-lived, single-use, hashed OAuth CSRF state bound to initiating user, channel and redirect URI.
- `publishing_configuration_versions`, `publishing_configuration_head`: immutable admin versions with dry-run as the default and explicit real-upload enablement.
- `publish_metadata_versions`: immutable title, composed description/source list, chapters, tags, language, audience, caption/thumbnail artifact hashes and synthetic-media disclosure bound to one render.
- `publication_approvals`: append-only `private_upload` or `public_release` decisions carrying both exact render and metadata hashes.
- `publications`: durable upload aggregate, stable idempotency key, encrypted resumable session URI, returned YouTube video ID, byte progress, caption/thumbnail state and reconciled processing resource.
- `publication_schedules`: immutable future `publishAt` requests bound to the separate release approval and a stable schedule key.

Configuration, metadata, publication approval and schedule versions are database-immutable. The upload aggregate remains mutable only for optimistic, audited provider progress; uniqueness constraints prevent a second publication or schedule for the same side-effect key.

## Identity and operational tables

- `authentication_rate_limits`: durable normalized login-failure windows and block time; rows clear only after successful authentication or window reset.
- `oidc_configuration_versions`, `oidc_configuration_heads`, `oidc_authentication_states`: immutable write-only-secret identity configuration plus one-use PKCE/state/nonce transactions.
- `analytics_metric_snapshots`: append-only metric periods, dimensions, source and canonical content hashes.
- `model_benchmark_runs`, `model_benchmark_results`, `model_recommendations`, `model_recommendation_decisions`: immutable candidate evidence, ranked primary/fallback recommendation and separate review; route application creates a normal immutable assignment version.
- `source_freshness_checks`, `originality_reports`, `correction_records`: release gates and changed/retracted-evidence impact/proposal lineage.
- `budget_policy_versions`, `budget_policy_heads`, `budget_usage_records`: versioned limits and idempotent usage decisions.
- `operational_evidence`: append-only backup, restore, SBOM, security and observability evidence with canonical and optional artifact hashes.

Temporal owns separate `temporal` and `temporal_visibility` databases. It does not share the application schema.
