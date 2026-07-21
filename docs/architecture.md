# Architecture

## Dependency direction

The system uses ports and adapters. `packages/core` contains framework-independent entities, state-transition policies, and port protocols. Service application layers orchestrate these types. Infrastructure adapters implement ports for PostgreSQL, S3, Temporal, model providers, research fetchers, media engines, and YouTube. Delivery layers translate HTTP/WebSocket or Temporal payloads into application request objects.

Imports point inward:

```text
FastAPI / Temporal / SQLAlchemy / provider SDKs
                    -> interface adapters
                    -> application use cases and ports
                    -> domain entities and policies
```

Domain code never imports FastAPI, SQLAlchemy, Temporal, React, MinIO, ComfyUI, Voicebox, or YouTube types. Provider-specific payloads terminate at adapters. This makes local and remote providers interchangeable and keeps ComfyUI/Voicebox out of editorial policy.

## Runtime topology

Caddy is the only public edge. Web and API share the application network. PostgreSQL and MinIO are isolated on the data network. Temporal has an application-facing path and a data path. The optional research worker crosses the application, data and dedicated research-egress networks; private SearXNG is egress-only and has no path to the application/data networks. The editorial worker joins application/data plus a provider-egress boundary; no provider SDK or endpoint is reachable from the browser, and raw provider secrets are mounted references rather than API values. The disabled-by-default publisher worker alone joins `publishing-egress`; the API calls its authenticated internal OAuth gateway and therefore never receives plaintext refresh tokens or obtains general internet egress. GPU services use a separate internal boundary. Databases, object storage, Temporal, research services, provider workers and GPU services have no published host ports.

Temporal orchestrates long-running work. Every network, AI, media, or publishing operation is an activity with an explicit timeout, retry policy, correlation ID, and persisted idempotency key. Workflows coordinate durable state; they do not perform external I/O directly.

## Production state

PostgreSQL stores versioned metadata, approvals, immutable audit events, activity/idempotency records, and object hashes. MinIO stores immutable source bodies and media artifacts through an S3 port. Temporal history makes orchestration restart-safe but does not replace application audit/provenance records.

Publishing uses a two-gate state transition. An exact approved render plus immutable metadata version first receives a `private_upload` approval. The worker records a stable release key, persists an encrypted resumable session URI before transmitting video bytes, and always creates the YouTube resource with `privacyStatus=private`. Captions, thumbnail and processing state attach to that publication record. A different `public_release` approval, bound to the same render and metadata hashes, is required before a future `publishAt` can be set. Metadata edits produce a new hash and cannot reuse either approval. No workflow calls YouTube delete or replaces a video resource.

## Policy boundaries

Search planning, scoring, evidence completion, hostile-content handling, script evidence coverage, SceneSpec validation, operating profiles, optimization ranking, budget decisions, publishing bindings and state transitions are pure and testable without a database, web server, workflow engine or provider. Research, editorial/media and publisher workers are outbound adapters; SQL and object-storage effects occur inside bounded activities or API application transactions. The delivery routers translate authenticated requests and record audit/correlation context but do not grant provider tools to domain policy or source-processing models.
