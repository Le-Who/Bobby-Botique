# /app/providers/tts.py
"""Gemini TTS provider — text-to-speech via REST generate_content.

Uses gemini-2.5-flash-preview-tts model (REST API) to generate speech audio.
Returns raw PCM 24kHz 16-bit mono bytes.

This is the sole production path for voice replies.  The prompt uses the
official "Director's Notes + Transcript" format with language-aware
pronunciation heuristics (Russian ё/е, stress, abbreviations, etc.).
"""

import asyncio
import logging
import re

from google.genai import types

from app.providers.gemini import get_cached_genai_client

TTS_MODEL = "gemini-2.5-flash-preview-tts"

# Available voices and their personalities:
#   Aoede  — Breezy, natural (smooth conversational narration)
#   Kore   — Firm, confident (upbeat, energetic)
#   Puck   — Upbeat male (friendly, approachable)
#   Charon — Informative (professional)
#   Fenrir — Excitable (animated)
#   Leda   — Youthful (light, bright)
#   Orus   — Firm (deep, authoritative)
#   Zephyr — Bright (clear, cheerful)
DEFAULT_VOICE = "Aoede"

# ─── Text pre-processing ──────────────────────────────────────────────────

# Compiled regexes for stripping Markdown / Telegram formatting artifacts
# that would confuse the TTS model or produce unnatural speech.
_RE_CODE_BLOCK = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_RE_INLINE_CODE = re.compile(r"`([^`]+)`")
_RE_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_RE_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_RE_BOLD_ITALIC = re.compile(r"(\*{1,3}|_{1,3})")
_RE_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_RE_BARE_URL = re.compile(r"https?://\S+")
_RE_HTML_TAG = re.compile(r"<[^>]+>")
_RE_CONSECUTIVE_NEWLINES = re.compile(r"\n{3,}")
_RE_EMOJI_CLUSTER = re.compile(
    r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
    r"\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF"
    r"\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    r"\u2600-\u26FF\u2700-\u27BF]{2,}"
)


def _clean_text_for_speech(text: str) -> str:
    """Strip formatting artifacts that degrade TTS quality.

    Operates in a careful order:
      1. Code blocks → removed entirely (never spoken)
      2. Images → removed (can't speak)
      3. Markdown links → keep visible text, drop URL
      4. Bare URLs → removed (unpronounceable)
      5. HTML tags → removed
      6. Bold/italic markers → strip (keep text)
      7. Headers → strip hashes (keep text)
      8. Emoji clusters → reduce to single (avoid "звезда звезда звезда")
      9. Collapse excessive whitespace
    """
    t = text
    t = _RE_CODE_BLOCK.sub("", t)                 # 1
    t = _RE_INLINE_CODE.sub(r"\1", t)              # 1b: keep inline code text
    t = _RE_MD_IMAGE.sub("", t)                    # 2
    t = _RE_MD_LINK.sub(r"\1", t)                  # 3
    t = _RE_BARE_URL.sub("", t)                    # 4
    t = _RE_HTML_TAG.sub("", t)                    # 5
    t = _RE_BOLD_ITALIC.sub("", t)                 # 6
    t = _RE_HEADER.sub("", t)                      # 7
    t = _RE_EMOJI_CLUSTER.sub(lambda m: m.group()[0], t)  # 8
    t = _RE_CONSECUTIVE_NEWLINES.sub("\n\n", t)    # 9
    return t.strip()


# ─── Director's Notes prompt ──────────────────────────────────────────────

# The prompt follows the Gemini TTS "Director's Notes + Transcript" pattern.
# Each section serves a specific purpose in controlling the audio output:
#
# Anti-patterns addressed:
#   - Breathiness / rasp: explicit "smooth, clear voice" + avoid "whispery"
#   - ё/е confusion: specific examples with phonetic guidance
#   - Abbreviations: read as words or spell out based on context
#   - Numbers: read in the language of surrounding text
#   - Markdown leftovers: pre-cleaned, but prompt has safety fallback
#   - Commentary injection: TTS models sometimes add "Here is... / Sure!"
#
# The prompt is intentionally in English because:
#   1. Gemini TTS models are instruction-tuned in English
#   2. The transcript language is auto-detected from the text itself
#   3. English instructions produce more reliable style adherence

_DIRECTOR_NOTES = """\
### DIRECTOR'S NOTES

**Character:** A knowledgeable, warm companion. Speak naturally, \
as if talking to a close friend — not as a newsreader or announcer.

**Delivery:**
- Clear, smooth voice. No breathiness, no rasp, no whispery artifacts.
- Natural, measured pace with organic micro-pauses at commas and periods.
- Slightly lower pitch register for calm authority.
- Vary intonation naturally; avoid monotone or robotic cadence.

**Pronunciation rules (CRITICAL — follow strictly):**
- Russian text: pronounce every word with **correct stress and diacritics \
per standard Russian phonetics**, even if the written text has errors.
- Treat 'е' as 'ё' wherever standard pronunciation demands it: \
'звездной' → read as 'звёздной', 'зеленый' → 'зелёный', \
'щелкнул' → 'щёлкнул', 'все' → 'все' or 'всё' depending on meaning.
- Common Russian abbreviations: 'ИИ' → 'ай-ай', 'ООН' → 'о-о-эн', \
'и т.д.' → 'и так далее', 'т.е.' → 'то есть', 'г.' → 'год' or 'город'.
- Numbers and dates: read in the language of the surrounding text.
- Foreign names and terms: preserve original pronunciation where possible.
- If you encounter a word that looks like a typo or misspelling, \
read the word the speaker most likely intended based on context.

**Constraints:**
- Do NOT add any words, commentary, greetings, or sign-offs.
- Do NOT say "Here is...", "Sure!", "Of course!" or similar preambles.
- Read ONLY the transcript below, nothing else.
- If the transcript contains formatting symbols (*, #, -, etc.), \
skip them silently — do not read them aloud.

### TRANSCRIPT
"""


async def generate_speech(
    text: str,
    api_key: str,
    *,
    voice: str = DEFAULT_VOICE,
    timeout: float = 90.0,
) -> bytes | None:
    """Generate speech audio from text using Gemini TTS REST API.

    Pipeline:
        1. Pre-clean text (strip Markdown, URLs, code blocks, emoji clusters)
        2. Build Director's Notes prompt (style + pronunciation rules)
        3. Call gemini-2.5-flash-preview-tts with AUDIO modality
        4. Extract raw PCM 24kHz 16-bit mono from response

    Args:
        text: Text to synthesize.
        api_key: Gemini API key.
        voice: Prebuilt voice name (default: Aoede).
        timeout: Maximum time to wait for TTS response.

    Returns:
        Raw PCM 24kHz 16-bit mono bytes, or None on failure.
    """
    if not text or not text.strip():
        return None

    client = get_cached_genai_client(api_key)

    # 1. Pre-clean: strip formatting that TTS should never speak
    clean = _clean_text_for_speech(text)
    if not clean:
        return None

    # 2. Truncate to avoid timeout on very long texts
    tts_text = clean[:2000] if len(clean) > 2000 else clean

    # 3. Build structured prompt
    prompt = _DIRECTOR_NOTES + tts_text

    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice,
                )
            )
        ),
    )

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=TTS_MODEL,
                contents=prompt,
                config=config,
            ),
            timeout=timeout,
        )

        # Extract PCM audio from inline_data in response parts
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.data:
                    audio_bytes = part.inline_data.data
                    logging.info(
                        "TTS generated: voice=%s, text_len=%d, audio_bytes=%d",
                        voice,
                        len(tts_text),
                        len(audio_bytes),
                    )
                    return audio_bytes

        logging.warning("TTS response contained no audio data")
        return None

    except TimeoutError:
        logging.error("TTS generation timed out after %.0fs", timeout)
        return None
    except Exception as e:
        logging.error("TTS generation failed: %s", e)
        return None
