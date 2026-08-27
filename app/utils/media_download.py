"""Bounded, SSRF-resistant downloads for URLs returned by media providers."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Collection, Sequence
from urllib.parse import urljoin, urlsplit

import httpx

IMAGE_MIME_TYPES = frozenset(
    {
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/tiff",
        "image/webp",
    }
)
AUDIO_MIME_TYPES = frozenset(
    {
        "audio/aac",
        "audio/flac",
        "audio/m4a",
        "audio/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/opus",
        "audio/wav",
        "audio/webm",
        "audio/x-flac",
        "audio/x-m4a",
        "audio/x-wav",
    }
)
MAX_IMAGE_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_AUDIO_DOWNLOAD_BYTES = 50 * 1024 * 1024

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

HostResolver = Callable[[str, int], Awaitable[Sequence[str]]]


class MediaDownloadError(RuntimeError):
    """A download failure whose string form never contains upstream data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


async def _resolve_host(hostname: str, port: int) -> Sequence[str]:
    try:
        results = await asyncio.get_running_loop().getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        raise MediaDownloadError("dns_error") from None

    addresses = tuple(dict.fromkeys(result[4][0] for result in results if result[4]))
    if not addresses:
        raise MediaDownloadError("dns_error")
    return addresses


def _is_forbidden_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    )


async def _validate_url(url: str, resolve_host: HostResolver) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
    except TypeError, ValueError:
        raise MediaDownloadError("invalid_url") from None

    if parsed.scheme.lower() != "https":
        raise MediaDownloadError("https_required")
    if parsed.username is not None or parsed.password is not None:
        raise MediaDownloadError("credentials_not_allowed")

    hostname = parsed.hostname
    if not hostname:
        raise MediaDownloadError("invalid_url")

    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        addresses = await resolve_host(hostname, port)
    else:
        addresses = (str(literal_address),)

    if not addresses:
        raise MediaDownloadError("dns_error")
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            raise MediaDownloadError("dns_error") from None
        if _is_forbidden_address(address):
            raise MediaDownloadError("private_address")


async def _download_with_client(
    url: str,
    *,
    allowed_mime_types: Collection[str],
    max_bytes: int,
    max_redirects: int,
    timeout: float | httpx.Timeout,
    client: httpx.AsyncClient,
    resolve_host: HostResolver,
) -> bytes:
    current_url = url
    redirects = 0
    normalized_mime_types = {mime.lower() for mime in allowed_mime_types}

    while True:
        await _validate_url(current_url, resolve_host)
        async with client.stream(
            "GET",
            current_url,
            follow_redirects=False,
            timeout=timeout,
        ) as response:
            if response.status_code in _REDIRECT_STATUSES:
                if redirects >= max_redirects:
                    raise MediaDownloadError("too_many_redirects")
                location = response.headers.get("location")
                if not location:
                    raise MediaDownloadError("invalid_redirect")
                current_url = urljoin(current_url, location)
                redirects += 1
                continue

            if not 200 <= response.status_code < 300:
                raise MediaDownloadError("http_error")

            mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if mime_type not in normalized_mime_types:
                raise MediaDownloadError("invalid_mime_type")

            raw_content_length = response.headers.get("content-length")
            if raw_content_length is not None:
                try:
                    content_length = int(raw_content_length)
                except ValueError:
                    raise MediaDownloadError("invalid_content_length") from None
                if content_length < 0:
                    raise MediaDownloadError("invalid_content_length")
                if content_length > max_bytes:
                    raise MediaDownloadError("too_large")

            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > max_bytes:
                    raise MediaDownloadError("too_large")
                content.extend(chunk)

            if not content:
                raise MediaDownloadError("empty_body")
            return bytes(content)


async def download_media(
    url: str,
    *,
    allowed_mime_types: Collection[str],
    max_bytes: int,
    max_redirects: int = 3,
    timeout: float | httpx.Timeout = 120.0,
    client: httpx.AsyncClient | None = None,
    resolve_host: HostResolver | None = None,
) -> bytes:
    """Download provider media without exposing the host network or memory."""
    if max_bytes <= 0 or max_redirects < 0 or not allowed_mime_types:
        raise MediaDownloadError("invalid_configuration")

    resolver = resolve_host or _resolve_host
    try:
        if client is not None:
            return await _download_with_client(
                url,
                allowed_mime_types=allowed_mime_types,
                max_bytes=max_bytes,
                max_redirects=max_redirects,
                timeout=timeout,
                client=client,
                resolve_host=resolver,
            )

        async with httpx.AsyncClient(follow_redirects=False) as owned_client:
            return await _download_with_client(
                url,
                allowed_mime_types=allowed_mime_types,
                max_bytes=max_bytes,
                max_redirects=max_redirects,
                timeout=timeout,
                client=owned_client,
                resolve_host=resolver,
            )
    except asyncio.CancelledError:
        raise
    except MediaDownloadError:
        raise
    except Exception:
        raise MediaDownloadError("network_error") from None
