# app/middleware/debounce.py
"""Message debounce — merges rapid-fire messages from the same user into a single
AI request.

Architecture upgrade (v2):
    The slot no longer stores raw text strings. It stores a list of
    ``_MessageEntry`` objects preserving the original ``telegram.Message``
    reference, the resolved author label, and an optional pending STT future
    for forwarded voice messages.

    This enables callers to synthesise rich, structured context blocks for the
    LLM (author attribution, timestamps, user-vs-forwarded split) instead of
    the previous dumb ``"\\n".join(texts)``.

Public API:
    debounce_message(user_id, message, *, bot) → DebounceResult | None

    Returns None when the message is absorbed (caller must return immediately).
    Returns a DebounceResult when the window fires — containing all accumulated
    entries and convenience accessors.

Debounce windows:
    _DEFAULT_WINDOW_S = 1.0  — normal typing / split-tap messages
    _FORWARD_WINDOW_S = 3.5  — forwarded burst (Telegram may batch-deliver over
                                several seconds; block=False now eliminates the
                                artificial PTB-internal delay, so 3.5 s is the
                                actual Telegram CDN delivery window)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Message

logger = logging.getLogger(__name__)

# ── Timing constants ─────────────────────────────────────────────────────────
_DEFAULT_WINDOW_S = 1.0
_FORWARD_WINDOW_S = 3.5

# Maximum time we will wait for a pending STT future beyond the debounce window.
# If STT finishes before this, transcript is included; otherwise a placeholder
# stub is inserted and STT continues independently.
_STT_GRACE_S = 8.0

# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class _MessageEntry:
    """One message accumulation unit inside a debounce slot."""

    message: Message
    """Original telegram.Message object."""

    text: str
    """Resolved display text (caption / voice transcript / message text)."""

    author_label: str
    """Human-readable attribution label, e.g. 'Иван Петров', 'Канал Известия'."""

    forwarded_date: datetime | None
    """Original send date from forward_origin, or None for user-authored."""

    is_forwarded: bool
    """True when message carries forward_origin."""

    is_user_authored: bool
    """True for messages the user typed themselves (no forward_origin)."""

    stt_future: asyncio.Future[str] | None = field(default=None, repr=False)
    """Pending STT future for forwarded voice messages. None for text/photo."""


@dataclass
class DebounceResult:
    """Result returned from debounce_message when the window fires."""

    entries: list[_MessageEntry]

    # ── Convenience accessors ────────────────────────────────────────────────

    @property
    def forwarded_entries(self) -> list[_MessageEntry]:
        return [e for e in self.entries if e.is_forwarded]

    @property
    def user_entries(self) -> list[_MessageEntry]:
        return [e for e in self.entries if e.is_user_authored]

    @property
    def has_mixed(self) -> bool:
        """True when both user-authored and forwarded entries are present."""
        return bool(self.user_entries) and bool(self.forwarded_entries)

    def build_llm_context(self) -> str:
        """Build a structured, attribution-rich text block for the LLM.

        Layout:
            If there are user-authored messages, they are placed FIRST as the
            explicit instruction / question, separated from the forwarded block.

        Format for forwarded messages:
            > [DD Месяц, HH:MM | Author]: text

        This layout is maximally clear for summarisation, dialogue analysis,
        and instruction-following tasks.
        """
        parts: list[str] = []

        # 1. User's own instruction (if any) — verbatim, no author prefix
        user_texts = [e.text for e in self.user_entries if e.text.strip()]
        if user_texts:
            parts.append("\n".join(user_texts))

        # 2. Forwarded block (sorted by original date when available)
        fwd = sorted(
            self.forwarded_entries,
            key=lambda e: e.forwarded_date or datetime.min.replace(tzinfo=UTC),
        )
        if fwd:
            if user_texts:
                parts.append("\n--- Пересланные сообщения ---")
            fwd_lines: list[str] = []
            for e in fwd:
                prefix = _format_fwd_prefix(e.author_label, e.forwarded_date)
                # Handle multi-line texts gracefully
                text_body = e.text.strip()
                if "\n" in text_body:
                    # Indent continuation lines with quote marker
                    indented = "\n".join(
                        f"> {line}" for line in text_body.splitlines()
                    )
                    fwd_lines.append(f"{prefix}\n{indented}")
                else:
                    fwd_lines.append(f"{prefix} {text_body}")
            parts.append("\n".join(fwd_lines))

        return "\n\n".join(parts)


# ── Internal slot ─────────────────────────────────────────────────────────────


class _DebounceSlot:
    """Per-user accumulator."""

    __slots__ = ("entries", "first_ts", "timer_task", "ready_event", "is_forward_burst")

    def __init__(self, entry: _MessageEntry) -> None:
        import time

        self.entries: list[_MessageEntry] = [entry]
        self.first_ts: float = time.monotonic()
        self.timer_task: asyncio.Task[None] | None = None
        self.ready_event: asyncio.Event = asyncio.Event()
        self.is_forward_burst: bool = entry.is_forwarded


# ── Per-user slot map ─────────────────────────────────────────────────────────
_debounce_slots: dict[int, _DebounceSlot] = {}


# ── Public API ────────────────────────────────────────────────────────────────


async def debounce_message(
    user_id: int,
    message: Message,
    *,
    bot=None,  # telegram.Bot — used for reaction feedback; optional
) -> DebounceResult | None:
    """Accumulate *message* for *user_id* and wait for the debounce window.

    Returns:
        None      → message absorbed; caller must return immediately.
        DebounceResult → window fired; caller proceeds with the merged context.
    """
    entry = await _build_entry(message, bot=bot)
    slot = _debounce_slots.get(user_id)

    async def _timer(wait_time: float) -> None:
        await asyncio.sleep(wait_time)
        nonlocal slot
        if slot is not None:
            slot.ready_event.set()

    if slot is not None and not slot.ready_event.is_set():
        # Window still open — absorb
        slot.entries.append(entry)
        if entry.is_forwarded:
            slot.is_forward_burst = True

        logger.debug(
            "Debounce: absorbed msg for user %d (now %d parts, fwd=%s)",
            user_id,
            len(slot.entries),
            entry.is_forwarded,
        )

        # Restart the trailing timer with the appropriate window
        current_timeout = _FORWARD_WINDOW_S if slot.is_forward_burst else _DEFAULT_WINDOW_S
        if slot.timer_task is not None:
            slot.timer_task.cancel()
        slot.timer_task = asyncio.create_task(_timer(current_timeout))
        return None

    # First message — open a new slot
    slot = _DebounceSlot(entry)
    _debounce_slots[user_id] = slot

    current_timeout = _FORWARD_WINDOW_S if entry.is_forwarded else _DEFAULT_WINDOW_S
    slot.timer_task = asyncio.create_task(_timer(current_timeout))

    # Block until window fires
    await slot.ready_event.wait()

    # Await any pending STT futures with a bounded grace period
    await _resolve_stt_futures(slot.entries)

    # Harvest
    result = DebounceResult(entries=list(slot.entries))
    _debounce_slots.pop(user_id, None)

    if len(slot.entries) > 1:
        logger.info(
            "Debounce: merged %d messages for user %d (fwd=%d, user=%d)",
            len(slot.entries),
            user_id,
            len(result.forwarded_entries),
            len(result.user_entries),
        )

    return result


# ── Legacy shim — keeps existing callers working ──────────────────────────────


async def debounce_text_message(user_id: int, text: str, is_forward: bool = False) -> str | None:
    """Compatibility shim: accepts raw text and returns merged text.

    New code should call ``debounce_message`` directly to get the richer
    ``DebounceResult``.  This shim is retained so any call-sites not yet
    migrated continue to work.
    """
    # We can't reconstruct a real Message object here, so we build a minimal
    # slot directly, bypassing the new entry machinery.
    slot = _debounce_slots.get(user_id)

    async def _timer(wait_time: float) -> None:
        nonlocal slot
        await asyncio.sleep(wait_time)
        if slot is not None:
            slot.ready_event.set()

    # Wrap in a lightweight _MessageEntry-like object using a mock
    # (avoids importing telegram.Message at module level)
    class _FakeMsg:
        forward_origin = None
        text = text
        voice = None
        caption = None

    fake = _FakeMsg()

    if slot is not None and not slot.ready_event.is_set():
        # Reconstruct entry inline for the shim
        entry = _MessageEntry(
            message=fake,  # type: ignore[arg-type]
            text=text,
            author_label="",
            forwarded_date=None,
            is_forwarded=is_forward,
            is_user_authored=not is_forward,
        )
        slot.entries.append(entry)
        if is_forward:
            slot.is_forward_burst = True

        current_timeout = _FORWARD_WINDOW_S if slot.is_forward_burst else _DEFAULT_WINDOW_S
        if slot.timer_task is not None:
            slot.timer_task.cancel()
        slot.timer_task = asyncio.create_task(_timer(current_timeout))
        return None

    entry = _MessageEntry(
        message=fake,  # type: ignore[arg-type]
        text=text,
        author_label="",
        forwarded_date=None,
        is_forwarded=is_forward,
        is_user_authored=not is_forward,
    )
    slot = _DebounceSlot(entry)
    _debounce_slots[user_id] = slot

    current_timeout = _FORWARD_WINDOW_S if is_forward else _DEFAULT_WINDOW_S
    slot.timer_task = asyncio.create_task(_timer(current_timeout))

    await slot.ready_event.wait()
    merged = "\n".join(e.text for e in slot.entries)
    _debounce_slots.pop(user_id, None)

    if len(slot.entries) > 1:
        logger.info(
            "Debounce shim: merged %d messages for user %d (%d chars)",
            len(slot.entries),
            user_id,
            len(merged),
        )

    return merged


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _build_entry(message: Message, *, bot=None) -> _MessageEntry:
    """Extract structured metadata from a telegram.Message into a _MessageEntry."""
    from datetime import datetime

    forward_origin = getattr(message, "forward_origin", None)
    is_forwarded = forward_origin is not None
    is_user_authored = not is_forwarded

    # ── Author label ─────────────────────────────────────────────────────────
    author_label = _extract_author_label(forward_origin)

    # ── Forwarded date ───────────────────────────────────────────────────────
    forwarded_date: datetime | None = None
    if forward_origin is not None:
        raw_date = getattr(forward_origin, "date", None)
        if raw_date is not None:
            if isinstance(raw_date, datetime):
                forwarded_date = raw_date.replace(tzinfo=UTC) if raw_date.tzinfo is None else raw_date
            else:
                # Integer UNIX timestamp
                forwarded_date = datetime.fromtimestamp(int(raw_date), tz=UTC)

    # ── Text resolution ──────────────────────────────────────────────────────
    text = (
        message.text
        or message.caption
        or ""
    )

    # ── Voice STT (fire-and-forget future, resolved after window closes) ─────
    stt_future: asyncio.Future[str] | None = None
    if is_forwarded and message.voice:
        stt_future = asyncio.get_event_loop().create_future()
        asyncio.create_task(_transcribe_forwarded_voice(message, stt_future))  # noqa: RUF006

    # ── Reaction feedback ────────────────────────────────────────────────────
    # Put 👀 on the message to confirm the bot received it into the debounce slot
    if bot is not None:
        asyncio.create_task(_set_absorbed_reaction(message, bot))  # noqa: RUF006

    return _MessageEntry(
        message=message,
        text=text,
        author_label=author_label,
        forwarded_date=forwarded_date,
        is_forwarded=is_forwarded,
        is_user_authored=is_user_authored,
        stt_future=stt_future,
    )


def _extract_author_label(forward_origin) -> str:
    """Map a MessageOrigin object to a human-readable label.

    Handles all four variants from Telegram Bot API 7.0+:
        MessageOriginUser        — real user
        MessageOriginHiddenUser  — privacy-protected user (name only, no id)
        MessageOriginChat        — group / supergroup
        MessageOriginChannel     — channel post (with optional author_signature)
    """
    if forward_origin is None:
        return ""

    origin_type = getattr(forward_origin, "type", "")

    if origin_type == "user":
        sender = getattr(forward_origin, "sender_user", None)
        if sender:
            parts = [sender.first_name or ""]
            if sender.last_name:
                parts.append(sender.last_name)
            return " ".join(parts).strip() or "Пользователь"
        return "Пользователь"

    if origin_type == "hidden_user":
        return getattr(forward_origin, "sender_user_name", None) or "Скрытый пользователь"

    if origin_type == "chat":
        sender_chat = getattr(forward_origin, "sender_chat", None)
        title = (getattr(sender_chat, "title", None) or "") if sender_chat else ""
        author_sig = getattr(forward_origin, "author_signature", None) or ""
        if title and author_sig:
            return f"{title} / {author_sig}"
        return title or author_sig or "Группа"

    if origin_type == "channel":
        chat = getattr(forward_origin, "chat", None)
        title = (getattr(chat, "title", None) or "") if chat else ""
        author_sig = getattr(forward_origin, "author_signature", None) or ""
        if title and author_sig:
            return f"{title} / {author_sig}"
        return title or author_sig or "Канал"

    return "Неизвестный источник"


def _format_fwd_prefix(author_label: str, forwarded_date: datetime | None) -> str:
    """Format the blockquote prefix for a forwarded message line."""
    MONTHS_RU = [
        "", "Янв", "Фев", "Мар", "Апр", "Май", "Июн",
        "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек",
    ]
    if forwarded_date is not None:
        date_str = f"{forwarded_date.day} {MONTHS_RU[forwarded_date.month]}, {forwarded_date.hour:02d}:{forwarded_date.minute:02d}"
        if author_label:
            return f"> **[{date_str} | {author_label}]:**"
        return f"> **[{date_str}]:**"
    if author_label:
        return f"> **[{author_label}]:**"
    return ">"


async def _set_absorbed_reaction(message: Message, bot) -> None:
    """Place 👀 reaction on a message to confirm it entered the debounce slot."""
    from telegram import ReactionTypeEmoji

    try:
        await bot.set_message_reaction(
            chat_id=message.chat_id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji="👀")],
            is_big=False,
        )
    except Exception as exc:
        # Reactions are non-critical; swallow all errors silently
        logger.debug("Could not set absorbed reaction: %s", exc)


async def _transcribe_forwarded_voice(message: Message, future: asyncio.Future[str]) -> None:
    """Download and transcribe a forwarded voice message, resolving the future.

    Errors are caught and the future is resolved with an error stub so the
    debounce window can still close and process the remaining text entries.
    """
    try:
        voice = message.voice
        if voice is None:
            if not future.done():
                future.set_result("")
            return
        voice_file = await voice.get_file()
        voice_bytes = bytes(await voice_file.download_as_bytearray())

        from app.utils.multimodal_processor import transcribe_voice

        transcript, _intent, _draw = await transcribe_voice(voice_bytes, mime_type="audio/ogg")
        future.set_result(transcript or "")
    except Exception as exc:
        logger.warning("STT for forwarded voice failed: %s", exc)
        if not future.done():
            future.set_result("")  # resolve with empty — caller inserts stub


async def _resolve_stt_futures(entries: list[_MessageEntry]) -> None:
    """After the debounce window closes, wait (with grace period) for STT futures.

    Per Улучшение 6 design note: STT may take longer than the debounce window
    (503 errors, network latency, model retries).  We apply _STT_GRACE_S as an
    additional budget BEYOND the already-elapsed window time, capped per future.

    If a future is still unresolved after the grace period, we substitute a
    placeholder stub and let the separate STT task continue asynchronously —
    it will not block the main AI request.
    """
    voice_entries = [e for e in entries if e.stt_future is not None]
    if not voice_entries:
        return

    for entry in voice_entries:
        assert entry.stt_future is not None
        try:
            transcript = await asyncio.wait_for(
                asyncio.shield(entry.stt_future),
                timeout=_STT_GRACE_S,
            )
            if transcript:
                entry.text = transcript
            else:
                entry.text = "[Голосовое сообщение — расшифровка недоступна]"
        except TimeoutError:
            logger.warning(
                "STT grace period exhausted for forwarded voice, inserting stub"
            )
            entry.text = "[Голосовое сообщение — расшифровка обрабатывается]"
            # Don't cancel the future — the background task will finish eventually
