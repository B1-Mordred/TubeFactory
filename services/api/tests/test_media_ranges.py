import pytest

from youtuber_api.routers.media import parse_byte_range


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
