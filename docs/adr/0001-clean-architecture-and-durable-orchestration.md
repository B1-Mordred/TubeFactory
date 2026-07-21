# ADR 0001: Clean architecture and durable orchestration

- Status: accepted
- Date: 2026-07-20

## Context

The product must support interchangeable local/remote AI providers and stable ComfyUI, Voicebox, storage, research, and publishing interfaces while preserving restart-safe multi-day editorial workflows.

## Decision

Use inward-pointing clean-architecture dependencies. Domain policies and application ports live in `packages/core`; provider/framework code is an outer adapter. Use Temporal workflows only for orchestration and Temporal activities for every external, AI, research, media, and publishing operation. Persist application state and idempotency records outside Temporal history.

## Consequences

Business and policy tests run without infrastructure. Adapters are replaceable and provider payloads cannot leak into domain logic. The repository carries more explicit mapping and composition-root code, which is accepted because it protects long-lived editorial policy from volatile integrations.
