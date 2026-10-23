"""Provider-independent PCM contract exercised with real WAV containers."""

import io
import struct
import wave

import pytest


def wav_bytes(*, rate=24000, channels=1, width=2, frames=b"\x01\x00" * 100):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(frames)
    return output.getvalue()


@pytest.mark.parametrize(
    "mime",
    ["audio/L16;codec=pcm;rate=24000", "audio/pcm;rate=24000;channels=1", "audio/x-pcm", "audio/raw"],
)
def test_raw_pcm_preserves_samples(mime):
    from app.providers.tts_audio import normalize_tts_audio

    pcm = b"\x01\x00\xff\x7f"
    assert normalize_tts_audio(pcm, mime) == pcm


@pytest.mark.parametrize("mime", [None, "application/octet-stream"])
def test_unspecified_media_requires_explicit_provider_contract(mime):
    from app.providers.tts_audio import TTSOutputError, normalize_tts_audio

    with pytest.raises(TTSOutputError):
        normalize_tts_audio(b"\x01\x00", mime)
    assert normalize_tts_audio(b"\x01\x00", mime, allow_octet_stream=True) == b"\x01\x00"


@pytest.mark.parametrize(
    "mime",
    [
        "application/json",
        "audio/mpeg",
        "audio/ogg",
        "audio/L16;rate=48000",
        "audio/pcm;channels=2",
        "audio/pcm;bits=24",
        "audio/pcm;codec=mp3",
        "audio/pcm;rate=24000;rate=48000",
    ],
)
def test_explicit_incompatible_media_is_rejected(mime):
    from app.providers.tts_audio import TTSOutputError, normalize_tts_audio

    with pytest.raises(TTSOutputError):
        normalize_tts_audio(b"\x01\x00" * 100, mime, allow_octet_stream=True)


@pytest.mark.parametrize("pcm", [b"", b"\x01", b"\x01\x00" * 5])
def test_pcm_must_be_nonempty_aligned_and_bounded(pcm):
    from app.providers.tts_audio import TTSOutputError, normalize_tts_audio

    with pytest.raises(TTSOutputError) as error:
        normalize_tts_audio(pcm, "audio/pcm", max_bytes=8)
    assert len(error.value.reason_code) <= 64
    assert pcm.hex() not in str(error.value) or not pcm


@pytest.mark.parametrize("mime", ["audio/wav", "audio/x-wav", "application/octet-stream"])
def test_wav_metadata_is_parsed_and_compatible_frames_returned(mime):
    from app.providers.tts_audio import normalize_tts_audio

    pcm = b"\x01\x00" * 100
    data = wav_bytes(frames=pcm)
    # A valid ancillary chunk proves the adapter does not strip a fixed 44-byte header.
    ancillary = b"JUNK" + struct.pack("<I", 4) + b"test"
    data = data[:12] + ancillary + data[12:]
    data = data[:4] + struct.pack("<I", len(data) - 8) + data[8:]
    assert normalize_tts_audio(data, mime, allow_octet_stream=True) == pcm


@pytest.mark.parametrize("kwargs", [{"rate": 16000}, {"channels": 2}, {"width": 1}])
def test_incompatible_wav_is_rejected(kwargs):
    from app.providers.tts_audio import TTSOutputError, normalize_tts_audio

    with pytest.raises(TTSOutputError):
        normalize_tts_audio(wav_bytes(**kwargs), "audio/wav")


@pytest.mark.parametrize("data", [b"RIFFbrokenWAVE", wav_bytes()[:-2]])
def test_malformed_or_truncated_wav_is_rejected(data):
    from app.providers.tts_audio import TTSOutputError, normalize_tts_audio

    with pytest.raises(TTSOutputError):
        normalize_tts_audio(data, "audio/wav")


def test_wav_must_not_silently_drop_partial_sample():
    from app.providers.tts_audio import TTSOutputError, normalize_tts_audio

    with pytest.raises(TTSOutputError):
        normalize_tts_audio(wav_bytes(frames=b"x" * 201), "audio/wav")


def test_contract_error_never_echoes_untrusted_reason():
    from app.providers.tts_audio import TTSContractError, TTSRequestError

    error = TTSRequestError("untrusted body containing secret\n" * 100)
    assert isinstance(error, TTSContractError)
    assert error.reason_code == "invalid_tts_contract"
    assert "secret" not in str(error)
