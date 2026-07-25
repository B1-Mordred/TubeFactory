import asyncio
import socket

import pytest
import aiohttp
from aiohttp import web

from editorial_core.acquisition import UnsafeSourceAddress
import research_worker.acquisition as acquisition_module
from research_worker.acquisition_activities import (
    _candidate_relevance,
    _classify_search_candidate,
    _doi_from_citation_url,
    _generic_evidence_queries,
    _linked_primary_citations,
    _public_api_candidate,
    _registry_query_for_citation,
    _resolve_registry_candidate,
    _scholarly_work_identity,
    _topic_source_bootstrap_queries,
)
from research_worker.acquisition import (
    CachedRobotsPolicy,
    DomainPolicy,
    DomainThrottle,
    PublicAddressResolver,
    RobotsDenied,
    RobotsPolicyCache,
    acquire_public_source,
    robots_text_allows,
    search_searxng,
    validate_identity_content_encoding,
    validate_redirect_target,
    validate_source_target,
)


def test_linked_primary_citations_extracts_explicit_study_links_only() -> None:
    html = b"""
    <article>
      <a href="https://www.acpjournals.org/doi/10.7326/ANNALS-25-01660">
        Prolonged Short Sleep and Its Effect on Body Weight and Composition
      </a>
      <a href="/news/related-story">Related reporting</a>
      <a href="https://www.acpjournals.org/doi/10.7326/ANNALS-25-01660?utm_source=x">
        Duplicate citation
      </a>
    </article>
    """

    citations = _linked_primary_citations(html, "https://www.mdr.de/wissen/story.html")

    assert citations == (
        {
            "url": "https://www.acpjournals.org/doi/10.7326/ANNALS-25-01660",
            "title": "Prolonged Short Sleep and Its Effect on Body Weight and Composition",
            "source_type": "primary",
            "resolution_strategy": "explicit_primary_link",
        },
        {
            "url": "https://doi.org/10.7326/ANNALS-25-01660",
            "title": "Primary evidence identified by DOI 10.7326/ANNALS-25-01660",
            "source_type": "primary",
            "resolution_strategy": "embedded_doi",
        },
    )


def test_linked_primary_citations_rejects_social_export_aliases() -> None:
    citations = _linked_primary_citations(
        b"""
        <a href="https://reddit.com/submit?url=https://arxiv.org/abs/2201.05048">Share</a>
        <a href="https://www.bibsonomy.org/BibtexHandler?url=https://arxiv.org/abs/2201.05048">Export</a>
        <a href="https://arxiv.org/abs/2201.05048v1">Actual paper</a>
        """,
        "https://example.org/story",
    )
    assert [item["url"] for item in citations] == [
        "https://arxiv.org/abs/2201.05048v1"
    ]


def test_doi_is_resolved_from_journal_and_canonical_urls() -> None:
    assert (
        _doi_from_citation_url(
            "https://www.acpjournals.org/doi/10.7326/ANNALS-25-01660?source=article"
        )
        == "10.7326/ANNALS-25-01660"
    )
    assert _doi_from_citation_url("https://doi.org/10.1000/example") == "10.1000/example"
    assert _doi_from_citation_url("https://example.org/story") is None


@pytest.mark.asyncio
async def test_doi_resolution_returns_crawlable_europe_pmc_record(
    unused_tcp_port: int,
) -> None:
    async def search(request: web.Request) -> web.Response:
        assert request.query["query"] == "DOI:10.1000/example"
        return web.json_response({"hitCount": 1, "resultList": {"result": [{"id": "1"}]}})

    app = web.Application()
    app.router.add_get("/search", search)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    try:
        async with aiohttp.ClientSession() as session:
            candidate = await _resolve_registry_candidate(
                session,
                {
                    "url": "https://doi.org/10.1000/example",
                    "title": "Example primary study",
                },
                europe_pmc_endpoint=f"http://127.0.0.1:{unused_tcp_port}/search",
            )
    finally:
        await runner.cleanup()

    assert candidate == {
        "url": (
            f"http://127.0.0.1:{unused_tcp_port}/search?"
            "query=DOI%3A10.1000%2Fexample&format=json&resultType=core"
        ),
        "title": "Europe PMC primary-study record: Example primary study",
        "source_type": "primary",
        "resolution_strategy": "europe_pmc_registry",
    }


def test_registry_and_public_api_resolvers_cover_multiple_disciplines() -> None:
    assert _registry_query_for_citation(
        {"url": "https://pubmed.ncbi.nlm.nih.gov/42407080/"}
    ) == "EXT_ID:42407080"
    assert _public_api_candidate(
        {
            "url": "https://clinicaltrials.gov/study/NCT02960776",
            "title": "Trial",
        }
    ) == {
        "url": "https://clinicaltrials.gov/api/v2/studies/NCT02960776",
        "title": "ClinicalTrials.gov study record: Trial",
        "source_type": "primary",
        "resolution_strategy": "clinicaltrials_api",
    }
    assert _public_api_candidate(
        {"url": "https://arxiv.org/abs/2607.01234", "title": "Paper"}
    ) == {
        "url": "https://export.arxiv.org/api/query?id_list=2607.01234",
        "title": "arXiv primary-paper record: Paper",
        "source_type": "primary",
        "resolution_strategy": "arxiv_api",
    }


def test_generic_queries_and_classification_are_topic_agnostic() -> None:
    queries = _generic_evidence_queries(
        "Warum rosten Fahrräder?", "Feuchtigkeit reagiert mit Eisen und Sauerstoff."
    )
    assert len(queries) == 3
    assert all("rosten Fahrräder" in query for query in queries)
    assert all('"Warum rosten Fahrräder?"' not in query for query in queries)
    assert any("counterevidence" in query for query in queries)
    assert (
        _classify_search_candidate(
            "https://data.gov/report", "Official statistics report", "dataset"
        )
        == "primary"
    )
    assert (
        _classify_search_candidate(
            "https://www.example.gov/about", "Agency page", "contact information"
        )
        == "authoritative"
    )
    assert (
        _classify_search_candidate(
            "https://example.com/explainer", "Explainer", "A summary"
        )
        == "secondary"
    )
    relevant_score, relevant_terms = _candidate_relevance(
        "Warum rosten Fahrräder?",
        "Feuchtigkeit reagiert mit Eisen und Sauerstoff.",
        "Warum Fahrräder aus Eisen rosten",
        "Eine technische Erklärung zur Reaktion mit Feuchtigkeit.",
    )
    noise_score, noise_terms = _candidate_relevance(
        "Weniger Schlaf, mehr Gewicht? Zusammenhang bei Schlafmangel",
        "Eine Studie untersucht moderaten Schlafmangel.",
        "weniger - Bedeutung und Synonyme",
        "Wörterbuchdefinition",
    )
    assert relevant_score >= 0.18 and relevant_terms >= 2
    assert noise_score < 0.18 or noise_terms < 2


def test_topic_source_bootstrap_queries_use_approved_topic_and_subject_context() -> None:
    queries = _topic_source_bootstrap_queries(
        title="Warum Batterien mit der Zeit schwächer werden",
        summary="Lithium-Ionen-Akkus verlieren durch Alterung nutzbare Kapazität.",
        subject_topic="Einfache Erklärvideos zu Technik und Wissenschaft",
        research_goal="Verständliche Quellen für ein sachliches Erklärvideo finden.",
        seed_queries=["Akkualterung Forschung Fraunhofer"],
        related_concepts=["Lithium-Ionen-Akku", "Batteriechemie"],
        negative_keywords=["Tatort"],
    )

    assert 1 <= len(queries) <= 6
    assert any(
        '"Warum Batterien mit der Zeit schwächer werden"' in query for query in queries
    )
    assert any("Technik" in query and "Wissenschaft" in query for query in queries)
    assert any("Akkualterung Forschung Fraunhofer" in query for query in queries)
    assert all('-"Tatort"' in query for query in queries)


def test_topic_relevance_distinguishes_explainer_sources_from_generic_noise() -> None:
    relevant_score, relevant_terms = _candidate_relevance(
        "Warum Batterien mit der Zeit schwächer werden",
        "Erklärung der chemischen und mechanischen Alterung von Lithium-Ionen-Akkus.",
        "Warum Batterien altern und Kapazität verlieren",
        "Lithium-Ionen-Akkus werden durch Ladezyklen, Temperatur und Nebenreaktionen schwächer.",
    )
    noise_score, noise_terms = _candidate_relevance(
        "Warum Batterien mit der Zeit schwächer werden",
        "Erklärung der chemischen und mechanischen Alterung von Lithium-Ionen-Akkus.",
        "warum - Bedeutung und Grammatik",
        "Wörterbuchdefinition mit Beispielen zur Verwendung des Wortes warum.",
    )

    assert relevant_score >= 0.32 and relevant_terms >= 2
    assert noise_score < 0.32 or noise_terms < 2


def test_generic_web_relevance_rejects_adjacent_generic_topic() -> None:
    score, shared = _candidate_relevance(
        "Herausforderungen und Chancen für die Lithiumgewinnung aus geothermalen Systemen in Deutschland",
        "Vergleich der technischen Verfahren und des realistischen Potenzials.",
        "Objektorientierte Graphendarstellung von Simulink-Modellen zur einfachen Analyse und Transformation",
        "Eine Studie zu technischen Systemen in Deutschland.",
    )

    assert shared < 2 or score < 0.32


def test_scholarly_identity_collapses_aliases_but_preserves_distinct_works() -> None:
    assert _scholarly_work_identity("https://arxiv.org/abs/2201.05048v1") == "arxiv:2201.05048"
    assert (
        _scholarly_work_identity("https://doi.org/10.48550/arXiv.2201.05048")
        == "arxiv:2201.05048"
    )
    assert (
        _scholarly_work_identity(
            "https://api.openalex.org/works/W4385709095",
            doi="https://doi.org/10.3390/en16165899",
        )
        == "doi:10.3390/en16165899"
    )
    assert (
        _scholarly_work_identity(
            "https://api.openalex.org/works/W3206414838",
            doi="10.3390/en14206805",
        )
        == "doi:10.3390/en14206805"
    )


@pytest.mark.asyncio
async def test_searxng_search_applies_configured_freshness_window(
    unused_tcp_port: int,
) -> None:
    observed: dict[str, str] = {}

    async def search(request: web.Request) -> web.Response:
        observed.update(request.query)
        return web.json_response(
            {
                "results": [
                    {
                        "url": "https://example.org/report",
                        "title": "Example report",
                        "content": "Evidence summary",
                        "publishedDate": "2026-07-21T08:00:00Z",
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_get("/search", search)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    try:
        response = await search_searxng(
            f"http://127.0.0.1:{unused_tcp_port}",
            query="Faktencheck",
            language="de",
            policy=DomainPolicy(),
            lookback_days=7,
            engines=("bing", "qwant news"),
        )
    finally:
        await runner.cleanup()

    assert observed["time_range"] == "week"
    assert observed["language"] == "de"
    assert observed["engines"] == "bing,qwant news"
    assert response.results[0]["published_at"] == "2026-07-21T08:00:00Z"
    assert response.requested_engines == ("bing", "qwant news")


@pytest.mark.asyncio
async def test_searxng_search_reads_streamed_json_to_eof(unused_tcp_port: int) -> None:
    async def search(_request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(_request)
        await response.write(b'{"results":[{"url":"https://example.org/')
        await asyncio.sleep(0.01)
        await response.write(b'report","title":"Streamed report","content":"Complete"}]}')
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/search", search)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    try:
        response = await search_searxng(
            f"http://127.0.0.1:{unused_tcp_port}",
            query="streamed",
            language="de",
            policy=DomainPolicy(),
        )
    finally:
        await runner.cleanup()

    assert response.results[0]["title"] == "Streamed report"


@pytest.mark.asyncio
async def test_searxng_search_preserves_unresponsive_engine_health(
    unused_tcp_port: int,
) -> None:
    async def search(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "results": [],
                "unresponsive_engines": [
                    ["brave", "Suspended: too many requests"],
                    ["duckduckgo", "CAPTCHA"],
                ],
            }
        )

    app = web.Application()
    app.router.add_get("/search", search)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    try:
        response = await search_searxng(
            f"http://127.0.0.1:{unused_tcp_port}",
            query="health",
            language="de",
            policy=DomainPolicy(),
        )
    finally:
        await runner.cleanup()

    assert response.results == ()
    assert response.unresponsive_engines == (
        {"engine": "brave", "reason": "Suspended: too many requests"},
        {"engine": "duckduckgo", "reason": "CAPTCHA"},
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
