"""Bounded audio contract shared by the specialized speech providers.

Providers return mono 24 kHz signed 16-bit little-endian samples. Compatible
WAV containers are decoded using their metadata before crossing this boundary.
"""

from __future__ import annotations

import io
import re
import wave

PCM_SAMPLE_RATE = 24000
MAX_TTS_AUDIO_BYTES = 32 * 1024 * 1024
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_RAW_MIME_TYPES = frozenset({"audio/l16", "audio/pcm", "audio/x-pcm", "audio/raw"})
_WAV_MIME_TYPES = frozenset({"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"})


class TTSContractError(RuntimeError):
    """A request/output contract failure that must not penalize a credential."""

    def __init__(self, reason_code: str):
        self.reason_code = (
            reason_code
            if isinstance(reason_code, str) and _REASON_CODE.fullmatch(reason_code)
            else "invalid_tts_contract"
        )
        super().__init__(self.reason_code)


class TTSOutputError(TTSContractError):
    """The provider returned incomplete, incompatible or oversized audio."""


class TTSRequestError(TTSContractError):
    """The local request cannot satisfy the configured speech contract."""


def _media_type(mime_type: str | None, allow_octet_stream: bool) -> str:
    if mime_type is None or not mime_type.strip():
        if allow_octet_stream:
            return "application/octet-stream"
        raise TTSOutputError("missing_audio_mime")
    fields = mime_type.lower().split(";")
    media = fields[0].strip()
    if media not in _RAW_MIME_TYPES | _WAV_MIME_TYPES and not (
        allow_octet_stream and media == "application/octet-stream"
    ):
        raise TTSOutputError("unsupported_audio_mime")
    parameters: dict[str, str] = {}
    for field in fields[1:]:
        key, separator, value = field.strip().partition("=")
        if not separator or key in parameters:
            raise TTSOutputError("invalid_audio_parameters")
        parameters[key] = value.strip().strip('"')
    supported = {
        "rate": {"24000"},
        "channels": {"1"},
        "bits": {"16"},
        "codec": {"pcm", "s16le", "pcm_s16le"},
    }
    if any(value not in supported.get(key, set()) for key, value in parameters.items()):
        raise TTSOutputError("incompatible_audio_parameters")
    return media


def _wav_pcm(data: bytes) -> bytes:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise TTSOutputError("invalid_wav")
    if int.from_bytes(data[4:8], "little") + 8 != len(data):
        raise TTSOutputError("truncated_wav")
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            if (
                reader.getnchannels() != 1
                or reader.getframerate() != PCM_SAMPLE_RATE
                or reader.getsampwidth() != 2
                or reader.getcomptype() != "NONE"
            ):
                raise TTSOutputError("incompatible_wav")
            frames = reader.getnframes()
            # readframes rounds the declared data size down when computing
            # nframes. Probe one extra frame to reject a partial final sample.
            pcm = reader.readframes(frames + 1)
            if len(pcm) != frames * 2:
                raise TTSOutputError("truncated_wav")
            return pcm
    except (wave.Error, EOFError, ValueError) as error:
        raise TTSOutputError("invalid_wav") from error


def normalize_tts_audio(
    data: bytes,
    mime_type: str | None,
    *,
    allow_octet_stream: bool = False,
    max_bytes: int = MAX_TTS_AUDIO_BYTES,
) -> bytes:
    """Validate provider audio and return the common raw PCM representation.

    Missing/octet-stream MIME is accepted only for an explicitly negotiated
    raw provider endpoint. An incompatible explicit MIME is always rejected.
    """
    if not isinstance(data, bytes) or not data:
        raise TTSOutputError("empty_audio")
    if len(data) > max_bytes:
        raise TTSOutputError("audio_too_large")
    media = _media_type(mime_type, allow_octet_stream)
    if media in _WAV_MIME_TYPES or data.startswith(b"RIFF"):
        data = _wav_pcm(data)
    if not data:
        raise TTSOutputError("empty_audio")
    if len(data) % 2:
        raise TTSOutputError("unaligned_pcm")
    return data
