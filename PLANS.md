# Production system execution plan

This is a living execution plan. It must remain usable by an engineer who has only this repository and the mission specification. Update progress, decisions, discoveries, and verification evidence whenever work advances.

## Purpose and observable outcome

An operator starting from a clean Docker host can configure providers and research subjects in the UI, discover and evidence a subject, approve a dossier and cited script, produce and review media, upload the exact approved artifact privately, and authorize publication separately. Every stage is durable, versioned, auditable, rerunnable, restart-safe, and recoverable.

The authoritative product requirements are the user-supplied mission brief. `docs/implementation-status.md` is a traceability ledger, not permission to narrow that brief.

## Progress

- [x] 2026-07-20: Confirmed the repository was empty and derived the gated delivery sequence.
- [x] 2026-07-20: Increment 0 foundation runtime acceptance gate passed; browser visual audit remains separately tracked.
- [x] 2026-07-20: Increment 1 discovery and evidence acceptance gate passed.
- [x] 2026-07-20: Increment 2 editorial production acceptance gate passed.
- [x] 2026-07-21: Increment 3 media production acceptance gate passed.
- [x] 2026-07-21: Increment 4 publishing mock/dry-run acceptance gate passed; real network use remains credential-gated.
- [x] 2026-07-21: Increment 5 optimization and hardening acceptance gate passed.
- [x] 2026-07-21: Final reproducible deployment, migration, regression, security, observability and recovery audit passed; real Google action remains operator-credential-gated.
- [x] 2026-07-21: Complete channel/subject lifecycle management passed: prefilled version-checked edits, audited soft deletion, schedule cleanup, dependency protection, responsive operator controls, and live validation.
- [x] 2026-07-21: Add a searchable Archived profiles restore workspace and replace placeholder live-opportunity scoring with evidence-derived, auditable score version 2 traces.

## Milestones

### Increment 0 — Foundation

Deliver the monorepo, pinned Compose topology, local auth/RBAC, optional TOTP-ready secret handling, PostgreSQL/Alembic, MinIO, Temporal, base API/UI, immutable audit events, versioned configuration, health checks, CI, and a durable probe workflow. The gate passes only when `docker compose up --build -d` becomes healthy and a running probe remains queryable and completable after the worker is recreated.

### Increment 1 — Discovery and evidence

Deliver profiles, scheduled search plans, safe acquisition, immutable snapshots, clustering/scoring, research plans, atomic claims/evidence, and dossier review. Validate against deterministic fixtures including duplicate and hostile-source cases.

### Increment 2 — Editorial production

Deliver the policy-enforcing AI gateway, registered model routing, prompts, cited script versions, verification, the side-by-side editor, SceneSpec schema, and storyboard versions. Prove unsupported factual narration cannot progress.

### Increment 3 — Media production

Deliver provider-neutral ComfyUI and Voicebox ports/adapters, registries, narration/alignment, Remotion rendering, FFmpeg validation, manifests, asset licences, and blocking QA.

### Increment 4 — Publishing

Deliver encrypted OAuth tokens, dry-run by default, resumable private upload, processing reconciliation, exact-version approval binding, captions/thumbnail, and a separate idempotent scheduling/publication transition.

### Increment 5 — Optimization and hardening

Deliver analytics, benchmark recommendations without automatic routing changes, freshness/corrections/originality policies, budgets, observability, backup/restore drill, SBOM, and security hardening.

## Architectural constraints

Domain entities and policies in `packages/core` import no web framework, ORM, Temporal SDK, provider SDK, or storage SDK. Application ports are defined inward and implemented by adapters. FastAPI controllers and Temporal workflows translate transport data into plain application inputs. External calls occur only in Temporal activities and use persisted idempotency keys.

PostgreSQL is authoritative for metadata and workflow-facing state; immutable large bodies and media live behind an S3 abstraction in MinIO. Source snapshots and audit events are append-only at both the application and database levels. Temporal uses separate `temporal` and `temporal_visibility` databases.

## Decisions

- 2026-07-20: Preserve the brief's increment order and refuse to advance past a failing mandatory gate.
- 2026-07-20: Use a modular monorepo with clean dependency direction; deployment services remain independently scalable without sharing framework models.
- 2026-07-20: Use explicit development secrets only to make a clean local clone bootable. Production operation requires replacement secret files and TLS.
- 2026-07-20: Treat optional-profile capability as unavailable until its real behavior and tests exist; never return fake success.
- 2026-07-21: Keep Google credentials and plaintext refresh tokens inside a dedicated publisher egress gateway; return only ciphertext to the API and never place secrets in Temporal history.
- 2026-07-21: Give dry-run and real uploads distinct stable release keys so a safe rehearsal never consumes or aliases the real external side effect.
- 2026-07-21: Expose user-requested profile deletion as recoverable archival. Preserve linked workflows, evidence and audit history; archive subjects only after stopping their schedule, and block channel archival while active subjects remain.
- 2026-07-21: Restore archived profiles as disabled records. Require the parent channel to be active before restoring a subject, keep its schedule paused, and create a new audited version instead of erasing archive history.

## Discoveries

- 2026-07-20: The repository contained only an empty Git repository on branch `dev`.
- 2026-07-20: The host has Docker 29.1.3 and Compose 2.40.3; host Python is 3.14 and npm is absent, so validation must run in pinned containers.
- 2026-07-20: The installed ExecPlan skill omitted its required `references/PLANS.md`; this plan therefore embeds all context needed for continuation.
- 2026-07-21: Live score concentration at 53 is caused by placeholder constants in `persist-live-opportunities`: five positive dimensions and two penalties are constant, while the remaining formulas receive the same one-result/one-domain inputs for most clusters. PostgreSQL confirmed 175 active opportunities at 53 and only seven at other values.

## Validation and acceptance evidence

2026-07-20 foundation evidence:

- `docker compose config --quiet` resolved eight core services.
- `docker compose ps` showed all eight services healthy; only Caddy publishes a host port (`8090`).
- `curl http://localhost:8090/api/health/ready` returned ready for PostgreSQL, object storage, and Temporal; `/` returned HTTP 200.
- API/core tests: 7 passed. Workflow/core tests: 6 passed. Next.js 16.2.10 production build and TypeScript validation passed.
- Live bootstrap established an Argon2id-backed administrator session. A viewer received `403` for user administration and a mutation without CSRF received `403`.
- Live YAML configuration import/export produced an immutable active version. Direct PostgreSQL `UPDATE audit_events` and `DELETE configuration_versions` both failed with `<table> is append-only`.
- Workflow `durability-probe-increment-0-gate-20260720` remained queryable with the same start time after `workflow-worker` restart, completed, and reconciled a repeat completion without a duplicate side effect.
- `scripts/smoke-core.sh` and `scripts/verify-durable-workflow.sh` reproduced the HTTP readiness and durable-restart gates; the latter tolerates the short interval between container HTTP health and resumed Temporal polling.
- Browser visual/accessibility evidence is missing because host npm/npx is not installed; the production web build and HTTP response are not being treated as visual evidence.

2026-07-20 Increment 1 evidence:

- Migrations through `0005_workflow_controls` are live with 22 application tables, pgvector, and database immutability triggers for source/evidence/chunk/transition/provenance/workflow-control records.
- Core, API, workflow and research suites contain 67 unique passing Python tests (19 core, 13 API, 6 workflow-worker and 29 research-worker). The Next.js production build and TypeScript gate pass with profile, Opportunity Board, source browser, dossier/claim ledger and workflow monitor screens.
- Core plus the nine-service research profile is healthy with private SearXNG, a separate Temporal research worker and self-hosted Firecrawl; all 17 services are healthy and only Caddy publishes a host port.
- Live `live-discovery` Temporal smoke executed four search strategies through private SearXNG, normalized and scored pending opportunities, and committed 20 append-only opportunity/source provenance rows in the observed run. The public result count is nondeterministic and this smoke is not a CI fixture.
- The public-source connector pins validated public DNS answers into the aiohttp connector and applies scheme/credential/port/domain, robots, redirect, TLS, MIME, byte, timeout, cookie and throttle controls. A live approval/acquisition drill stored a 79,383-byte GOV.UK raw object plus a 5,806-byte normalized object, persisted the SHA-256 snapshot with robots/DNS metadata, rejected a direct snapshot update, and reused the Temporal workflow on retry.
- Opportunity approval now records `DISCOVERY -> SHORTLISTED`; acquisition completion records `SHORTLISTED -> RESEARCHING`. Reject/defer decisions map to explicit interruption stages and every decision is version-checked and audited.
- `scripts/verify-discovery-fixture.sh` created a subject, generated a validated multi-strategy/falsification SearchPlan, deduplicated one of four findings, scored an opportunity with visible components/penalties, stored three raw and normalized MinIO snapshots, and produced a reviewable cited dossier.
- The dossier contained two independent direct supports (including primary evidence) and one contradiction, exact excerpts, source URLs and SHA-256 snapshot hashes. Retry preserved one dossier/idempotency result. Direct `UPDATE source_snapshots` failed with `source_snapshots is append-only`.
- Reviewer/admin claim and dossier approval succeeded with optimistic versions and audit records; a viewer review attempt returned `403`.
- Temporal subject schedule reconciliation is runtime-verified: two manual schedule triggers produced distinct run-scoped idempotency records, and disabling the subject paused the same schedule with both reconciliations audited.
- A live GOV.UK immutable snapshot produced dossier `bc55a4fe-de24-4790-b70e-b3c5a693630c` with two exact-offset claims. The configured high-risk rule correctly blocked completion for one independent/non-primary source; approval returned `409` without an override and succeeded only with a reasoned audited override. Retrying the start returned the same workflow and left exactly one dossier, idempotency row and `RESEARCHING -> DOSSIER_REVIEW` transition.
- The same snapshot was indexed through the authorized `source-semantic-index` workflow into nine immutable, stable-offset pgvector chunks using local `feature-hash-v1` embeddings. Retry preserved one idempotency record, vector self-distance was zero, the source-browser API exposed snapshot/chunk metadata, and normalized preview text was explicitly marked untrusted.
- Trafilatura 2.1.0 HTML, pypdf 6.14.2 PDF, canonical JSON, defused XML, bounded CSV and isolated text extractors are implemented. Tests cover injection retention, active-element removal, encrypted/malformed PDF rejection, XML entities, structured nesting and CSV normalization limits. A live 15,877-byte GOV.UK JSON source produced canonical normalized JSON and 22 immutable semantic chunks through Temporal/MinIO/PostgreSQL.
- Firecrawl is pinned by image digest at upstream build SHA `93387da4fbdf9de199649086efc637257e630397`. Its internal Playwright browser cannot open a direct public socket (`ENETUNREACH`), its DNS-pinning proxy returned `403` for loopback and metadata targets, and public CONNECT returned `200`. A live JS-only source produced 89 direct characters, 1,000 rendered/normalized characters, verified separate raw/rendered/composite hashes, two semantic chunks, and exactly one workflow/snapshot/run/idempotency record on replay.
- Redirect traversal now preserves an origin's explicit directory slash and rejects HTTPS-to-HTTP downgrades; both paths have regression tests and the downgrade was live-observed against the browser fixture.
- Multi-source dossier `3715eeb2-1d76-4068-bea7-22e730a3cec2` persisted an automatic `exact_duplicate` lineage edge for identical acquired evidence. All eight evidence links were marked non-independent, the completion rule reported zero independent supporting sources, and replay preserved one dossier, one relationship and one idempotency result. Deterministic tests separately prove the local cosine threshold produces `near_duplicate` lineage and prevents both related documents from counting independently.
- Robots policies are cached by origin and agent in a bounded one-hour LRU-style cache; concurrent checks coalesce behind a per-origin lock and each target path is still evaluated against the cached rules. Public acquisition rejects non-identity encodings before parsing. Controlled aiohttp fixtures verify allowed redirects, redirect exhaustion, MIME rejection, declared/streamed byte ceilings and encoding rejection; a mixed public/private DNS response rejects the entire answer set.
- Migration `0005_workflow_controls` adds immutable, non-secret workflow input provenance and parent lineage. Authenticated SSE through Caddy streamed workflow `live-discovery-control-gate-20260720202818`; operator cancellation reached `CANCELLED`, retry started child `live-discovery-retry-control-gate-20260720202818-retry`, repeated retry reconciled that child, and the child completed `OPPORTUNITY_REVIEW`. Exactly two control rows preserve the parent edge. Safe log history contains only whitelisted lifecycle/activity messages, and SSE releases its database session before remaining open.

2026-07-20 Increment 2 evidence:

- Migrations through `0010_scene_alternatives` are live with 39 application tables. Prompt, routing, usage, script, segment, claim-link, storyboard, scene-version, approval and generated-alternative records are append-only where required. A disposable empty database migrated from 0001 through 0010 and contained the `scene_alternatives_immutable` trigger.
- The provider-neutral gateway implements fake/CI, OpenAI-compatible, Ollama-native, Anthropic-style, Gemini-style and generic REST/JSON drivers. It validates configured/visible routes and one active prompt, enforces local/remote data policy before serialization, performs deterministic remote redaction, validates response schemas, bounds repair/retry/deadline behavior, uses fallback/circuit/concurrency/sliding-minute-rate controls, resolves only mounted secret basenames and persists safe usage/correlation/redaction records.
- The provider UI configures endpoints, mounted secret references, visible models, task routing and immutable prompts. It exposes prompt version comparison and the safe usage ledger, including redaction category/path/value hash without revealing removed values or credentials.
- Six isolated suites pass with 88 unique Python tests: 24 core, 7 AI gateway, 18 API, 1 foundation workflow-worker, 29 research-worker and 9 editorial-worker. The Next.js production image passed TypeScript validation and optimized build.
- Live supported workflow `script-generation-inc2-supported-20260720214553` produced eight strict segments, eight claim links, 100% central-claim coverage, no issues and exact-hash approval. Unsupported workflow `script-generation-inc2-unsupported-20260720215538` persisted a blocked draft with `unsupported_factual_statement` and `missing_evidence_excerpt`; approval returned `409`.
- A manual CTA edit created immutable script v2, independent verification created v3 with the same content hash and 100% coverage, and approval bound that exact v3 hash while v1/v2 remained intact. Selected regeneration workflow `script-regeneration-inc2-selection-202607202238` changed only `call_to_action`; the other seven segment hashes exactly matched the parent. Re-verification created v5 and exact-hash approval succeeded.
- The strict storyboard gate produced eight ordered, claim/source-linked SceneSpecs. A locked scene edit returned `409`; a one-scene edit cloned all eight scene records into storyboard v2 and invalidated aggregate approval. Generated-alternative workflow `scene-alternative-generation-inc2-alt-202607202249` stored an immutable candidate tied to the exact base scene. Selecting it cloned all eight scenes into storyboard v3: only `scene-001` changed, hashes for scenes 002–008 remained identical, the old candidate became historical and exact-hash approval succeeded.
- Viewer access to script/storyboard reads returned `200`; provider configuration and scene mutation returned `403`. AI model calls, version changes, approvals, workflow starts and alternative selection have correlation-linked audit records.

## Recovery and idempotence

2026-07-21 Increment 3 evidence:

- Migrations through `0013_narration_regen` are live. Core/API/editorial-worker/renderer tests and the Next.js optimized build pass.
- Approved storyboard hash `10aeed141148667cb201c6bf4cd7af51ed975abcbc775a6a5af9eb8a15728633` produced deterministic preview and 1080p H.264/AAC renders, aligned captions, source/chapter/thumbnail artifacts and immutable manifests.
- Enhanced FFmpeg QA checked stream/probe, duration, silence, black frames, freezes, caption reading rate, timing and visual overlap. Exact render/manifest approval `99d89998-4259-4b74-95c8-101991f4b4d8` was recorded only after mandatory checks passed.
- Scene and narration selection workflows produced new immutable assets while the parent approved production and every unselected scene/segment remained unchanged.

2026-07-21 Increment 4 evidence:

- Migration `0014_youtube_publishing` is live; 65 core/API tests and three publisher tests pass. The publishing profile, publisher worker and API are healthy, and the web UI passes TypeScript plus optimized production build.
- The dedicated gateway keeps the OAuth client secret and plaintext tokens behind publishing egress. Refresh tokens and resumable URIs are encrypted with the separately mounted key; OAuth state is single-use and time bounded.
- Live dry-run publication `b7c21cc2-b184-4a1b-9f25-130fbbf10739` received video ID `dry_271b9c7d04c`. Two starts with different client retry keys resolved to one publication, one video ID and one stable provider key. Caption/thumbnail attachment completed and reconciliation reached `processed` while privacy remained `private`.
- Changing metadata produced hash `f73415669b3bc23bd891b58160d06d1d6c5f446c1514366c580e3386a3eb2ba6`; upload returned `409` until that exact version receives a new approval. A real request returned `403` under the dry-run configuration. No Google video was created.

Compose stop/start must preserve named volumes. Migrations are forward-only and repeatable. External side effects use stable idempotency keys and reconciliation records. Never use `docker compose down -v` in normal operations. Backup/restore tooling has been exercised against the complete demo project and refuses to overwrite production targets.

2026-07-21 Increment 5 and final-audit evidence:

- Immutable analytics, model benchmark/recommendation review/application, source freshness, originality, corrections, budgets and operational-evidence APIs/UI are complete. Recommendation never mutates routing automatically.
- Assisted/supervised/trusted policy, sensitive-topic mandatory gates and required opportunity rationale are deterministic, UI-configurable and approval-bound.
- Local/TOTP/OIDC authentication, issuer-bound OIDC subjects, durable login throttling, Caddy body ceiling and nonce CSP are implemented.
- OTel collector, Prometheus, Grafana, Loki and Promtail are healthy; all Prometheus targets were up and trace/log ingestion was observed.
- A clean empty database migrated through `0018_operating_policy` to 75 tables with the immutable-trigger set.
- Full final suites passed: 44 core, 84 API, 7 AI gateway, 6 workflow, 29 research, 13 editorial/media, 3 publisher and 2 renderer tests; the web TypeScript and production build pass.
- Security audit proves one host port, internal private networks, read-only/capability-dropped application services and dedicated identity/provider/publishing egress membership.
- The final 26-image checksummed CycloneDX SBOM is `/srv/youtuber-backups/sbom-final-20260721T0818`; its 26-entry manifest SHA-256 is `0a0de726474b163584cfb9077c544683fc83b4c09690c4d194c8d6361589b5be`.
- The post-`0018` backup/restore artifact is `/srv/youtuber-backups/final-20260721T0822`; the disposable drill restored 75 tables, 148 audit events, eight source snapshots and 185 objects, removed its temporary targets and did not modify production. Real YouTube OAuth/upload was not invoked without operator-owned credentials.

2026-07-21 channel and subject lifecycle evidence:

- Every active channel and subject card exposes an accessible Edit action with a prefilled complete profile editor. Saves use the existing optimistic version check, preserve linked history and immediately refresh both the card list and channel workspace selector.
- Archive is an audited, version-checked soft deletion. Channel archival is blocked while active subjects remain; subject archival first reconciles its Temporal schedule to disabled/paused and records the resulting schedule state in the audit context.
- API regression passed with 93 tests. The Next.js TypeScript check and optimized production build passed, and the rebuilt API and web images are deployed.
- A disposable live channel was edited from version 1 to 2. Its disposable subject was edited from version 1 to 2, enabled, reconciled to an active `0 4 * * *` schedule, then archived; PostgreSQL confirmed `schedule_paused=true`, `enabled=false`, `deleted_at` set and version 4. The channel was then archived at version 3, disappeared from the workspace selector, and the selected scope safely reset to All channels.
- Desktop and 390-pixel browser checks confirmed the prefilled editors and dependency-aware archive dialog. The browser console reported zero errors and zero warnings; all 25 deployed services were running without unhealthy or starting containers.

2026-07-21 archived profiles and scoring-trace evidence:

- The Administration navigation now exposes a searchable Archived profiles workspace to readers. It filters by profile type and identifying content, exposes complete archived configuration on demand, and limits restore controls to editors and administrators.
- Restore is audited and optimistic-version checked. A restored channel remains disabled; a subject restore is blocked until its parent channel is active, and a restored subject remains disabled so its existing cron cannot run without an explicit later enable action.
- A disposable archived channel and subject were restored through the live UI in dependency order, verified as disabled, and archived again. PostgreSQL retained both restore and archive events; the subject was again archived before its parent channel.
- Live-discovery scoring now derives all seven positive components and three penalties from observed rank, topic/query coverage, source count/type/diversity, content specificity, publication freshness, purpose, domain rarity and prior-finding similarity. Stored reasons include the observed inputs rather than generic constant text.
- The append-only upgrade added score version 2 to all 191 legacy placeholder-scored opportunities and preserved every original score row. For 19 records without source links, the trace explicitly uses stored title/summary/subject signals with zero-source, unavailable-rank and unavailable-date inputs rather than inventing provenance.
- Before the upgrade, 175 active opportunities had the identical 53 trace. Current latest live-discovery scores span 30 integer values from 26 through 74; 19 round to 53 naturally, with varied component and reason traces, and no latest score retains the placeholder component set.
- API regression passed with 96 tests, research-worker regression passed with 40 tests, and the Next.js TypeScript and optimized production build gates passed. Rebuilt API, research-worker and web services are healthy in the 25-service deployment.
- Browser checks at desktop and 390 pixels confirmed archived search/empty states, dependency-aware restore controls and score-version-2 detail presentation. A live 51/100 finding showed seven observed components, three penalties and finding-specific reasons; the TubeFactory browser console reported zero errors and zero warnings.
