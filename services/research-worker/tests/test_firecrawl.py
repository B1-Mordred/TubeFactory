import json

import pytest

from research_worker.acquisition import DomainPolicy
from research_worker.firecrawl import parse_firecrawl_response, validate_firecrawl_endpoint


def test_firecrawl_endpoint_rejects_credentials_and_query() -> None:
    assert validate_firecrawl_endpoint("http://firecrawl-api:3002/") == "http://firecrawl-api:3002"
    with pytest.raises(ValueError):
        validate_firecrawl_endpoint("http://user:secret@firecrawl-api:3002")
    with pytest.raises(ValueError):
        validate_firecrawl_endpoint("http://firecrawl-api:3002?target=override")


def test_firecrawl_response_is_bounded_and_revalidates_final_domain() -> None:
    raw = json.dumps(
        {
            "success": True,
            "data": {
                "rawHtml": "<main>Rendered evidence</main>",
                "metadata": {"sourceURL": "https://www.example.org/final", "statusCode": 200},
            },
        }
    ).encode()
    result = parse_firecrawl_response(
        raw,
        requested_url="https://example.org/start",
        policy=DomainPolicy(allow=("example.org",)),
        maximum_html_bytes=1_000,
    )
    assert result.body == b"<main>Rendered evidence</main>"
    assert result.final_url == "https://www.example.org/final"

    with pytest.raises(ValueError, match="domain policy"):
        parse_firecrawl_response(
            raw.replace(b"www.example.org", b"attacker.invalid"),
            requested_url="https://example.org/start",
            policy=DomainPolicy(allow=("example.org",)),
            maximum_html_bytes=1_000,
        )
    with pytest.raises(ValueError, match="byte limit"):
        parse_firecrawl_response(
            raw,
            requested_url="https://example.org/start",
            policy=DomainPolicy(),
            maximum_html_bytes=5,
        )


def test_firecrawl_response_rejects_https_downgrade() -> None:
    raw = json.dumps(
        {
            "success": True,
            "data": {
                "rawHtml": "<main>Rendered evidence</main>",
                "metadata": {"sourceURL": "http://example.org/final", "statusCode": 200},
            },
        }
    ).encode()
    with pytest.raises(ValueError, match="downgrade"):
        parse_firecrawl_response(
            raw,
            requested_url="https://example.org/start",
            policy=DomainPolicy(),
            maximum_html_bytes=1_000,
        )
