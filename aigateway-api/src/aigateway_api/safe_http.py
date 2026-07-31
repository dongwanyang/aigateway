"""Restricted and DNS-pinned HTTP text fetcher for administrator RAG ingestion."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpx
from fastapi import HTTPException

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
_ALLOWED_TYPES = {
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/atom+xml",
}


@dataclass(frozen=True)
class ResolvedPublicURL:
    original_url: str
    parsed: SplitResult
    hostname: str
    port: int
    addresses: tuple[str, ...]


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status,
        detail={"error": {"code": code, "message": message}},
    )


def _public_address(address: str) -> str:
    normalized = address.split("%", 1)[0]
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise _error(400, "validation_error", "Invalid resolved address") from exc
    if not parsed.is_global:
        raise _error(400, "unsafe_url", "URL resolves to a non-public address")
    return str(parsed)


def _idna_hostname(hostname: str) -> str:
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise _error(400, "validation_error", "Invalid URL hostname") from exc


async def resolve_public_url(url: str) -> ResolvedPublicURL:
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise _error(400, "validation_error", "Invalid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _error(
            400,
            "validation_error",
            "Only http/https URLs are allowed",
        )
    if parsed.username is not None or parsed.password is not None:
        raise _error(400, "unsafe_url", "URL credentials are not allowed")
    if "%" in parsed.hostname:
        raise _error(400, "unsafe_url", "Scoped IP addresses are not allowed")

    hostname = _idna_hostname(parsed.hostname)
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = (_public_address(str(literal)),)
    else:
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise _error(
                400,
                "validation_error",
                "URL hostname cannot be resolved",
            ) from exc
        unique = {_public_address(record[4][0]) for record in records}
        if not unique:
            raise _error(
                400,
                "validation_error",
                "URL hostname cannot be resolved",
            )
        addresses = tuple(
            sorted(unique, key=lambda value: ipaddress.ip_address(value))
        )
    return ResolvedPublicURL(url, parsed, hostname, port, addresses)


async def validate_public_url(url: str) -> None:
    await resolve_public_url(url)


def _pinned_url(target: ResolvedPublicURL, address: str) -> str:
    parsed_address = ipaddress.ip_address(address)
    host = f"[{address}]" if parsed_address.version == 6 else address
    netloc = f"{host}:{target.port}"
    return urlunsplit(
        (
            target.parsed.scheme,
            netloc,
            target.parsed.path or "/",
            target.parsed.query,
            "",
        )
    )


def _host_header(target: ResolvedPublicURL) -> str:
    host = target.hostname
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass
    default_port = 443 if target.parsed.scheme == "https" else 80
    return f"{host}:{target.port}" if target.port != default_port else host


def _request_extensions(target: ResolvedPublicURL) -> dict[str, bytes]:
    if target.parsed.scheme != "https":
        return {}
    try:
        ipaddress.ip_address(target.hostname)
    except ValueError:
        return {"sni_hostname": target.hostname.encode("ascii")}
    return {}


def _allowed_content_type(response: httpx.Response) -> bool:
    media_type = (
        response.headers.get("content-type", "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    return media_type.startswith("text/") or media_type in _ALLOWED_TYPES


async def _fetch_resolved(
    client: httpx.AsyncClient,
    target: ResolvedPublicURL,
) -> tuple[str, str] | tuple[None, str]:
    last_transport_error: httpx.TransportError | None = None
    for address in target.addresses:
        try:
            async with client.stream(
                "GET",
                _pinned_url(target, address),
                headers={"Host": _host_header(target)},
                extensions=_request_extensions(target),
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise _error(
                            400,
                            "validation_error",
                            "Redirect response is missing Location",
                        )
                    return None, urljoin(target.original_url, location)
                response.raise_for_status()
                if not _allowed_content_type(response):
                    raise _error(
                        400,
                        "validation_error",
                        "Unsupported URL content type",
                    )
                try:
                    declared = int(
                        response.headers.get("content-length", "0")
                    )
                except ValueError:
                    declared = 0
                if declared > MAX_RESPONSE_BYTES:
                    raise _error(
                        413,
                        "payload_too_large",
                        "URL response exceeds 5 MiB",
                    )
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise _error(
                            413,
                            "payload_too_large",
                            "URL response exceeds 5 MiB",
                        )
                filename = (
                    PurePosixPath(target.parsed.path).name[:100]
                    or "webpage"
                )
                text = bytes(payload).decode(
                    response.encoding or "utf-8",
                    errors="replace",
                )
                return text, filename
        except httpx.TransportError as exc:
            last_transport_error = exc
    if last_transport_error is not None:
        raise last_transport_error
    raise _error(400, "validation_error", "Unable to fetch URL")


async def fetch_public_text(url: str) -> tuple[str, str]:
    current = url
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "aigateway-rag-import/1.0"},
    ) as client:
        for redirect_count in range(MAX_REDIRECTS + 1):
            target = await resolve_public_url(current)
            content, value = await _fetch_resolved(client, target)
            if content is not None:
                return content, value
            if redirect_count >= MAX_REDIRECTS:
                raise _error(
                    400,
                    "validation_error",
                    "Too many redirects",
                )
            current = value
    raise _error(400, "validation_error", "Unable to fetch URL")


__all__ = [
    "ResolvedPublicURL",
    "fetch_public_text",
    "resolve_public_url",
    "validate_public_url",
]
