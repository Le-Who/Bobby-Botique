"""Security contract for bounded downloads of provider-returned media URLs."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _public_resolver() -> AsyncMock:
    return AsyncMock(return_value=("93.184.216.34",))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://media.example/image.png",
        "https://user:password@media.example/image.png",
        "https://127.0.0.1/image.png",
        "https://10.0.0.1/image.png",
        "https://169.254.1.1/image.png",
        "https://224.0.0.1/image.png",
        "https://240.0.0.1/image.png",
        "https://0.0.0.0/image.png",
        "https://[::1]/image.png",
        "https://[fe80::1]/image.png",
        "https://[ff02::1]/image.png",
        "https://[::]/image.png",
    ],
)
async def test_unsafe_url_is_rejected_before_transport_request(url: str) -> None:
    from app.utils.media_download import MediaDownloadError, download_media

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"data")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MediaDownloadError):
            await download_media(
                url,
                allowed_mime_types={"image/png"},
                max_bytes=4,
                client=client,
                resolve_host=_public_resolver(),
            )

    assert requests == []


@pytest.mark.asyncio
async def test_hostname_resolving_to_private_ip_is_rejected_before_request() -> None:
    from app.utils.media_download import MediaDownloadError, download_media

    requests: list[httpx.Request] = []
    resolver = AsyncMock(return_value=("10.1.2.3",))

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"data")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MediaDownloadError, match="private_address"):
            await download_media(
                "https://media.example/image.png",
                allowed_mime_types={"image/png"},
                max_bytes=4,
                client=client,
                resolve_host=resolver,
            )

    resolver.assert_awaited_once_with("media.example", 443)
    assert requests == []


@pytest.mark.asyncio
async def test_redirect_to_private_ip_is_rejected_before_second_request() -> None:
    from app.utils.media_download import MediaDownloadError, download_media

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://169.254.169.254/latest/meta-data"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MediaDownloadError, match="private_address"):
            await download_media(
                "https://media.example/image.png",
                allowed_mime_types={"image/png"},
                max_bytes=4,
                client=client,
                resolve_host=_public_resolver(),
            )

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_redirect_count_is_bounded() -> None:
    from app.utils.media_download import MediaDownloadError, download_media

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "/next"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MediaDownloadError, match="too_many_redirects"):
            await download_media(
                "https://media.example/start",
                allowed_mime_types={"image/png"},
                max_bytes=4,
                max_redirects=2,
                client=client,
                resolve_host=_public_resolver(),
            )

    assert len(requests) == 3


@pytest.mark.asyncio
async def test_oversized_content_length_is_rejected_before_body_read() -> None:
    from app.utils.media_download import MediaDownloadError, download_media

    stream = _ChunkStream(b"never-read")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": "5"},
            stream=stream,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MediaDownloadError, match="too_large"):
            await download_media(
                "https://media.example/image.png",
                allowed_mime_types={"image/png"},
                max_bytes=4,
                client=client,
                resolve_host=_public_resolver(),
            )

    assert stream.closed is True


@pytest.mark.asyncio
async def test_streamed_body_is_rejected_when_it_crosses_hard_cap() -> None:
    from app.utils.media_download import MediaDownloadError, download_media

    stream = _ChunkStream(b"abc", b"de")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            stream=stream,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MediaDownloadError, match="too_large"):
            await download_media(
                "https://media.example/image.png",
                allowed_mime_types={"image/png"},
                max_bytes=4,
                client=client,
                resolve_host=_public_resolver(),
            )

    assert stream.closed is True


@pytest.mark.asyncio
async def test_exact_limit_stream_is_accepted() -> None:
    from app.utils.media_download import download_media

    stream = _ChunkStream(b"ab", b"cd")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            stream=stream,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await download_media(
            "https://media.example/image.png",
            allowed_mime_types={"image/png"},
            max_bytes=4,
            client=client,
            resolve_host=_public_resolver(),
        )

    assert result == b"abcd"
    assert stream.closed is True


@pytest.mark.asyncio
async def test_mime_type_must_be_allowlisted() -> None:
    from app.utils.media_download import MediaDownloadError, download_media

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"nope",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MediaDownloadError, match="invalid_mime_type"):
            await download_media(
                "https://media.example/image.png",
                allowed_mime_types={"image/png"},
                max_bytes=4,
                client=client,
                resolve_host=_public_resolver(),
            )


@pytest.mark.asyncio
async def test_network_error_does_not_log_raw_url_or_exception(caplog) -> None:
    from app.utils.media_download import MediaDownloadError, download_media

    raw_secret = "RAW-UPSTREAM-SECRET"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(raw_secret, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with caplog.at_level(logging.DEBUG), pytest.raises(MediaDownloadError) as error:
            await download_media(
                "https://private-name.example/secret-path",
                allowed_mime_types={"image/png"},
                max_bytes=4,
                client=client,
                resolve_host=_public_resolver(),
            )

    assert str(error.value) == "network_error"
    assert raw_secret not in caplog.text
    assert "private-name.example" not in caplog.text
    assert "secret-path" not in caplog.text


@pytest.mark.asyncio
async def test_fta_image_url_response_uses_bounded_downloader(monkeypatch) -> None:
    from app.providers import freetheai_image

    image_url = "https://media.example/generated.png"
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": [{"url": image_url}]}
    download_response = MagicMock(content=b"legacy-download")
    download_response.raise_for_status.return_value = None
    client = AsyncMock()
    client.post.return_value = response
    client.get.return_value = download_response
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    downloader = AsyncMock(return_value=b"bounded-image")
    monkeypatch.setattr(freetheai_image, "_pick_key", lambda: ("api-key", "hash"))
    monkeypatch.setattr(freetheai_image, "download_media", downloader, raising=False)
    monkeypatch.setattr(freetheai_image.httpx, "AsyncClient", lambda **kwargs: client)

    result = await freetheai_image.FreeTheAIImageProvider().generate("cat")

    assert result.images == [b"bounded-image"]
    downloader.assert_awaited_once()
    assert downloader.await_args.args == (image_url,)
    assert "image/png" in downloader.await_args.kwargs["allowed_mime_types"]
    assert downloader.await_args.kwargs["max_bytes"] > 0


@pytest.mark.asyncio
async def test_fta_audio_url_response_uses_bounded_downloader(monkeypatch) -> None:
    from app.providers import freetheai_audio

    audio_url = "https://media.example/generated.mp3"
    response = MagicMock(status_code=200)
    response.json.return_value = {"choices": [{"message": {"content": audio_url}}]}
    download_response = MagicMock(content=b"legacy-download")
    download_response.raise_for_status.return_value = None
    client = AsyncMock()
    client.post.return_value = response
    client.get.return_value = download_response
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    downloader = AsyncMock(return_value=b"bounded-audio")
    monkeypatch.setattr(freetheai_audio, "_pick_key", lambda: ("api-key", "hash"))
    monkeypatch.setattr(freetheai_audio, "download_media", downloader, raising=False)
    monkeypatch.setattr(freetheai_audio.httpx, "AsyncClient", lambda **kwargs: client)

    result = await freetheai_audio.FreeTheAIAudioProvider().generate("jazz")

    assert result.audio_bytes == b"bounded-audio"
    downloader.assert_awaited_once()
    assert downloader.await_args.args == (audio_url,)
    assert "audio/mpeg" in downloader.await_args.kwargs["allowed_mime_types"]
    assert downloader.await_args.kwargs["max_bytes"] > 0


@pytest.mark.asyncio
async def test_pollinations_response_url_uses_bounded_downloader(monkeypatch) -> None:
    from app.providers import pollinations

    image_url = "https://media.example/generated.webp"
    downloader = AsyncMock(return_value=b"bounded-image")
    monkeypatch.setattr(pollinations, "download_media", downloader, raising=False)

    result = await pollinations._extract_b64_or_url_bytes({"data": [{"url": image_url}]})

    assert result == [b"bounded-image"]
    downloader.assert_awaited_once()
    assert downloader.await_args.args == (image_url,)
    assert "image/webp" in downloader.await_args.kwargs["allowed_mime_types"]
    assert downloader.await_args.kwargs["max_bytes"] > 0


@pytest.mark.asyncio
async def test_pollinations_direct_get_fallback_uses_bounded_downloader(monkeypatch) -> None:
    from app.providers import pollinations
    from app.repos import provider_keys

    downloader = AsyncMock(return_value=b"bounded-direct-image")
    monkeypatch.setattr(pollinations, "download_media", downloader)
    monkeypatch.setattr(provider_keys, "get_provider_key", AsyncMock(return_value="runtime-key"))

    result = await pollinations.PollinationsProvider()._try_get(
        prompt="private prompt",
        model="flux",
        width=512,
        height=512,
        seed=1,
        enhance=False,
        negative_prompt="",
        timeout=5,
    )

    assert result.success is True
    assert result.images == [b"bounded-direct-image"]
    downloader.assert_awaited_once()
    assert downloader.await_args.kwargs["max_bytes"] == pollinations.MAX_IMAGE_DOWNLOAD_BYTES
    assert "key=runtime-key" in downloader.await_args.args[0]


@pytest.mark.asyncio
async def test_pollinations_rejects_oversized_inline_base64_before_decode(monkeypatch) -> None:
    from app.providers import pollinations

    monkeypatch.setattr(pollinations, "MAX_IMAGE_DOWNLOAD_BYTES", 4)
    oversized = "A" * (((pollinations.MAX_IMAGE_DOWNLOAD_BYTES + 2) // 3) * 4 + 4)

    result = await pollinations._extract_b64_or_url_bytes({"data": [{"b64_json": oversized}]})

    assert result == []


@pytest.mark.asyncio
async def test_fta_image_rejects_oversized_inline_base64(monkeypatch) -> None:
    from app.providers import freetheai_image

    monkeypatch.setattr(freetheai_image, "MAX_IMAGE_DOWNLOAD_BYTES", 4)
    oversized = "A" * (((freetheai_image.MAX_IMAGE_DOWNLOAD_BYTES + 2) // 3) * 4 + 4)
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": [{"b64_json": oversized}]}
    client = AsyncMock()
    client.post.return_value = response
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    monkeypatch.setattr(freetheai_image, "_pick_key", lambda: ("api-key", "hash"))
    monkeypatch.setattr(freetheai_image.httpx, "AsyncClient", lambda **kwargs: client)

    result = await freetheai_image.FreeTheAIImageProvider().generate("cat")

    assert result.success is False
    assert result.error_message == "empty_response"


def test_fta_audio_rejects_oversized_inline_base64(monkeypatch) -> None:
    from app.providers import freetheai_audio

    monkeypatch.setattr(freetheai_audio, "MAX_AUDIO_DOWNLOAD_BYTES", 4)
    oversized = "A" * (((freetheai_audio.MAX_AUDIO_DOWNLOAD_BYTES + 2) // 3) * 4 + 4)
    content = f"data:audio/mpeg;base64,{oversized}"

    audio, _mime, _remaining = freetheai_audio._extract_audio_from_response(content)

    assert audio is None
