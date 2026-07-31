"""Restricted HTTP text fetcher used by administrator RAG ingestion."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit

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


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status,
        detail={"error": {"code": code, "message": message}},
    )


def _reject_non_public(address: str) -> None:
    if not ipaddress.ip_address(address.split("%", 1)[0]).is_global:
        raise _error(400, "unsafe_url", "URL resolves to a non-public address")


async def validate_public_url(url: str) -> None:
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
    try:
        literal = ipaddress.ip_address(parsed.hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        _reject_non_public(str(literal))
        return
    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise _error(
            400,
            "validation_error",
            "URL hostname cannot be resolved",
        ) from exc
    addresses = {record[4][0] for record in records}
    if not addresses:
        raise _error(
            400,
            "validation_error",
            "URL hostname cannot be resolved",
        )
    for address in addresses:
        _reject_non_public(address)


def _allowed_content_type(response: httpx.Response) -> bool:
    media_type = (
        response.headers.get("content-type", "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    return media_type.startswith("text/") or media_type in _ALLOWED_TYPES


async def fetch_public_text(url: str) -> tuple[str, str]:
    current = url
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "aigateway-rag-import/1.0"},
    ) as client:
        for redirect_count in range(MAX_REDIRECTS + 1):
            await validate_public_url(current)
            async with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or redirect_count >= MAX_REDIRECTS:
                        raise _error(
                            400,
                            "validation_error",
                            "Too many or invalid redirects",
                        )
                    current = urljoin(current, location)
                    continue
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
                    PurePosixPath(urlsplit(current).path).name[:100]
                    or "webpage"
                )
                text = bytes(payload).decode(
                    response.encoding or "utf-8",
                    errors="replace",
                )
                return text, filename
    raise _error(400, "validation_error", "Unable to fetch URL")


__all__ = ["fetch_public_text", "validate_public_url"]
