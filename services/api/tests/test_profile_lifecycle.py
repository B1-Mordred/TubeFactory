from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from youtuber_api.models import ChannelProfileModel, SubjectProfileModel
from youtuber_api.routers import profiles


class FakeSession:
    def __init__(self, records, subjects=()):
        self.records = records
        self.subjects = list(subjects)

    async def get(self, model, identifier, **_kwargs):
        return self.records.get((model, identifier))

    async def scalars(self, _statement):
        return self.subjects


def request_fixture():
    return SimpleNamespace(
        state=SimpleNamespace(correlation_id="profile-lifecycle-test"),
        app=SimpleNamespace(state=SimpleNamespace(temporal_client=object())),
    )


async def no_commit(**kwargs):
    return kwargs["profile"]


@pytest.mark.asyncio
async def test_channel_archive_is_blocked_until_subjects_are_archived(monkeypatch) -> None:
    channel_id = uuid4()
    channel = SimpleNamespace(id=channel_id, version=3, enabled=True, deleted_at=None)
    subject = SimpleNamespace(name="Still active")
    session = FakeSession({(ChannelProfileModel, channel_id): channel}, [subject])
    monkeypatch.setattr(profiles, "_commit_profile", no_commit)

    with pytest.raises(HTTPException, match="Still active") as caught:
        await profiles.archive_channel(
            channel_id,
            request_fixture(),
            SimpleNamespace(id=uuid4()),
            session,
            expected_version=3,
        )

    assert caught.value.status_code == 409
    assert channel.deleted_at is None


@pytest.mark.asyncio
async def test_channel_archive_is_versioned_and_recoverable(monkeypatch) -> None:
    channel_id = uuid4()
    channel = SimpleNamespace(id=channel_id, version=3, enabled=True, deleted_at=None, updated_at=None)
    session = FakeSession({(ChannelProfileModel, channel_id): channel})
    monkeypatch.setattr(profiles, "_commit_profile", no_commit)

    result = await profiles.archive_channel(
        channel_id,
        request_fixture(),
        SimpleNamespace(id=uuid4()),
        session,
        expected_version=3,
    )

    assert channel.enabled is False
    assert channel.deleted_at == result.archived_at
    assert channel.version == result.version == 4


@pytest.mark.asyncio
async def test_subject_archive_pauses_schedule_before_soft_delete(monkeypatch) -> None:
    subject_id = uuid4()
    subject = SimpleNamespace(
        id=subject_id,
        version=2,
        enabled=True,
        deleted_at=None,
        updated_at=None,
        schedule={"cron": "0 6 * * *", "timezone": "Europe/Berlin"},
    )
    session = FakeSession({(SubjectProfileModel, subject_id): subject})
    calls = []

    class FakeScheduleGateway:
        def __init__(self, _client):
            pass

        async def reconcile(self, **kwargs):
            calls.append(kwargs)
            return {
                "schedule_id": f"subject-discovery-{subject_id}",
                "exists": True,
                "paused": True,
            }

    monkeypatch.setattr(profiles, "SubjectScheduleGateway", FakeScheduleGateway)
    monkeypatch.setattr(profiles, "_commit_profile", no_commit)

    result = await profiles.archive_subject(
        subject_id,
        request_fixture(),
        SimpleNamespace(id=uuid4()),
        session,
        expected_version=2,
    )

    assert calls == [{
        "subject_profile_id": subject_id,
        "cron": "0 6 * * *",
        "timezone_name": "Europe/Berlin",
        "enabled": False,
    }]
    assert subject.enabled is False
    assert subject.deleted_at == result.archived_at
    assert subject.version == result.version == 3


@pytest.mark.asyncio
async def test_subject_archive_rejects_stale_version_before_schedule_change(monkeypatch) -> None:
    subject_id = uuid4()
    subject = SimpleNamespace(id=subject_id, version=5, enabled=True, deleted_at=None)
    session = FakeSession({(SubjectProfileModel, subject_id): subject})
    schedule_gateway_created = False

    class UnexpectedScheduleGateway:
        def __init__(self, _client):
            nonlocal schedule_gateway_created
            schedule_gateway_created = True

    monkeypatch.setattr(profiles, "SubjectScheduleGateway", UnexpectedScheduleGateway)

    with pytest.raises(HTTPException, match="reload before archiving") as caught:
        await profiles.archive_subject(
            subject_id,
            request_fixture(),
            SimpleNamespace(id=uuid4()),
            session,
            expected_version=4,
        )

    assert caught.value.status_code == 409
    assert schedule_gateway_created is False
