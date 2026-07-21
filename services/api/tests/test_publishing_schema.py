import pytest
from pydantic import ValidationError

from editorial_core.publishing import PublishMode, YOUTUBE_SCOPES
from youtuber_api.schemas import PublicationStart, YouTubeOAuthStart


def test_upload_mode_defaults_to_dry_run():
    payload = PublicationStart.model_validate({
        "connection_id": "11111111-1111-4111-8111-111111111111",
        "render_id": "22222222-2222-4222-8222-222222222222",
        "expected_render_hash": "a" * 64,
        "metadata_version_id": "33333333-3333-4333-8333-333333333333",
        "expected_metadata_hash": "b" * 64,
        "idempotency_key": "dry-run-default",
    })
    assert payload.mode is PublishMode.DRY_RUN


def test_oauth_redirect_requires_https_except_localhost():
    channel = "11111111-1111-4111-8111-111111111111"
    assert YouTubeOAuthStart(channel_profile_id=channel, redirect_uri="http://localhost:8090/callback")
    with pytest.raises(ValidationError, match="must use HTTPS"):
        YouTubeOAuthStart(channel_profile_id=channel, redirect_uri="http://studio.example/callback")
    assert set(YOUTUBE_SCOPES) == {
        "https://www.googleapis.com/auth/youtube.force-ssl",
        "https://www.googleapis.com/auth/youtube.upload",
    }
