# /app/utils/audio.py
"""Audio format conversion utilities for Telegram voice messages.

Converts raw PCM audio (from Gemini TTS/Live API) to OGG Opus format
required by Telegram's send_voice API.
"""

import asyncio
import io
import logging


async def pcm_to_ogg_opus(
    pcm_data: bytes,
    *,
    sample_rate: int = 24000,
    channels: int = 1,
    bitrate: str = "24k",
) -> bytes | None:
    """Convert raw PCM audio to OGG Opus format for Telegram send_voice.

    Uses ffmpeg subprocess for conversion.
    Input:  raw PCM (little-endian, 16-bit, mono, 24kHz) — Gemini output format.
    Output: OGG Opus bytes ready for Telegram.

    Returns None on failure (non-critical — voice reply just won't be sent).
    """
    ffmpeg_cmd = [
        "ffmpeg",
        "-f",
        "s16le",  # Input format: signed 16-bit little-endian PCM
        "-ar",
        str(sample_rate),  # Input sample rate
        "-ac",
        str(channels),  # Input channels (mono)
        "-i",
        "pipe:0",  # Read from stdin
        "-c:a",
        "libopus",  # Encode with Opus codec
        "-b:a",
        bitrate,  # Output bitrate (48k = good quality for speech)
        "-f",
        "ogg",  # Output container format
        "-application",
        "voip",  # Opus application mode: optimized for speech
        "-y",  # Overwrite (no interactive prompts)
        "-loglevel",
        "error",  # Suppress verbose output
        "pipe:1",  # Write to stdout
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=pcm_data),
            timeout=90.0,
        )
        # ⚠ pcm_data has been consumed by communicate — help GC reclaim
        # (the caller's reference may still exist, but our local copy is freed)

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace")[:500]
            del stderr
            logging.error("ffmpeg OGG encoding failed (exit %d): %s", proc.returncode, err_msg)
            return None

        del stderr  # free stderr buffer on success path too

        if not stdout or len(stdout) < 100:
            logging.error(
                "ffmpeg produced empty or suspiciously small output (%d bytes)",
                len(stdout) if stdout else 0,
            )
            return None

        logging.debug("PCM→OGG Opus: %d bytes → %d bytes", len(pcm_data), len(stdout))
        return stdout

    except FileNotFoundError:
        logging.error(
            "ffmpeg not found in PATH. Install ffmpeg to enable voice responses. On Docker: apt-get install -y ffmpeg"
        )
        return None
    except TimeoutError:
        try:
            proc.kill()
        except OSError:
            pass
        logging.error("ffmpeg encoding timed out (>90s)")
        return None
    except Exception as e:
        logging.error("Audio encoding failed: %s", e)
        return None


def make_voice_file(ogg_bytes: bytes) -> io.BytesIO:
    """Wrap OGG bytes in a BytesIO with .name for Telegram send_voice.

    Telegram requires the .name attribute to end in .ogg to recognize
    the payload as a voice note (not a generic document).
    """
    buf = io.BytesIO(ogg_bytes)
    buf.name = "voice.ogg"
    return buf


# ─── PCM post-processing ─────────────────────────────────────────────────────

# PCM format: signed 16-bit little-endian, mono, 24 kHz
_SAMPLE_BYTES = 2
_SAMPLE_RATE = 24000


def trim_trailing_silence(
    pcm: bytes,
    *,
    threshold: int = 1500,
    min_tail_ms: int = 150,
) -> bytes:
    """Remove trailing silence from raw PCM 16-bit LE mono audio.

    Gemini TTS often pads short chunks with 10-25 seconds of near-zero samples.
    This function scans backwards from the end of the buffer to find the last
    "loud" sample (|value| > threshold) and trims everything after it, keeping
    a small fade-out tail (min_tail_ms) for natural ending.

    Args:
        pcm: Raw PCM bytes (s16le, mono, 24kHz).
        threshold: Amplitude below which a sample counts as silent (out of 32768).
        min_tail_ms: Milliseconds of silence to keep after the last loud sample.

    Returns:
        Trimmed PCM bytes. Never returns empty — returns original if all silent.
    """
    import struct

    n_samples = len(pcm) // _SAMPLE_BYTES
    if n_samples < _SAMPLE_RATE:  # less than 1 second — don't trim
        return pcm

    # Scan backwards to find the last non-silent sample
    last_loud = n_samples - 1
    for i in range(n_samples - 1, -1, -1):
        sample = struct.unpack_from("<h", pcm, i * _SAMPLE_BYTES)[0]
        if abs(sample) > threshold:
            last_loud = i
            break
    else:
        # Entire buffer is silence — return as-is (caller will handle)
        return pcm

    # Keep min_tail_ms of padding after the last loud sample for fade-out
    tail_samples = int(_SAMPLE_RATE * min_tail_ms / 1000)
    cut_at = min(last_loud + tail_samples, n_samples)
    trimmed = pcm[: cut_at * _SAMPLE_BYTES]

    trimmed_duration_ms = (n_samples - cut_at) * 1000 // _SAMPLE_RATE
    if trimmed_duration_ms > 500:
        logging.debug(
            "Trimmed %dms trailing silence from PCM chunk (%d → %d bytes)",
            trimmed_duration_ms,
            len(pcm),
            len(trimmed),
        )

    return trimmed


def crossfade_pcm_chunks(
    chunks: list[bytes],
    *,
    fade_ms: int = 80,
) -> bytes:
    """Concatenate PCM chunks with cross-fade to reduce audible seams.

    Each chunk boundary gets a short linear cross-fade where the outgoing
    chunk fades out and the incoming chunk fades in, smoothing the tonal
    discontinuity between independently generated TTS segments.

    Args:
        chunks: List of raw PCM byte blobs (s16le, mono, 24kHz).
        fade_ms: Cross-fade duration in milliseconds at each boundary.

    Returns:
        Single concatenated PCM buffer with cross-fades applied.
    """
    import struct

    if not chunks:
        return b""
    if len(chunks) == 1:
        return chunks[0]

    fade_samples = int(_SAMPLE_RATE * fade_ms / 1000)

    result = bytearray(chunks[0])

    for chunk in chunks[1:]:
        if not chunk:
            continue

        overlap = min(fade_samples, len(result) // _SAMPLE_BYTES, len(chunk) // _SAMPLE_BYTES)

        if overlap < 10:
            # Too short to cross-fade — just concatenate
            result.extend(chunk)
            continue

        # Cross-fade region: last `overlap` samples of result × first `overlap` samples of chunk
        result_offset = len(result) - overlap * _SAMPLE_BYTES

        for i in range(overlap):
            # Linear fade: outgoing fades 1→0, incoming fades 0→1
            alpha = i / overlap  # 0.0 → 1.0

            out_pos = result_offset + i * _SAMPLE_BYTES
            out_sample = struct.unpack_from("<h", result, out_pos)[0]

            in_pos = i * _SAMPLE_BYTES
            in_sample = struct.unpack_from("<h", chunk, in_pos)[0]

            mixed = int(out_sample * (1.0 - alpha) + in_sample * alpha)
            mixed = max(-32768, min(32767, mixed))

            struct.pack_into("<h", result, out_pos, mixed)

        # Append the rest of the incoming chunk (after the overlap region)
        result.extend(chunk[overlap * _SAMPLE_BYTES :])

    return bytes(result)
