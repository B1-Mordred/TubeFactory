import pytest

from youtuber_api.routers.media import _timeline_plan_hash, parse_byte_range


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        (None, 100, None),
        ("bytes=0-9", 100, (0, 9)),
        ("bytes=90-", 100, (90, 99)),
        ("bytes=-10", 100, (90, 99)),
        ("bytes=0-999", 100, (0, 99)),
    ],
)
def test_parse_byte_range(header, size, expected):
    assert parse_byte_range(header, size) == expected


@pytest.mark.parametrize("header", ["items=0-1", "bytes=", "bytes=4-2", "bytes=100-", "bytes=0-1,3-4"])
def test_parse_byte_range_rejects_invalid_or_unsupported_ranges(header):
    with pytest.raises(ValueError):
        parse_byte_range(header, 100)


def test_timeline_plan_hash_ignores_mutable_metadata():
    plan = {
        "schema_version": "media_timeline.v1",
        "production_id": "prod-1",
        "duration_seconds": 12.5,
        "scenes": [{"id": "scene-1", "duration_seconds": 12.5}],
        "tracks": {"video": [], "audio": []},
        "updated_at": "2026-08-15T10:00:00+00:00",
        "content_hash": "a" * 64,
    }
    changed_metadata = {
        **plan,
        "updated_at": "2026-08-15T11:00:00+00:00",
        "content_hash": "b" * 64,
    }
    changed_content = {
        **plan,
        "duration_seconds": 13.0,
    }

    assert _timeline_plan_hash(plan) == _timeline_plan_hash(changed_metadata)
    assert _timeline_plan_hash(plan) != _timeline_plan_hash(changed_content)
