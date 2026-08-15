# Implement narration-first media editor

This ExecPlan is a living document. The sections `Progress`,
`Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`
must be kept current while work proceeds.

## Purpose / Big Picture

TubeFactory gains an operator-friendly media workflow where narration is
generated first from configurable narrator settings. The measured narration
then defines an editable production timeline. Operators can see separate video
and audio tracks, upload media clips, trim those clips, and add or remove
intro/outro scenes before or after the narration-derived storyboard body. The
approved storyboard and exact-hash approval gates remain intact.

An operator observes the change on the Media page: first create a narration
timeline from an approved storyboard, then edit the timeline and clips, then
queue the render from that timeline.

## Progress

- [x] (2026-08-15 20:15Z) Loaded ExecPlan guidance and UX heuristics.
- [x] (2026-08-15 20:20Z) Inspected TubeFactory media API, worker, render service, and web media panel.
- [x] (2026-08-15 20:25Z) Located the sibling media project at `/srv/DialectiCore` and inspected its Voicebox, timeline, and asset replacement patterns.
- [x] (2026-08-15 20:05Z) Added durable narration-timeline draft workflow and persistence.
- [x] (2026-08-15 20:08Z) Added operator clip upload, timeline save, and timeline render APIs.
- [x] (2026-08-15 20:10Z) Added render-service trim support and worker audio-track mixing.
- [x] (2026-08-15 20:12Z) Added operator-friendly Media page controls for narration timeline, scene edits, clip trims, and render queueing.
- [x] (2026-08-15 20:21Z) Ran targeted backend, worker, render-service, and web validation.
- [x] (2026-08-15 20:29Z) Deployed/restarted the live local `youtuber` stack, smoke-tested the browser flow, and verified the migration and HTTP route.
- [ ] Commit, push `origin/dev`, verify CI plus local/remote SHA equality.

## Surprises & Discoveries

- Observation: The requested `/srv/Dialecticore` path does not exist; the sibling project is `/srv/DialectiCore`.
  Evidence: `/srv` contains `/srv/DialectiCore`.

- Observation: TubeFactory already generates narration before scene visuals inside `MediaProductionWorkflow`, but it persists only the final render result.
  Evidence: `MediaProductionWorkflow` runs `generate-production-narration`, then `synchronize-media-timing`, then scene generation and assembly.

- Observation: DialectiCore uses the same practical shape requested here: explicit narrator settings, media asset records, editable timeline JSON, and asset replacement that rewrites active timeline references.
  Evidence: `PrimerProductionService`, `VoiceboxService`, `TimelineService`, and `AssetReplacementService`.

- Observation: The live LAN route is still served by the existing `youtuber-*` Compose project. Because the local directory was renamed, running Compose without an explicit project name creates a separate `tubefactory-*` stack.
  Evidence: `docker ps` showed `youtuber-caddy-1` bound to `0.0.0.0:8090`; a first `docker compose up` created separate `tubefactory-*` containers.

- Observation: Alembic revision IDs must fit the existing `alembic_version.version_num` column length.
  Evidence: revision `0023_narration_first_media_editor` failed with `value too long for type character varying(32)`; shortening it to `0023_media_timeline_editor` applied successfully.

- Observation: A pre-existing one-step render that was in `assembling` timed out when the render service was recreated during deployment.
  Evidence: Temporal history for `media-production-ui-3fe83102-6b69-4b58-b4be-d8ca65224e83` shows `ActivityTaskTimedOut` with `Server disconnected without sending a response`.

## Decision Log

- Decision: Store the operator timeline plan inside `media_productions.settings` instead of mutating approved `SceneSpec` rows.
  Rationale: The approved storyboard remains immutable and exact-hash approvals continue to protect editorial content.
  Date/Author: 2026-08-15 / Codex.

- Decision: Add a staged `media-timeline-draft` workflow for narration and a separate `media-timeline-render` workflow for rendering the saved timeline.
  Rationale: Human timeline editing happens between narration and final render; Temporal workflows stay durable and idempotent without requiring long-lived signals.
  Date/Author: 2026-08-15 / Codex.

- Decision: Keep this slice as an operator-safe timeline editor, not a full NLE.
  Rationale: The requested requirements are covered by narration timing, video/audio tracks, uploaded clip trimming, and intro/outro scene controls.
  Date/Author: 2026-08-15 / Codex.

- Decision: Starting a new narration timeline is blocked only by active or already `timeline_ready` productions for the same storyboard/tier, not by old ready/failed/blocked renders.
  Rationale: Operators must be able to improve or retry media from an approved storyboard without archiving old one-step renders first.
  Date/Author: 2026-08-15 / Codex.

## Outcomes & Retrospective

Implemented the narration-first operator flow:

- `media-timeline-draft` generates/masteres narration from the selected voice
  profile, synchronizes real narration durations, persists the mastered
  narration assets, and stores a default `media_timeline.v1` plan in the
  production as `timeline_ready`.
- Operators can upload immutable image/video/audio clips, bind image/video clips
  to scenes, trim clip in/out points, add/delete intro and outro scenes around
  the narration-locked storyboard body, save the plan by expected hash, and
  queue `media-timeline-render`.
- `media-timeline-render` loads the saved plan, generates only missing
  storyboard visuals, renders the planned scene order, mixes narration and
  operator audio tracks with FFmpeg, writes the saved timeline into the
  manifest, and reuses existing media QA.
- Render-service validation and Remotion composition accept safe internal media
  clip URLs with source trim metadata.

Validation evidence:

- `python3 -m compileall services/api/src/youtuber_api services/editorial-worker/src/editorial_worker`
- `npm test` in `services/render-service`
- `npm test` in `apps/web`
- `sudo docker run --rm youtuber-api:latest pytest -q tests/test_media_ranges.py tests/test_app_contract.py -p no:cacheprovider` -> 19 passed
- `sudo env COMPOSE_PROJECT_NAME=youtuber docker compose --profile core ps` -> live `api`, `web`, `editorial-worker`, `render-service`, and `caddy` healthy
- `select version_num from alembic_version` -> `0023_media_timeline_editor`
- `curl -fsSI http://evidence-studio.local:8090/` -> HTTP 200
- Browser smoke test: signed in as local admin, opened `?page=media`, confirmed
  “Create narration timeline”, narrator/workflow/tier selectors, and production
  queue render on the live route.

## Context and Orientation

The API is in `services/api/src/youtuber_api`, with media routes in
`routers/media.py`, request/response schemas in `schemas.py`, and SQLAlchemy
models in `models.py`. The editorial worker is in
`services/editorial-worker/src/editorial_worker`; media activities live in
`media_activities.py` and Temporal workflows in `workflows.py`. Remotion render
validation and composition live in `services/render-service`. The single-page
React UI is `apps/web/app/page.tsx` with styles in `apps/web/app/styles.css`.

Existing production flow:

approved storyboard -> media-production workflow -> narration -> timing sync ->
scene visual generation -> Remotion render -> FFmpeg mux/QA -> immutable
manifest/render assets -> exact render approval.

New staged flow:

approved storyboard -> media-timeline-draft workflow -> persisted narration and
default timeline plan -> operator uploads/trims clips and optional intro/outro
scenes -> media-timeline-render workflow -> QA/render approval.

## Plan of Work

Add workflow-control and media-production state values for the two new media
workflow types and the `timeline_ready` state. Add `operator_clip` as a media
asset kind. Implement worker activities to persist narration-only draft assets,
build a default timeline plan from measured narration, load an existing timeline
production, render that plan, and update the existing production. Extend render
request validation and the Remotion scene component to support source trim
fields. Add API routes for creating timeline drafts, uploading clips, saving
timeline plans, and queueing timeline renders. Add web UI controls that present
the process as clear steps and avoid hidden hover-only behavior.

## Concrete Steps

Run from `/srv/TubeFactory`:

    pytest -q services/api/tests/test_media_ranges.py -p no:cacheprovider
    cd services/render-service && npm test
    cd apps/web && npm test

If backend changes touch worker imports or migrations, also run containerized
smoke checks against the compose stack before deployment.

## Validation and Acceptance

Acceptance requires:

- The API exposes narration timeline draft, timeline plan save, operator clip
  upload, and timeline render routes.
- A draft production reaches `timeline_ready` after narration generation and
  stores mastered narration assets plus a default timeline plan.
- Operators can add intro/outro scenes, select uploaded video/audio clips, enter
  trim ranges, and queue a render without editing JSON manually.
- Render props accept internal media clip URLs with safe source trim metadata.
- Timeline render manifests show the saved timeline plan and render from the
  selected clips where provided.

## Idempotence and Recovery

Workflow IDs use UI idempotency keys and `workflow_control_records`. Reusing the
same key returns the existing Temporal workflow. Timeline saves overwrite only
the mutable `settings.timeline_plan` document and keep previous media assets.
Uploaded clips are immutable media assets. If a render fails, the production
remains available for timeline edits and another render workflow with a fresh
idempotency key.

## Artifacts and Notes

- `execplan/narration-first-media-editor.md`
- `services/api/src/youtuber_api/routers/media.py`
- `services/editorial-worker/src/editorial_worker/media_activities.py`
- `services/editorial-worker/src/editorial_worker/workflows.py`
- `services/render-service/validation.mjs`
- `services/render-service/src/video.jsx`
- `apps/web/app/page.tsx`

## Interfaces and Dependencies

New API endpoints:

- `POST /api/v1/media/timeline-drafts`
- `POST /api/v1/media/productions/{production_id}/clips`
- `PUT /api/v1/media/productions/{production_id}/timeline-plan`
- `POST /api/v1/media/productions/{production_id}/timeline-render`

New Temporal workflows:

- `media-timeline-draft`
- `media-timeline-render`
