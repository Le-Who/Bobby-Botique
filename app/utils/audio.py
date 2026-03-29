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
            timeout=15.0,
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
        logging.error("ffmpeg encoding timed out (>15s)")
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
