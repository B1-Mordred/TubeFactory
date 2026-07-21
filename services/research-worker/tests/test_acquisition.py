import asyncio
import socket

import pytest
import aiohttp
from aiohttp import web

from editorial_core.acquisition import UnsafeSourceAddress
import research_worker.acquisition as acquisition_module
from research_worker.acquisition import (
    CachedRobotsPolicy,
    DomainPolicy,
    DomainThrottle,
    PublicAddressResolver,
    RobotsDenied,
    RobotsPolicyCache,
    acquire_public_source,
    robots_text_allows,
    validate_identity_content_encoding,
    validate_redirect_target,
    validate_source_target,
)


def test_domain_policy_supports_exact_and_subdomain_rules() -> None:
    policy = DomainPolicy(allow=("example.org",), block=("ads.example.org",))
    assert policy.permits("https://www.example.org/report")
    assert not policy.permits("https://ads.example.org/tracker")
    assert not policy.permits("https://example.com/report")


def test_source_target_rejects_credentials_and_nonstandard_ports() -> None:
    with pytest.raises(ValueError):
        validate_source_target("https://user:secret@example.org/report", DomainPolicy())
    with pytest.raises(ValueError, match="disallowed port"):
        validate_source_target("https://example.org:8443/report", DomainPolicy())


def test_source_target_preserves_explicit_directory_slash_for_redirects() -> None:
    assert (
        validate_source_target("https://example.org/reports/", DomainPolicy())
        == "https://example.org/reports/"
    )


def test_redirect_target_rejects_https_downgrade() -> None:
    with pytest.raises(ValueError, match="downgrade"):
        validate_redirect_target(
            "https://example.org/report",
            "http://example.org/report/",
            DomainPolicy(),
        )
    assert (
        validate_redirect_target(
            "https://example.org/report", "/report/", DomainPolicy()
        )
        == "https://example.org/report/"
    )


def test_robots_policy_parser_honors_agent_specific_disallow() -> None:
    body = "User-agent: EvidenceStudioResearch\nDisallow: /private\nAllow: /public\n"
    assert robots_text_allows(
        "https://example.org/robots.txt",
        body,
        "EvidenceStudioResearch/0.1",
        "https://example.org/public/report",
    )
    assert not robots_text_allows(
        "https://example.org/robots.txt",
        body,
        "EvidenceStudioResearch/0.1",
        "https://example.org/private/report",
    )


def test_robots_cache_is_bounded_expires_and_rechecks_each_path() -> None:
    now = [100.0]
    cache = RobotsPolicyCache(
        ttl_seconds=60,
        maximum_entries=1,
        clock=lambda: now[0],
    )
    key = cache.key("https://example.org/public", "EvidenceStudioResearch/0.1")
    rules = CachedRobotsPolicy(
        "https://example.org/robots.txt",
        "User-agent: EvidenceStudioResearch\nDisallow: /private\nAllow: /public\n",
    )
    cache.put(key, rules)
    assert cache.get(key) is rules
    rules.enforce("EvidenceStudioResearch/0.1", "https://example.org/public/report")
    with pytest.raises(RobotsDenied, match="disallows"):
        rules.enforce("EvidenceStudioResearch/0.1", "https://example.org/private/report")
    cache.put(
        cache.key("https://other.example/report", "EvidenceStudioResearch/0.1"),
        CachedRobotsPolicy("https://other.example/robots.txt"),
    )
    assert cache.get(key) is None
    now[0] += 61
    assert cache.get(
        cache.key("https://other.example/report", "EvidenceStudioResearch/0.1")
    ) is None


def test_encoded_response_bodies_are_rejected_before_parsing() -> None:
    validate_identity_content_encoding(None, "source")
    validate_identity_content_encoding("identity", "source")
    with pytest.raises(ValueError, match="Content-Encoding"):
        validate_identity_content_encoding("gzip", "source")


@pytest.mark.asyncio
async def test_concurrent_robots_checks_are_coalesced_per_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_fetch(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return CachedRobotsPolicy(
            "https://example.org/robots.txt",
            "User-agent: *\nAllow: /\n",
        )

    monkeypatch.setattr(acquisition_module, "_fetch_robots_policy", fake_fetch)
    cache = RobotsPolicyCache(ttl_seconds=60, maximum_entries=8)
    results = await asyncio.gather(
        *[
            acquisition_module.enforce_robots_policy(
                f"https://example.org/report/{index}",
                policy=DomainPolicy(),
                throttle=DomainThrottle(0),
                user_agent="EvidenceStudioResearch/0.1",
                cache=cache,
            )
            for index in range(4)
        ]
    )
    assert calls == 1
    assert results.count(False) == 1
    assert results.count(True) == 3


@pytest.mark.asyncio
async def test_connector_resolver_rejects_private_dns_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()

    async def private_answer(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", private_answer)
    with pytest.raises(UnsafeSourceAddress, match="non-public"):
        await PublicAddressResolver().resolve("attacker.example", 443)


@pytest.mark.asyncio
async def test_connector_resolver_returns_only_validated_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()

    async def public_answer(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", public_answer)
    result = await PublicAddressResolver().resolve("example.org", 443)
    assert result[0]["host"] == "93.184.216.34"
    assert result[0]["hostname"] == "example.org"


@pytest.mark.asyncio
async def test_connector_rejects_mixed_public_private_dns_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()

    async def mixed_answer(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(loop, "getaddrinfo", mixed_answer)
    with pytest.raises(UnsafeSourceAddress, match="non-public"):
        await PublicAddressResolver().resolve("rebinding.example", 443)


@pytest.mark.asyncio
async def test_bounded_http_transport_redirect_mime_size_and_encoding_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def robots(_: web.Request) -> web.Response:
        return web.Response(text="User-agent: *\nAllow: /\n")

    async def ok(_: web.Request) -> web.Response:
        return web.Response(text="<main>bounded evidence</main>", content_type="text/html")

    async def redirect(_: web.Request) -> web.Response:
        raise web.HTTPFound("/ok")

    async def loop(_: web.Request) -> web.Response:
        raise web.HTTPFound("/loop")

    async def bad_mime(_: web.Request) -> web.Response:
        return web.Response(body=b"binary", content_type="application/octet-stream")

    async def too_large(_: web.Request) -> web.Response:
        return web.Response(body=b"x" * 256, content_type="text/plain")

    async def encoded(_: web.Request) -> web.Response:
        return web.Response(
            body=b"not-compressed",
            content_type="text/plain",
            headers={"Content-Encoding": "gzip"},
        )

    app = web.Application()
    app.add_routes(
        [
            web.get("/robots.txt", robots),
            web.get("/ok", ok),
            web.get("/redirect", redirect),
            web.get("/loop", loop),
            web.get("/bad-mime", bad_mime),
            web.get("/too-large", too_large),
            web.get("/encoded", encoded),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    # The real public-address validator has separate unit gates above. This fixture
    # substitutes only the connector/port so it can exercise actual aiohttp response
    # and streaming behavior without opening a public listener during the test.
    monkeypatch.setattr(acquisition_module, "PublicAddressResolver", aiohttp.DefaultResolver)
    monkeypatch.setattr(
        acquisition_module,
        "validate_source_target",
        lambda url, policy: url,
    )
    common = {
        "policy": DomainPolicy(),
        "throttle": DomainThrottle(0),
        "user_agent": "EvidenceStudioResearch/0.1",
        "maximum_bytes": 100,
        "robots_cache": RobotsPolicyCache(ttl_seconds=60, maximum_entries=8),
    }
    try:
        acquired = await acquire_public_source(
            f"http://127.0.0.1:{port}/redirect", **common
        )
        assert acquired.final_url.endswith("/ok")
        assert acquired.redirect_chain == (f"http://127.0.0.1:{port}/redirect",)
        with pytest.raises(ValueError, match="MIME type"):
            await acquire_public_source(f"http://127.0.0.1:{port}/bad-mime", **common)
        with pytest.raises(ValueError, match="byte limit"):
            await acquire_public_source(f"http://127.0.0.1:{port}/too-large", **common)
        with pytest.raises(ValueError, match="Content-Encoding"):
            await acquire_public_source(f"http://127.0.0.1:{port}/encoded", **common)
        with pytest.raises(ValueError, match="redirect limit"):
            await acquire_public_source(
                f"http://127.0.0.1:{port}/loop", maximum_redirects=1, **common
            )
    finally:
        await runner.cleanup()
