import inspect
from uuid import uuid4

import pytest
from pydantic import ValidationError

from youtuber_api.main import app
from youtuber_api.routers.editorial import edit_script_version
from youtuber_api.schemas import (
    AllOpportunityArchiveWrite,
    DirectScriptedVideoImportStart,
    ExistingResearchScriptImportStart,
    PlaceholderReplacementWrite,
    ScoredOpportunityArchiveWrite,
    ScriptAnnotationWrite,
    ScriptSegmentWrite,
    ScriptVersionEdit,
)


def test_application_routes_can_be_constructed() -> None:
    paths = {route.path for route in app.routes}
    methods_by_path: dict[str, set[str]] = {}
    for route in app.routes:
        methods_by_path.setdefault(route.path, set()).update(getattr(route, "methods", set()) or set())
    assert "/api/health/ready" in paths
    assert "/api/v1/auth/logout" in paths
    assert "/api/v1/auth/totp/enroll" in paths
    assert "/api/v1/auth/totp/confirm" in paths
    assert "/api/v1/auth/totp/disable" in paths
    assert "/api/v1/auth/oidc/status" in paths
    assert "/api/v1/auth/oidc/configuration" in paths
    assert "/api/v1/auth/oidc/start" in paths
    assert "/api/v1/auth/oidc/callback" in paths
    assert "/api/v1/system/durability-probes" in paths
    assert "/api/v1/channel-profiles" in paths
    assert "PUT" in methods_by_path["/api/v1/channel-profiles/{profile_id}"]
    assert "DELETE" in methods_by_path["/api/v1/channel-profiles/{profile_id}"]
    assert "/api/v1/channel-profiles/archived" in paths
    assert "POST" in methods_by_path["/api/v1/channel-profiles/{profile_id}/restore"]
    assert "/api/v1/subject-profiles" in paths
    assert "PUT" in methods_by_path["/api/v1/subject-profiles/{profile_id}"]
    assert "DELETE" in methods_by_path["/api/v1/subject-profiles/{profile_id}"]
    assert "/api/v1/subject-profiles/archived" in paths
    assert "POST" in methods_by_path["/api/v1/subject-profiles/{profile_id}/restore"]
    assert "/api/v1/subject-profiles/{profile_id}/test-search-plan" in paths
    assert "/api/v1/subject-profiles/{profile_id}/schedule" in paths
    assert "/api/v1/subject-profiles/{profile_id}/schedule/reconcile" in paths
    assert "/api/v1/research/fixture-runs" in paths
    assert "/api/v1/research/live-discovery-runs" in paths
    assert "/api/v1/research/acquisition-runs" in paths
    assert "/api/v1/research/dossier-runs" in paths
    assert "/api/v1/research/runs/{workflow_id}/events" in paths
    assert "/api/v1/research/runs/{workflow_id}/logs" in paths
    assert "/api/v1/research/runs/{workflow_id}/cancel" in paths
    assert "/api/v1/research/runs/{workflow_id}/retry" in paths
    assert "/api/v1/research/sources" in paths
    assert "/api/v1/research/sources/{source_id}/preview" in paths
    assert "/api/v1/research/sources/{source_id}/index" in paths
    assert "/api/v1/research/source-relationships" in paths
    assert "/api/v1/research/opportunities/{opportunity_id}/decision" in paths
    assert "/api/v1/research/opportunities/{opportunity_id}/manual-dossier" in paths
    assert "/api/v1/research/opportunities" in paths
    assert "/api/v1/research/opportunities/archived" in paths
    assert "/api/v1/research/opportunities/archive-scored" in paths
    assert "/api/v1/research/opportunities/archive-all" in paths
    assert "/api/v1/research/dossiers/{dossier_id}" in paths
    assert "/api/v1/research/dossiers/{dossier_id}/review" in paths
    assert "/api/v1/research/claims/{claim_id}/review" in paths
    assert "/api/v1/providers" in paths
    assert "/api/v1/models" in paths
    assert "/api/v1/ai-usage" in paths
    assert "/api/v1/task-model-assignments/{task_type}" in paths
    assert "/api/v1/prompt-templates/{template_key}" in paths
    assert "/api/v1/editorial/script-runs" in paths
    assert "/api/v1/editorial/script-import-runs" in paths
    assert "/api/v1/editorial/direct-scripted-video-preview" in paths
    assert "/api/v1/editorial/direct-scripted-video-runs" in paths
    assert "/api/v1/editorial/runs/{workflow_id}/events" in paths
    assert "/api/v1/editorial/runs/{workflow_id}/cancel" in paths
    assert "/api/v1/editorial/runs/{workflow_id}/retry" in paths
    assert "/api/v1/editorial/scripts/{script_id}" in paths
    assert "/api/v1/editorial/scripts/{script_id}/versions" in paths
    assert "/api/v1/editorial/scripts/{script_id}/verification-runs" in paths
    assert "/api/v1/editorial/scripts/{script_id}/regeneration-runs" in paths
    assert "/api/v1/editorial/scripts/{script_id}/approve" in paths
    assert "/api/v1/editorial/scripts/{script_id}/storyboard-runs" in paths
    assert "/api/v1/editorial/storyboards/{storyboard_id}" in paths
    assert "/api/v1/editorial/storyboards/{storyboard_id}/placeholders/replace" in paths
    assert "/api/v1/editorial/storyboards/{storyboard_id}/scenes/{scene_id}/lock" in paths
    assert "/api/v1/editorial/storyboards/{storyboard_id}/scenes/{scene_id}/versions" in paths
    assert "/api/v1/editorial/storyboards/{storyboard_id}/scenes/{scene_id}/alternative-runs" in paths
    assert "/api/v1/editorial/storyboards/{storyboard_id}/scenes/{scene_id}/alternatives" in paths
    assert "/api/v1/editorial/storyboards/{storyboard_id}/scenes/{scene_id}/alternatives/{alternative_id}/select" in paths
    assert "/api/v1/editorial/storyboards/{storyboard_id}/approve" in paths
    assert "/api/v1/media/productions" in paths
    assert "/api/v1/media/timeline-drafts" in paths
    assert "/api/v1/media/productions/{production_id}/clips" in paths
    assert "/api/v1/media/productions/{production_id}/timeline-plan" in paths
    assert "/api/v1/media/productions/{production_id}/timeline-render" in paths
    assert "POST" in methods_by_path["/api/v1/media/timeline-drafts"]
    assert "POST" in methods_by_path["/api/v1/media/productions/{production_id}/clips"]
    assert "PUT" in methods_by_path["/api/v1/media/productions/{production_id}/timeline-plan"]
    assert "POST" in methods_by_path["/api/v1/media/productions/{production_id}/timeline-render"]
    assert "/api/v1/publishing/configuration" in paths
    assert "/api/v1/publishing/oauth/start" in paths
    assert "/api/v1/publishing/oauth/callback" in paths
    assert "/api/v1/publishing/metadata" in paths
    assert "/api/v1/publishing/approvals" in paths
    assert "/api/v1/publishing/uploads" in paths
    assert "/api/v1/publishing/uploads/{publication_id}/reconcile" in paths
    assert "/api/v1/publishing/uploads/{publication_id}/schedule" in paths
    assert "/api/v1/optimization/analytics/snapshots" in paths
    assert "/api/v1/optimization/analytics/dashboard" in paths
    assert "/api/v1/optimization/benchmarks" in paths
    assert "/api/v1/optimization/recommendations/{recommendation_id}/decision" in paths
    assert "/api/v1/optimization/recommendations/{recommendation_id}/apply" in paths
    assert "/api/v1/optimization/freshness-checks" in paths
    assert "/api/v1/optimization/originality-reports" in paths
    assert "/api/v1/optimization/corrections" in paths
    assert "/api/v1/optimization/budgets/{scope}" in paths
    assert "/api/v1/optimization/budget-usage" in paths
    assert "/api/v1/optimization/operational-evidence" in paths


def test_manual_script_edits_allocate_after_preserved_candidates() -> None:
    source = inspect.getsource(edit_script_version)

    assert "func.max(ScriptVersionModel.version_number)" in source


def test_scored_opportunity_archive_requires_an_explicit_reason() -> None:
    with pytest.raises(ValidationError):
        ScoredOpportunityArchiveWrite(subject_profile_id=uuid4(), reason="cleanup")

    payload = ScoredOpportunityArchiveWrite(
        subject_profile_id=uuid4(),
        reason="Remove obsolete discovery findings before a clean acceptance run.",
    )
    assert payload.reason.startswith("Remove obsolete")


def test_direct_scripted_video_import_rejects_short_idempotency_key() -> None:
    with pytest.raises(ValidationError):
        DirectScriptedVideoImportStart(
            channel_profile_id=uuid4(),
            title="Direct script",
            master_script="S01 | 00:00–00:10 | Short\nVoiceover: " + "word " * 50,
            idempotency_key="short",
        )


def test_systemwide_opportunity_archive_requires_an_explicit_reason() -> None:
    with pytest.raises(ValidationError):
        AllOpportunityArchiveWrite(reason="cleanup")

    payload = AllOpportunityArchiveWrite(
        reason="Archive every old workflow lineage before starting a clean test run."
    )
    assert payload.reason.startswith("Archive every")


def test_existing_research_script_import_requires_substantive_text() -> None:
    with pytest.raises(ValidationError):
        ExistingResearchScriptImportStart(
            opportunity_id=uuid4(),
            title="Draft",
            script_text="too short",
            idempotency_key="import-test",
        )

    payload = ExistingResearchScriptImportStart(
        opportunity_id=uuid4(),
        title="Imported explainer",
        script_text="A complete source-bound script sentence. " * 10,
        idempotency_key="import-test",
    )
    assert len(payload.script_text) >= 200


def test_placeholder_replacement_normalizes_tokens_and_rejects_nested_placeholders() -> None:
    payload = PlaceholderReplacementWrite(
        expected_script_version=1,
        expected_script_hash="a" * 64,
        expected_storyboard_version=1,
        expected_storyboard_hash="b" * 64,
        replacements={"LEGACY_AKTIV_MIN": "42 Minuten"},
        comment="Replace benchmark placeholders before render.",
    )
    assert payload.replacements == {"[LEGACY_AKTIV_MIN]": "42 Minuten"}

    with pytest.raises(ValidationError):
        PlaceholderReplacementWrite(
            expected_script_version=1,
            expected_script_hash="a" * 64,
            expected_storyboard_version=1,
            expected_storyboard_hash="b" * 64,
            replacements={"[LEGACY_AKTIV_MIN]": "[OTHER_TOKEN]"},
            comment="Reject values that create another unresolved placeholder.",
        )


def test_direct_script_version_edit_allows_short_scene_plan_schema() -> None:
    segment = ScriptSegmentWrite(
        segment_key="s01",
        segment_type="hook",
        narration="A concise direct narration segment.",
        presentation_purpose="Open the direct scripted video.",
        duration_seconds=20,
        citation_display={},
        annotations=[
            ScriptAnnotationWrite(
                text="A concise direct narration segment.",
                start_offset=0,
                end_offset=35,
                kind="editorial",
            )
        ],
    )
    payload = ScriptVersionEdit(
        expected_version=1,
        expected_hash="c" * 64,
        title="Short direct script",
        segments=[segment],
        comment="Direct scripts may preserve a short operator scene plan.",
    )
    assert len(payload.segments) == 1
