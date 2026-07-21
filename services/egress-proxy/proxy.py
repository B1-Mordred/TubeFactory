from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from contextlib import suppress
from urllib.parse import urlsplit


LISTEN_PORT = int(os.getenv("PROXY_PORT", "3128"))
MAX_CONNECTIONS = int(os.getenv("MAX_CONNECTIONS", "64"))
MAX_HEADER_BYTES = 65_536
CONNECT_TIMEOUT_SECONDS = 8
IDLE_TIMEOUT_SECONDS = 45
ALLOWED_PORTS = {80, 443}
BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "instance-data",
    "169.254.169.254",
}
_semaphore = asyncio.Semaphore(MAX_CONNECTIONS)


class ProxyDenied(ValueError):
    pass


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def _parse_authority(authority: str, default_port: int) -> tuple[str, int]:
    value = authority.strip()
    if not value or "@" in value:
        raise ProxyDenied("credentials and empty authorities are denied")
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            raise ProxyDenied("invalid IPv6 authority")
        host = value[1:end]
        port_text = value[end + 1 :]
        port = int(port_text[1:]) if port_text.startswith(":") else default_port
    elif value.count(":") == 1:
        host, port_text = value.rsplit(":", 1)
        port = int(port_text)
    else:
        host, port = value, default_port
    host = host.rstrip(".").casefold()
    if not host or port not in ALLOWED_PORTS:
        raise ProxyDenied("only destination ports 80 and 443 are allowed")
    if host in BLOCKED_HOSTNAMES or host.endswith((".internal", ".local", ".localhost")):
        raise ProxyDenied("local and metadata hostnames are denied")
    return host, port


async def resolve_public(host: str, port: int) -> tuple[str, socket.AddressFamily]:
    loop = asyncio.get_running_loop()
    try:
        records = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise ProxyDenied("destination DNS resolution failed") from exc
    candidates: list[tuple[str, socket.AddressFamily]] = []
    for family, _type, _protocol, _canonical, sockaddr in records:
        address = sockaddr[0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ProxyDenied("resolver returned an invalid address") from exc
        if not parsed.is_global:
            raise ProxyDenied("destination resolved to a non-public address")
        candidate = (address, family)
        if candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        raise ProxyDenied("destination did not resolve")
    # The selected IP is passed directly to open_connection. The destination cannot
    # be silently re-resolved between policy enforcement and the TCP connection.
    return candidates[0]


async def open_public_connection(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    address, family = await resolve_public(host, port)
    try:
        return await asyncio.wait_for(
            asyncio.open_connection(address, port, family=family),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise ProxyDenied("destination connection failed") from exc


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await asyncio.wait_for(reader.read(65_536), timeout=IDLE_TIMEOUT_SECONDS):
            writer.write(chunk)
            await writer.drain()
    except (OSError, asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        with suppress(Exception):
            writer.close()
            await writer.wait_closed()


async def _read_headers(reader: asyncio.StreamReader) -> bytes:
    try:
        data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError) as exc:
        raise ProxyDenied("invalid or incomplete proxy request headers") from exc
    if len(data) > MAX_HEADER_BYTES:
        raise ProxyDenied("proxy request headers are too large")
    return data


async def _handle_connect(target: str, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    host, port = _parse_authority(target, 443)
    upstream_reader, upstream_writer = await open_public_connection(host, port)
    client_writer.write(b"HTTP/1.1 200 Connection Established\r\nConnection: close\r\n\r\n")
    await client_writer.drain()
    _log("connect_allowed", hostname=host, port=port)
    await asyncio.gather(
        _relay(client_reader, upstream_writer),
        _relay(upstream_reader, client_writer),
        return_exceptions=True,
    )


async def _handle_http(
    method: str,
    target: str,
    version: str,
    headers: list[bytes],
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    if method not in {"GET", "HEAD"}:
        raise ProxyDenied("unencrypted proxy requests only permit GET and HEAD")
    parts = urlsplit(target)
    if parts.scheme != "http" or not parts.hostname or parts.username or parts.password:
        raise ProxyDenied("HTTP proxy requests require an absolute public URL")
    host, port = _parse_authority(parts.netloc, 80)
    upstream_reader, upstream_writer = await open_public_connection(host, port)
    path = parts.path or "/"
    if parts.query:
        path += f"?{parts.query}"
    safe_headers = [
        line
        for line in headers
        if not line.lower().startswith((b"proxy-connection:", b"connection:"))
    ]
    request = b" ".join((method.encode(), path.encode("ascii", errors="strict"), version.encode()))
    upstream_writer.write(request + b"\r\n" + b"\r\n".join(safe_headers) + b"\r\nConnection: close\r\n\r\n")
    await upstream_writer.drain()
    _log("http_allowed", hostname=host, port=port, method=method)
    await _relay(upstream_reader, client_writer)


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    async with _semaphore:
        try:
            raw_headers = await _read_headers(reader)
            lines = raw_headers[:-4].split(b"\r\n")
            method_raw, target_raw, version_raw = lines[0].split(b" ", 2)
            method = method_raw.decode("ascii").upper()
            target = target_raw.decode("ascii")
            version = version_raw.decode("ascii")
            if version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise ProxyDenied("unsupported proxy protocol version")
            if method == "CONNECT":
                await _handle_connect(target, reader, writer)
                return
            await _handle_http(method, target, version, lines[1:], reader, writer)
        except (ProxyDenied, UnicodeError, ValueError) as exc:
            _log("request_denied", peer=str(peer), reason=str(exc)[:200])
            with suppress(Exception):
                writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
                    b"Content-Length: 23\r\nConnection: close\r\n\r\n"
                    b"outbound target denied\n"
                )
                await writer.drain()
        except Exception as exc:
            _log("proxy_error", peer=str(peer), error=type(exc).__name__)
        finally:
            with suppress(Exception):
                writer.close()
                await writer.wait_closed()


async def main() -> None:
    server = await asyncio.start_server(handle_client, "0.0.0.0", LISTEN_PORT, limit=MAX_HEADER_BYTES)
    _log("proxy_ready", port=LISTEN_PORT, max_connections=MAX_CONNECTIONS)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
