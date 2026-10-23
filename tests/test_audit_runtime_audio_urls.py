"""Music downloads retain signed URL parameters and infer MIME from the path."""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.providers import freetheai_audio


@pytest.mark.parametrize(("extension", "expected_mime"), [("wav", "audio/wav"), ("ogg", "audio/ogg")])
async def test_signed_music_url_reaches_downloader_intact(monkeypatch, extension, expected_mime):
    audio_url = f"https://media.example/generated.{extension}?signature=synthetic-signature&expires=123"
    client = AsyncMock()
    client.post.return_value = httpx.Response(
        200,
        json={"choices": [{"message": {"content": f"Generated track: {audio_url}"}}]},
    )
    client.__aenter__.return_value = client
    monkeypatch.setattr(freetheai_audio.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(freetheai_audio, "_pick_key", lambda: ("synthetic-key", "synthetic-hash"))

    async def download(url, **_kwargs):
        if url != audio_url:
            raise freetheai_audio.MediaDownloadError("http_error")
        return b"synthetic-audio"

    monkeypatch.setattr(freetheai_audio, "download_media", download)

    result = await freetheai_audio.FreeTheAIAudioProvider()._generate_one_model("a melody")

    assert result.success is True
    assert result.audio_bytes == b"synthetic-audio"
    assert result.mime_type == expected_mime
    assert result.text_content == "Generated track:"
