from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from youtuber_api.models import (
    MediaProductionModel,
    OpportunityModel,
    ScriptModel,
    StoryboardModel,
    StoryboardVersionModel,
    SubjectProfileModel,
)
from youtuber_api.routers.publishing import _require_matching_channel_lineage


class FakeSession:
    def __init__(self, values):
        self.values = values

    async def get(self, model, identifier):
        return self.values.get((model, identifier))


def lineage_fixture():
    channel_id = uuid4()
    production_id, storyboard_version_id, storyboard_id = uuid4(), uuid4(), uuid4()
    script_id, opportunity_id, subject_id = uuid4(), uuid4(), uuid4()
    values = {
        (MediaProductionModel, production_id): SimpleNamespace(storyboard_version_id=storyboard_version_id),
        (StoryboardVersionModel, storyboard_version_id): SimpleNamespace(storyboard_id=storyboard_id),
        (StoryboardModel, storyboard_id): SimpleNamespace(script_id=script_id),
        (ScriptModel, script_id): SimpleNamespace(opportunity_id=opportunity_id),
        (OpportunityModel, opportunity_id): SimpleNamespace(subject_profile_id=subject_id),
        (SubjectProfileModel, subject_id): SimpleNamespace(channel_profile_id=channel_id),
    }
    return FakeSession(values), SimpleNamespace(production_id=production_id), channel_id


@pytest.mark.asyncio
async def test_upload_accepts_render_and_connection_from_same_channel() -> None:
    session, render, channel_id = lineage_fixture()
    await _require_matching_channel_lineage(
        session, render, SimpleNamespace(channel_profile_id=channel_id)
    )


@pytest.mark.asyncio
async def test_upload_rejects_cross_channel_render() -> None:
    session, render, _ = lineage_fixture()
    with pytest.raises(HTTPException, match="does not belong") as caught:
        await _require_matching_channel_lineage(
            session, render, SimpleNamespace(channel_profile_id=uuid4())
        )
    assert caught.value.status_code == 409
