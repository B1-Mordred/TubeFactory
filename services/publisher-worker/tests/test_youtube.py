from datetime import datetime, timedelta, timezone

import pytest
import httpx

from publisher_worker.youtube import FakeYouTubeProvider, YouTubeAPI


@pytest.mark.asyncio
async def test_mock_private_upload_retry_is_idempotent():
    provider = FakeYouTubeProvider()
    body = {"snippet": {"title": "Evidence"}, "status": {"privacyStatus": "private"}}
    first = await provider.upload_private(idempotency_key="same-release", body=body)
    second = await provider.upload_private(idempotency_key="same-release", body=body)
    assert first.video_id == second.video_id
    assert provider.uploads_created == 1
    assert first.resource["status"]["privacyStatus"] == "private"


@pytest.mark.asyncio
async def test_mock_caption_thumbnail_and_schedule_are_idempotent():
    provider = FakeYouTubeProvider()
    result = await provider.upload_private(idempotency_key="release", body={"snippet": {}, "status": {"privacyStatus": "private"}})
    await provider.upload_caption(result.video_id, "en", "English", b"WEBVTT", "text/vtt")
    await provider.upload_caption(result.video_id, "en", "English", b"WEBVTT", "text/vtt")
    await provider.set_thumbnail(result.video_id, b"image", "image/png")
    publish_at = datetime.now(timezone.utc) + timedelta(days=1)
    await provider.schedule(result.video_id, publish_at)
    await provider.schedule(result.video_id, publish_at)
    assert len(provider.videos[result.video_id]["captions"]) == 1
    assert provider.videos[result.video_id]["thumbnail"] is True
    assert provider.schedules_created == 1


@pytest.mark.asyncio
async def test_real_schedule_preserves_disclosure_and_audience_status():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(200, json={"id": "video", "status": captured["status"]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = YouTubeAPI("access-token", client=client)
    publish_at = datetime.now(timezone.utc) + timedelta(days=1)
    await api.schedule("video", publish_at, {
        "privacyStatus": "private", "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": True, "embeddable": True,
    })
    await client.aclose()
    assert captured["status"]["privacyStatus"] == "private"
    assert captured["status"]["selfDeclaredMadeForKids"] is False
    assert captured["status"]["containsSyntheticMedia"] is True
