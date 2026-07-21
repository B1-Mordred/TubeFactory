from __future__ import annotations

import asyncio
import json
import socket
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable
from urllib import robotparser
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from editorial_core.acquisition import validate_public_source_url
from editorial_core.discovery import canonicalize_url


ALLOWED_SOURCE_MIME_TYPES = {
    "application/atom+xml",
    "application/csv",
    "application/json",
    "application/ld+json",
    "application/pdf",
    "application/rss+xml",
    "application/xml",
    "application/xhtml+xml",
    "text/csv",
    "text/html",
    "text/plain",
    "text/xml",
}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _domain_matches(hostname: str, rule: str) -> bool:
    normalized = rule.strip().rstrip(".").casefold()
    if normalized.startswith("*."):
        normalized = normalized[2:]
    return bool(normalized) and (hostname == normalized or hostname.endswith(f".{normalized}"))


@dataclass(frozen=True)
class DomainPolicy:
    allow: tuple[str, ...] = ()
    block: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> DomainPolicy:
        value = value or {}
        return cls(
            allow=tuple(str(item) for item in value.get("allow", []) if str(item).strip()),
            block=tuple(str(item) for item in value.get("block", []) if str(item).strip()),
        )

    def permits(self, url: str) -> bool:
        hostname = (urlsplit(url).hostname or "").rstrip(".").casefold()
        if not hostname or any(_domain_matches(hostname, rule) for rule in self.block):
            return False
        return not self.allow or any(_domain_matches(hostname, rule) for rule in self.allow)


class PublicAddressResolver(AbstractResolver):
    """Resolve once, reject the whole answer if any address is not globally routable.

    aiohttp connects to the returned IP while retaining the URL hostname for TLS SNI and
    certificate checks. A fresh connector is used for every redirect hop, so DNS cannot
    be silently re-resolved by a different network layer after validation.
    """

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_UNSPEC
    ) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, family=family
        )
        addresses = tuple(dict.fromkeys(record[4][0] for record in records))
        scheme = "https" if port == 443 else "http"
        validate_public_source_url(f"{scheme}://{host}", addresses)
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": record[0],
                "proto": record[2],
                "flags": socket.AI_NUMERICHOST,
            }
            for address in addresses
            for record in records
            if record[4][0] == address
        ]

    async def close(self) -> None:
        return None


class DomainThrottle:
    def __init__(self, minimum_interval_seconds: float) -> None:
        self.minimum_interval_seconds = max(0.0, minimum_interval_seconds)
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}

    async def wait(self, hostname: str) -> None:
        lock = self._locks.setdefault(hostname, asyncio.Lock())
        async with lock:
            remaining = self.minimum_interval_seconds - (
                monotonic() - self._last_request.get(hostname, 0.0)
            )
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last_request[hostname] = monotonic()


@dataclass(frozen=True)
class AcquiredSource:
    final_url: str
    redirect_chain: tuple[str, ...]
    content_type: str
    body: bytes
    robots_cache_hit: bool


class RobotsDenied(ValueError):
    pass


@dataclass(frozen=True)
class CachedRobotsPolicy:
    robots_url: str
    body: str | None = None
    denial_reason: str | None = None

    def enforce(self, user_agent: str, target_url: str) -> None:
        if self.denial_reason:
            raise RobotsDenied(self.denial_reason)
        if self.body is not None and not robots_text_allows(
            self.robots_url, self.body, user_agent, target_url
        ):
            raise RobotsDenied("robots.txt disallows this source path")


class RobotsPolicyCache:
    """Bounded per-origin robots cache with per-origin request coalescing."""

    def __init__(
        self,
        ttl_seconds: float = 3600,
        maximum_entries: int = 1024,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.ttl_seconds = max(1.0, ttl_seconds)
        self.maximum_entries = max(1, maximum_entries)
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, CachedRobotsPolicy]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def key(target_url: str, user_agent: str) -> str:
        parts = urlsplit(target_url)
        agent_token = user_agent.split("/", 1)[0].strip().casefold() or "*"
        return f"{parts.scheme.casefold()}://{parts.netloc.casefold()}|{agent_token}"

    def get(self, key: str) -> CachedRobotsPolicy | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, policy = entry
        if expires_at <= self._clock():
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return policy

    def put(self, key: str, policy: CachedRobotsPolicy) -> None:
        self._entries[key] = (self._clock() + self.ttl_seconds, policy)
        self._entries.move_to_end(key)
        while len(self._entries) > self.maximum_entries:
            expired_key, _ = self._entries.popitem(last=False)
            lock = self._locks.get(expired_key)
            if lock is not None and not lock.locked():
                self._locks.pop(expired_key, None)

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())


_robots_policy_cache = RobotsPolicyCache()


def validate_identity_content_encoding(value: str | None, resource: str) -> None:
    encoding = (value or "identity").strip().casefold()
    if encoding not in {"", "identity"}:
        raise ValueError(f"{resource} returned an unsupported Content-Encoding")


def validate_source_target(url: str, policy: DomainPolicy) -> str:
    original_parts = urlsplit(url.strip())
    canonical = canonicalize_url(url)
    # Discovery deduplication treats `/report` and `/report/` as one candidate, but
    # HTTP servers are allowed to distinguish them. Preserve a redirect target's
    # explicit directory slash so canonicalization cannot manufacture a loop.
    if original_parts.path.endswith("/") and original_parts.path != "/":
        canonical_parts = urlsplit(canonical)
        canonical = urlunsplit(
            (
                canonical_parts.scheme,
                canonical_parts.netloc,
                canonical_parts.path + "/",
                canonical_parts.query,
                "",
            )
        )
    parts = urlsplit(canonical)
    if parts.port not in {None, 80, 443}:
        raise ValueError("source URL uses a disallowed port")
    if not policy.permits(canonical):
        raise ValueError("source URL is denied by the subject domain policy")
    return canonical


def validate_redirect_target(current_url: str, location: str, policy: DomainPolicy) -> str:
    target = validate_source_target(urljoin(current_url, location), policy)
    if urlsplit(current_url).scheme == "https" and urlsplit(target).scheme != "https":
        raise ValueError("HTTPS source redirects cannot downgrade to HTTP")
    return target


def robots_text_allows(robots_url: str, body: str, user_agent: str, target_url: str) -> bool:
    parser = robotparser.RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(body.splitlines())
    agent_token = user_agent.split("/", 1)[0].strip() or "*"
    return parser.can_fetch(agent_token, target_url)


async def enforce_robots_policy(
    target_url: str,
    *,
    policy: DomainPolicy,
    throttle: DomainThrottle,
    user_agent: str,
    maximum_redirects: int = 3,
    cache: RobotsPolicyCache = _robots_policy_cache,
) -> bool:
    target = validate_source_target(target_url, policy)
    cache_key = cache.key(target, user_agent)
    cached = cache.get(cache_key)
    if cached is not None:
        cached.enforce(user_agent, target)
        return True
    cache_lock = cache.lock(cache_key)
    async with cache_lock:
        cached = cache.get(cache_key)
        if cached is not None:
            cached.enforce(user_agent, target)
            return True
        fetched = await _fetch_robots_policy(
            target,
            policy=policy,
            throttle=throttle,
            user_agent=user_agent,
            maximum_redirects=maximum_redirects,
        )
        cache.put(cache_key, fetched)
        fetched.enforce(user_agent, target)
        return False


async def _fetch_robots_policy(
    target: str,
    *,
    policy: DomainPolicy,
    throttle: DomainThrottle,
    user_agent: str,
    maximum_redirects: int,
) -> CachedRobotsPolicy:
    parts = urlsplit(target)
    robots_url = validate_source_target(
        f"{parts.scheme}://{parts.netloc}/robots.txt", policy
    )
    timeout = aiohttp.ClientTimeout(total=12, connect=4, sock_connect=4, sock_read=6)
    current = robots_url
    for hop in range(maximum_redirects + 1):
        hostname = urlsplit(current).hostname or ""
        await throttle.wait(hostname)
        connector = aiohttp.TCPConnector(
            resolver=PublicAddressResolver(), use_dns_cache=False, force_close=True
        )
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
            headers={"User-Agent": user_agent, "Accept-Encoding": "identity"},
        ) as session:
            async with session.get(current, allow_redirects=False) as response:
                if response.status in REDIRECT_STATUSES:
                    if hop == maximum_redirects:
                        raise RobotsDenied("robots.txt exceeded the redirect limit")
                    location = response.headers.get("Location")
                    if not location:
                        raise RobotsDenied("robots.txt redirect omitted a Location header")
                    try:
                        current = validate_redirect_target(current, location, policy)
                    except ValueError as exc:
                        raise RobotsDenied(str(exc)) from exc
                    continue
                if response.status in {404, 410}:
                    return CachedRobotsPolicy(robots_url=current)
                if response.status in {401, 403}:
                    return CachedRobotsPolicy(
                        robots_url=current,
                        denial_reason="robots.txt denies automated retrieval",
                    )
                response.raise_for_status()
                try:
                    validate_identity_content_encoding(
                        response.headers.get("Content-Encoding"), "robots.txt"
                    )
                except ValueError as exc:
                    raise RobotsDenied(str(exc)) from exc
                if response.content_length is not None and response.content_length > 512_000:
                    raise RobotsDenied("robots.txt exceeds the byte limit")
                raw = await response.content.read(512_001)
                if len(raw) > 512_000:
                    raise RobotsDenied("robots.txt exceeds the byte limit")
                return CachedRobotsPolicy(
                    robots_url=current,
                    body=raw.decode("utf-8", errors="replace"),
                )
    raise AssertionError("robots redirect loop terminated unexpectedly")


async def acquire_public_source(
    url: str,
    *,
    policy: DomainPolicy,
    throttle: DomainThrottle,
    user_agent: str,
    maximum_bytes: int = 10_000_000,
    maximum_redirects: int = 5,
    robots_cache: RobotsPolicyCache = _robots_policy_cache,
) -> AcquiredSource:
    current = validate_source_target(url, policy)
    robots_cache_hit = await enforce_robots_policy(
        current,
        policy=policy,
        throttle=throttle,
        user_agent=user_agent,
        cache=robots_cache,
    )
    redirects: list[str] = []
    timeout = aiohttp.ClientTimeout(total=20, connect=5, sock_connect=5, sock_read=10)
    for hop in range(maximum_redirects + 1):
        hostname = urlsplit(current).hostname or ""
        await throttle.wait(hostname)
        connector = aiohttp.TCPConnector(
            resolver=PublicAddressResolver(), use_dns_cache=False, force_close=True
        )
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
            headers={"User-Agent": user_agent, "Accept-Encoding": "identity"},
        ) as session:
            async with session.get(current, allow_redirects=False) as response:
                if response.status in REDIRECT_STATUSES:
                    if hop == maximum_redirects:
                        raise ValueError("source exceeded the redirect limit")
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("source redirect omitted a Location header")
                    redirects.append(current)
                    current = validate_redirect_target(current, location, policy)
                    continue
                response.raise_for_status()
                validate_identity_content_encoding(
                    response.headers.get("Content-Encoding"), "source"
                )
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type not in ALLOWED_SOURCE_MIME_TYPES:
                    raise ValueError(f"source MIME type is not allowed: {content_type or 'missing'}")
                declared = response.content_length
                if declared is not None and declared > maximum_bytes:
                    raise ValueError("source exceeds the configured byte limit")
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > maximum_bytes:
                        raise ValueError("source exceeds the configured byte limit")
                return AcquiredSource(
                    current,
                    tuple(redirects),
                    content_type,
                    bytes(body),
                    robots_cache_hit,
                )
    raise AssertionError("redirect loop terminated unexpectedly")


async def search_searxng(
    endpoint: str,
    *,
    query: str,
    language: str,
    policy: DomainPolicy,
    maximum_results: int = 10,
) -> list[dict[str, Any]]:
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("SEARXNG_ENDPOINT must be an absolute HTTP(S) URL without credentials")
    timeout = aiohttp.ClientTimeout(total=20, connect=5, sock_read=12)
    async with aiohttp.ClientSession(
        timeout=timeout, cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False
    ) as session:
        async with session.get(
            endpoint.rstrip("/") + "/search",
            params={"q": query, "format": "json", "language": language, "safesearch": "1"},
            allow_redirects=False,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        ) as response:
            response.raise_for_status()
            validate_identity_content_encoding(
                response.headers.get("Content-Encoding"), "SearXNG"
            )
            if response.content_length is not None and response.content_length > 2_000_000:
                raise ValueError("SearXNG response exceeds the configured byte limit")
            raw = await response.content.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("SearXNG response exceeds the configured byte limit")
            payload = json.loads(raw)
    findings: list[dict[str, Any]] = []
    for item in payload.get("results", []):
        try:
            canonical = canonicalize_url(str(item.get("url", "")))
        except (TypeError, ValueError):
            continue
        if not policy.permits(canonical):
            continue
        title = str(item.get("title", "")).strip()[:500]
        if not title:
            continue
        findings.append(
            {
                "url": canonical,
                "title": title,
                "summary": str(item.get("content", "")).strip()[:4000],
                "published_at": item.get("publishedDate"),
                "source_type": "secondary",
            }
        )
        if len(findings) >= maximum_results:
            break
    return findings
