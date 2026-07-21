from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from research_worker.acquisition import DomainPolicy, validate_source_target


@dataclass(frozen=True)
class RenderedSource:
    body: bytes
    final_url: str
    status_code: int


def validate_firecrawl_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("FIRECRAWL_ENDPOINT must be an absolute HTTP(S) service URL")
    return endpoint.rstrip("/")


def parse_firecrawl_response(
    raw: bytes,
    *,
    requested_url: str,
    policy: DomainPolicy,
    maximum_html_bytes: int,
) -> RenderedSource:
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Firecrawl returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("Firecrawl did not return a successful scrape")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Firecrawl response omitted scrape data")
    html = data.get("rawHtml")
    if not isinstance(html, str) or not html.strip():
        raise ValueError("Firecrawl response omitted rendered HTML")
    body = html.encode("utf-8")
    if len(body) > maximum_html_bytes:
        raise ValueError("Firecrawl rendered HTML exceeds the configured byte limit")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    final_url = validate_source_target(str(metadata.get("sourceURL") or requested_url), policy)
    if urlsplit(requested_url).scheme == "https" and urlsplit(final_url).scheme != "https":
        raise ValueError("Firecrawl final URL cannot downgrade HTTPS to HTTP")
    status_code = metadata.get("statusCode", 200)
    if not isinstance(status_code, int) or not 200 <= status_code < 400:
        raise ValueError("Firecrawl reported an unsuccessful source response")
    return RenderedSource(body=body, final_url=final_url, status_code=status_code)


async def render_public_html(
    endpoint: str,
    *,
    url: str,
    policy: DomainPolicy,
    user_agent: str,
    maximum_html_bytes: int,
) -> RenderedSource:
    service = validate_firecrawl_endpoint(endpoint)
    # The direct acquisition path has already checked robots.txt and validated every
    # redirect. Firecrawl receives no credentials, actions, scripts, selectors, or
    # model prompts; its sole capability here is a fixed, bounded page render.
    request_body = {
        "url": validate_source_target(url, policy),
        "formats": ["rawHtml"],
        "onlyMainContent": False,
        "maxAge": 0,
        "headers": {"User-Agent": user_agent},
        "waitFor": 750,
        "timeout": 20_000,
        "skipTlsVerification": False,
        "removeBase64Images": True,
        "blockAds": True,
        "storeInCache": False,
    }
    timeout = aiohttp.ClientTimeout(total=30, connect=4, sock_connect=4, sock_read=25)
    async with aiohttp.ClientSession(
        timeout=timeout,
        cookie_jar=aiohttp.DummyCookieJar(),
        auto_decompress=True,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    ) as session:
        async with session.post(f"{service}/v1/scrape", json=request_body) as response:
            if response.status != 200:
                raise ValueError(f"Firecrawl scrape failed with HTTP {response.status}")
            buffer = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                buffer.extend(chunk)
                if len(buffer) > maximum_html_bytes:
                    raise ValueError("Firecrawl response exceeds the configured byte limit")
            raw = bytes(buffer)
    return parse_firecrawl_response(
        raw,
        requested_url=url,
        policy=policy,
        maximum_html_bytes=maximum_html_bytes,
    )
