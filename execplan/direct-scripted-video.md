# Implement direct scripted-video production

This ExecPlan is a living document. The sections `Progress`,
`Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`
must be kept current while work proceeds.

## Purpose / Big Picture

TubeFactory gains a reusable “Direct scripted video” workflow. An operator can
paste a complete master script with scene directions, approve the imported
script, approve the derived storyboard, and then let existing narration,
ComfyUI/fixture scene generation, Remotion assembly, and QA produce a video.
This path performs no research, dossier generation, evidence checking, claim
scoring, or source validation. Existing evidence-first workflows remain
unchanged and continue to require dossier claims and verifier gates.

## Progress

- [x] (2026-08-15 00:00Z) Inspected existing script, storyboard, media, and UI lineage.
- [x] (2026-08-15 00:00Z) Decided on an explicit direct source mode instead of dummy dossiers.
- [x] (2026-08-15 14:35Z) Added shared parser and direct script/storyboard projection tests.
- [x] (2026-08-15 14:36Z) Added API schema/model/migration and direct import/preview routes.
- [x] (2026-08-15 14:37Z) Added worker activity/workflow and direct storyboard generation branch.
- [x] (2026-08-15 14:37Z) Adapted media production and QA for no-evidence source mode.
- [x] (2026-08-15 14:38Z) Added UI paste/import, preview, and direct-mode review affordances.
- [x] (2026-08-15 14:41Z) Ran targeted parser/API/worker/web validation.
- [x] (2026-08-15 14:49Z) Deployed the change to the live `youtuber` compose project that owns the active data and `:8090`.
- [x] (2026-08-15 14:50Z) Committed, pushed `dev`, and verified GitHub Actions CI passed.

## Surprises & Discoveries

- Observation: The existing `scripts` table requires non-null `opportunity_id`
  and `research_dossier_id`.
  Evidence: SQLAlchemy `ScriptModel` and migration `0006_editorial_production.py`.

- Observation: Storyboard and media context loaders join through opportunity and
  dossier lineage.
  Evidence: `load-storyboard-generation-context` and `load-media-production-context`.

- Observation: Making `scripts.source_kind` mandatory also requires updating
  the existing evidence-backed script insert path.
  Evidence: `persist-script-result` uses raw SQL and now explicitly inserts
  `source_kind='research_dossier'`.

## Decision Log

- Decision: Add `production_briefs` plus `scripts.source_kind='direct_scripted_video'`.
  Rationale: This avoids fake evidence records and keeps the existing verifier path intact.
  Date/Author: 2026-08-15 / Codex.

- Decision: Direct imports create a verified script in the sense of structural
  validity only, with `evidence_required=false` and no claim coverage.
  Rationale: Existing script approval requires `status='verified'`; the report
  must clearly state that evidence verification was not performed.
  Date/Author: 2026-08-15 / Codex.

- Decision: Script and storyboard approvals remain separate; automatic
  continuation is reused.
  Rationale: Matches current TubeFactory gates and the user-selected plan.
  Date/Author: 2026-08-15 / Codex.

## Outcomes & Retrospective

The direct scripted-video path is implemented and deployed to the live local
TubeFactory stack. Container validation passed for API route/schema
registration, direct parser/import smoke checks, editorial worker tests, web
TypeScript, Python syntax/import checks, migration application, service health,
LAN Caddy routing, and browser hydration to the login screen. The implementation
was committed and pushed to `origin/dev`; GitHub Actions CI passed.

## Context and Orientation

The API lives under `services/api/src/youtuber_api`. The editorial worker lives
under `services/editorial-worker/src/editorial_worker`. Shared deterministic
contracts live under `packages/core/src/editorial_core`. The web application is
`apps/web/app/page.tsx`.

The existing evidence-first path is:
approved dossier -> script generation/import -> independent verification ->
script approval -> storyboard generation -> storyboard approval -> media
production. The new path starts from a pasted production brief and skips all
dossier/evidence steps while reusing script/storyboard/media persistence and
approval objects.

## Plan of Work

Add a deterministic parser in `editorial_core.direct_scripted_video` that
extracts scene IDs, time ranges, voiceover, visual direction, on-screen text,
asset requests, approval notes, placeholders, script segments, and storyboard
templates. Add API schema and route for preview/import. Add a migration and
model for production briefs and direct source lineage. Add an editorial worker
workflow that persists the direct script and stores storyboard templates in the
verification report. Modify storyboard generation to materialize those templates
without AI. Modify media production to support direct lineage and no source
requirements. Update the UI to show a paste/import panel and direct-mode labels.

## Concrete Steps

Run from `/srv/TubeFactory`:

    sudo -n docker compose run --rm api python - <<'PY'
    # direct parser/import smoke
    PY
    sudo -n docker compose run --rm api pytest -q tests/test_app_contract.py tests/test_discovery_schema.py -p no:cacheprovider
    sudo -n docker compose --profile core run --rm editorial-worker pytest -q tests/test_editorial_worker.py -p no:cacheprovider
    cd apps/web && npm test

Then commit, push `dev`, and inspect GitHub Actions for the pushed SHA.

## Validation and Acceptance

Acceptance requires the direct parser fixture to produce twelve scenes from the
Chromsystems master script format, the API route to exist, direct scripts to be
listed under their selected channel, storyboard generation to skip AI in direct
mode, media QA to mark evidence coverage not applicable, and final media start
to reject unresolved placeholders.

## Idempotence and Recovery

Workflow idempotency keys map to one `workflow_control_records` row and one
production brief/script version. Re-running the same key returns the existing
script/version. If a migration must be rolled back, delete direct production
brief data first, then downgrade; evidence-first rows remain compatible.

## Artifacts and Notes

- `packages/core/src/editorial_core/direct_scripted_video.py`
- `services/api/migrations/versions/0022_direct_scripted_video.py`
- `execplan/direct-scripted-video.md`

## Interfaces and Dependencies

New API endpoints:

- `POST /api/v1/editorial/direct-scripted-video-preview`
- `POST /api/v1/editorial/direct-scripted-video-runs`

New Temporal workflow:

- `direct-scripted-video-import`

New source mode:

- `scripts.source_kind = 'direct_scripted_video'`
