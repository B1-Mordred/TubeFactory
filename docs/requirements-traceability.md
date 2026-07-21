# Requirements traceability

Last updated: 2026-07-21. This matrix maps the complete mission to authoritative implementation evidence. `Verified` means the implementation and its stated evidence were inspected; credential-dependent real Google actions remain an operator acceptance step and are never simulated.

| Requirement area | Increment | Current state | Completion evidence required |
| --- | ---: | --- | --- |
| UI-configurable operations and versioned YAML/JSON | 0–5 | Verified: typed feature forms plus versioned generic YAML/JSON registry/import/export | API strict-schema, role and production-build gates |
| Claim/evidence coverage blocks unsupported narration | 1–2 | Verified | Scenario 3 persisted sentence-level issues and exact-hash approval returned `409`; supported script reached 100% coverage |
| Persistent/versioned/auditable/rerunnable/restart-safe stages | 0–5 | Verified | State-policy tests, immutable version histories, worker restart, retry lineage and external-side-effect replay gates |
| Private upload then separately authorized release | 4 | Verified in deterministic mock; real adapter implemented and gated | Mock retry produced one private video; dual exact-hash approval and schedule adapter tests; real mode `403` while disabled |
| Interchangeable AI providers with pre-serialization data policy | 2 | Verified | Six driver contracts, local/remote policy fixtures, deterministic redaction ledger, configured/visible routing and safe secret resolver tests |
| Stable ComfyUI and Voicebox boundaries | 3 | Verified | Fake/real-adapter contracts, typed allowlists, caching, REST/WebSocket and selective regeneration live gate |
| No fake controls or silent scope reduction | All | Verified: capability endpoint and all delivered UI controls call real APIs; real upload is explicitly disabled by default | UI/API contract audit |
| Pinned packages and containers | All | Verified | Lockfiles, resolved image tags/digests and checksummed CycloneDX inventory |
| Compose profiles and split GPU deployment | 0–3 | Verified in resolved topology; GPU services support local profile or authenticated LAN endpoints without making NVIDIA a core requirement | Compose, health and network exposure audit; site operator validates its LAN endpoint |
| Local auth, Argon2id, TOTP, OIDC and API RBAC | 0/5 | Verified | TOTP/recovery fixtures, signed RSA OIDC fixture, PKCE/state/nonce implementation, throttling and role matrix |
| Complete core domain model | 1–5 | Verified through migration `0018` | Clean empty-database migration: 75 tables and immutable trigger audit |
| Legal production state machine and explicit return | 0–4 | Pure state policy unit-tested | Persisted transition service, authorization/gate rules, full transition property tests |
| Subject monitoring, SearchPlan, clustering and transparent scoring | 1 | Fixture and durable live SearXNG discovery verified with append-only source provenance; Temporal schedule repeat/pause gate verified | Scenario 1 and stable adapter contract |
| Deterministic safe acquisition and normalization | 1 | Verified: GOV.UK JSON and JavaScript browser drills plus controlled redirect/MIME/size/encoding fixtures; robots cache/coalescing, mixed-answer DNS rejection, TLS downgrade rejection, bounded multi-format extraction, immutable raw/rendered/normalized objects and replay reconciliation | Re-run adapters when dependency pins or network policy change |
| Evidence-first research, atomic claims and dossier review | 1 | Verified: fixture/live claim ledgers, support/contradiction, exact and semantic duplicate lineage, independence blocking, high-risk completion rules, authorized review and reasoned override audit | Re-run Scenario 2 when policy defaults change |
| Script generation, coverage and independent verification | 2 | Verified | Supported/unsupported live workflows, exact claim/evidence links, 100% coverage, immutable edit/reverify, lock preservation and selected-regeneration hash comparison |
| Storyboard and strict SceneSpec | 2 | Verified | Strict schema/policy tests, live lock rejection, full-version scene clone, durable immutable alternative generation/selection and unchanged-scene hash proof |
| ComfyUI registry and deterministic adapter | 3 | Verified | Fake/live adapter contracts, cache keys, node/model allowlists and selective scene gate |
| Voicebox/TTS, mastering and caption alignment | 3 | Verified | REST/WebSocket/fake contracts, stable chunks/cache/mastering and aligned fixture captions |
| Remotion assembly, FFmpeg validation and QA | 3 | Verified | Preview/full deterministic renders, manifest/probe inspection, QA gate and exact hash approval |
| YouTube OAuth/private upload/scheduling | 4 | Verified in mock; real network path requires operator-owned OAuth credentials | Scenario 9 idempotent private mock, scenario 11 changed-hash rejection, encrypted OAuth/session tests, real-disabled gate and documented operator drill |
| Operating profiles and sensitive-topic deterministic policy | 1–5 | Verified | Assisted/supervised/trusted matrices, sensitive fail-closed fixtures, rationale and approval-bound policy snapshots |
| AI gateway and benchmark optimizer | 2/5 | Verified | Six driver contracts and controlled two-model recommendation; review and application are separate and never automatic |
| Complete responsive UI and live progress controls | 0–5 | Verified by TypeScript/optimized build and live HTTP/SSE routes | Browser/site visual regression remains an operator deployment check for local branding/content |
| Security, egress, secrets and graceful recovery | 0–5 | Verified | Mounted/encrypted secrets, CSRF/RBAC/throttling/CSP/body ceiling, SSRF guards, read-only/cap-drop containers, network/port audit and SBOM |
| Backup/restore and observability | 5 | Verified | Complete PostgreSQL+MinIO restore drill; all Prometheus targets up, Loki query and collector traces observed, Grafana healthy |
| Disabled FakeBuster sample | 1 | Shipped as German/English YAML examples with automatic publication false | Import/export test proving disabled and non-publishing defaults |
| Final clean-host operator journey | Final | Reproducible and fixture-verified; real Google step credential-gated | Operator guide covers UI-only journey, deterministic mock private upload, backup/restore and real OAuth handoff without DB edits |

The twelve required test scenarios are tracked as explicit gates in the relevant rows. Passing narrower unit tests cannot substitute for those end-to-end scenarios.
