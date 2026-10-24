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
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Message

from app.utils.background_tasks import submit_task
from app.utils.tg_file import get_file_bytes

logger = logging.getLogger(__name__)

# ── Timing constants ─────────────────────────────────────────────────────────
_DEFAULT_WINDOW_S = 1.0
_FORWARD_WINDOW_S = 3.5
_SPEECH_WINDOW_S = 15.0
_SPEECH_TOTAL_S = 60.0
_SPEECH_MAX_PARTS = 32
_SPEECH_MAX_BYTES = 256 * 1024
_SPEECH_MAX_SLOTS = 128

# Maximum time we will wait for a pending STT future beyond the debounce window.
# If STT finishes before this, transcript is included; otherwise a placeholder
# stub is inserted and STT continues independently.
_STT_GRACE_S = 8.0

# Performance: hoisted from _format_fwd_prefix() — eliminates a 13-element list
# allocation on every forwarded message entry rendered in build_llm_context().
# Index 0 is a dummy empty string so months map 1-based (Jan=1 … Dec=12).
_MONTHS_RU: tuple[str, ...] = (
    "",
    "Янв",
    "Фев",
    "Мар",
    "Апр",
    "Май",
    "Июн",
    "Июл",
    "Авг",
    "Сен",
    "Окт",
    "Ноя",
    "Дек",
)

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

    stt_task: asyncio.Task | None = field(default=None, repr=False)
    """Owned transcription task, cancelled if the slot owner exits early."""


@dataclass
class DebounceResult:
    """Result returned from debounce_message when the window fires."""

    entries: list[_MessageEntry]
    speech_collection: bool = False
    speech_epoch: int | None = None
    speech_error: str = ""

    # ── Convenience accessors ────────────────────────────────────────────────

    @property
    def has_forwarded_content(self) -> bool:
        """True when the batch contains at least one forwarded message."""
        return any(e.is_forwarded for e in self.entries)

    @property
    def forwarded_photo_messages(self) -> list[Message]:
        """Original telegram.Message objects for all forwarded photos."""
        return [e.message for e in self.forwarded_entries if e.message.photo]

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
            parts.append("\n<forwarded_dialogue>")
            fwd_lines: list[str] = []
            for e in fwd:
                prefix = _format_fwd_prefix(e.author_label, e.forwarded_date)
                # Handle multi-line texts gracefully
                text_body = e.text.strip()
                if "\n" in text_body:
                    # Indent continuation lines with quote marker
                    indented = "\n".join(f"> {line}" for line in text_body.splitlines())
                    fwd_lines.append(f"{prefix}\n{indented}")
                else:
                    fwd_lines.append(f"{prefix} {text_body}")
            parts.append("\n".join(fwd_lines))
            parts.append("</forwarded_dialogue>")

            # Improvement 2: If the user sent NO instructions, add a fallback intent
            if not user_texts:
                parts.append(
                    "\n<task>\n"
                    "Пользователь переслал стенограмму выше без явного комментария.\n"
                    "Ваша задача: выступить в роли беспристрастного аналитика. "
                    "Проанализируйте содержание <forwarded_dialogue> и предоставьте структурированную выжимку: "
                    "укажите ключевые тезисы, участников и общий смысл обсуждения.\n"
                    "</task>"
                )

        return "\n\n".join(parts)


# ── Internal slot ─────────────────────────────────────────────────────────────


class _DebounceSlot:
    """Per-user accumulator."""

    __slots__ = ("entries", "first_ts", "timer_task", "ready_event", "is_forward_burst")

    def __init__(self, entry: _MessageEntry) -> None:
        self.entries: list[_MessageEntry] = [entry]
        self.first_ts: float = time.monotonic()
        self.timer_task: asyncio.Task[None] | None = None
        self.ready_event: asyncio.Event = asyncio.Event()
        self.is_forward_burst: bool = entry.is_forwarded


# ── Per-user slot map ─────────────────────────────────────────────────────────
_debounce_slots: dict[int, _DebounceSlot] = {}


@dataclass
class _SpeechSlot:
    entries: list[_MessageEntry]
    chat_id: int
    epoch: int | None
    owner: asyncio.Task
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    finished: bool = False
    finish_requested: bool = False
    error: str = ""
    retained_bytes: int = 0
    first_ts: float = field(default_factory=time.monotonic)
    last_ts: float = field(default_factory=time.monotonic)
    notices: list[Message] = field(default_factory=list)
    pending_ids: set[int] = field(default_factory=set)


_speech_slots: dict[int, _SpeechSlot] = {}


@dataclass
class _SpeechHint:
    """Metadata-only ownership while the first update passes admission."""

    owner: asyncio.Task
    chat_id: int
    first_message_id: int
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    first_ts: float = field(default_factory=time.monotonic)
    pending_ids: set[int] = field(default_factory=set)
    collection: _SpeechSlot | None = None
    error: str = ""


_speech_hints: dict[int, _SpeechHint] = {}


@dataclass(frozen=True)
class SpeechIngressReservation:
    """One physical source's metadata-only claim on its exact collection owner."""

    continuation: bool = False
    capacity: bool = False
    target: _SpeechSlot | _SpeechHint | None = None
    message_id: int = 0


def reserve_speech_hint(user_id: int, message: Message) -> SpeechIngressReservation:
    if accepts_speech_message(user_id, message):
        target = _speech_slots.get(user_id) or _speech_hints[user_id]
        if len(target.pending_ids) >= _SPEECH_MAX_PARTS:
            return SpeechIngressReservation(True, True, target, message.message_id)
        target.pending_ids.add(message.message_id)
        return SpeechIngressReservation(True, False, target, message.message_id)
    if user_id in _speech_slots or user_id in _speech_hints or not starts_long_speech(message):
        return SpeechIngressReservation()
    if len(_speech_hints) + len(_speech_slots) >= _SPEECH_MAX_SLOTS:
        return SpeechIngressReservation()
    owner = asyncio.current_task()
    assert owner is not None
    _speech_hints[user_id] = _SpeechHint(owner, message.chat_id, message.message_id)
    return SpeechIngressReservation()


def release_speech_reservation(reservation: SpeechIngressReservation) -> None:
    target = reservation.target
    if target is not None and not reservation.capacity:
        target.pending_ids.discard(reservation.message_id)
        slot = target if isinstance(target, _SpeechSlot) else target.collection
        if slot is not None:
            slot.changed.set()


def release_speech_hint(user_id: int, owner: asyncio.Task) -> None:
    hint = _speech_hints.get(user_id)
    if hint is not None and hint.owner is owner:
        _speech_hints.pop(user_id, None)
        hint.ready.set()


async def wait_for_speech_collection(
    user_id: int, message: Message, reservation: SpeechIngressReservation | None = None
) -> bool:
    target = reservation.target if reservation else None
    hint = target if isinstance(target, _SpeechHint) else _speech_hints.get(user_id)
    if not has_open_speech_slot(user_id, message.chat_id) and hint is not None:
        remaining = _SPEECH_TOTAL_S - (time.monotonic() - hint.first_ts)
        try:
            await asyncio.wait_for(hint.ready.wait(), timeout=max(0, remaining))
        except TimeoutError:
            return False
    slot = _speech_slots.get(user_id)
    expected_slot = target.collection if isinstance(target, _SpeechHint) else target
    if reservation is not None:
        return slot is not None and slot is expected_slot and not slot.finished
    return has_open_speech_slot(user_id, message.chat_id) and accepts_speech_message(user_id, message)


async def capture_reserved_speech_message(
    user_id: int, message: Message, reservation: SpeechIngressReservation, *, bot=None
) -> bool:
    if reservation.capacity or not await wait_for_speech_collection(user_id, message, reservation):
        return False
    await _collect_speech_message(user_id, message, bot=bot, speech_epoch=_speech_slots[user_id].epoch)
    return True


def reject_speech_reservation(reservation: SpeechIngressReservation) -> None:
    target = reservation.target
    if isinstance(target, _SpeechHint):
        target.error = "source_admission"
    slot = target if isinstance(target, _SpeechSlot) else target.collection if target is not None else None
    if slot is not None:
        slot.error = "source_admission"
        slot.finished = True
        slot.changed.set()


def _near_telegram_limit(text: str) -> bool:
    # Desktop can choose a preferred word/newline cut anywhere after half the
    # scalar limit; some clients instead enforce the UTF-16 representation.
    return 2049 <= len(text) <= 4096 or 4094 <= len(text.encode("utf-16-le")) // 2 <= 4096


def has_hard_speech_boundary(text: str) -> bool:
    """Infer an exact hard client cut; discarded client whitespace is unknowable."""
    return len(text) == 4096 or len(text.encode("utf-16-le")) // 2 == 4096


def starts_long_speech(message: Message) -> bool:
    """Only an explicit, near-limit inline source starts continuation capture."""
    from app.voice_intent import resolve_speech_request

    text = message.text or ""
    if getattr(message, "forward_origin", None) or not _near_telegram_limit(text):
        return False
    intent = resolve_speech_request(user_texts=[text])
    return intent.action == "speak" and intent.source == "inline"


def has_open_speech_slot(user_id: int, chat_id: int) -> bool:
    slot = _speech_slots.get(user_id)
    return slot is not None and slot.chat_id == chat_id and not slot.finished


def accepts_speech_message(user_id: int, message: Message) -> bool:
    """A short final source or an explicit reply elsewhere closes association."""
    slot = _speech_slots.get(user_id)
    if slot is None:
        hint = _speech_hints.get(user_id)
        if (
            hint is None
            or hint.chat_id != getattr(message, "chat_id", 0)
            or time.monotonic() - hint.first_ts >= _SPEECH_TOTAL_S
            or message.message_id <= hint.first_message_id
        ):
            return False
        source_ids = {hint.first_message_id}
    else:
        if not has_open_speech_slot(user_id, getattr(message, "chat_id", 0)):
            return False
        tail = max(slot.entries, key=lambda entry: entry.message.message_id)
        # Concurrent admission may deliver an earlier source after its short
        # terminal source. Keep that earlier ID inside the open window.
        if message.message_id < min(entry.message.message_id for entry in slot.entries):
            return False
        if not _near_telegram_limit(tail.text) and message.message_id > tail.message.message_id:
            return False
        if slot.finish_requested and message.message_id > tail.message.message_id:
            return False
        source_ids = {entry.message.message_id for entry in slot.entries}
    if getattr(message, "forward_origin", None) or any(
        getattr(message, name, None) for name in ("photo", "voice", "document", "video", "audio")
    ):
        return False
    text = message.text or ""
    # Bare commands are an explicit control escape. Preserve account erasure's
    # confirmation as well; slash-prefixed prose with payload remains source data.
    if re.fullmatch(r"/[a-z0-9_]+(?:@[a-z0-9_]+)?\s*", text, re.IGNORECASE) or re.fullmatch(
        r"/deleteme(?:@[a-z0-9_]+)?\s+CONFIRM\s*", text, re.IGNORECASE
    ):
        return False
    reply = getattr(message, "reply_to_message", None)
    return reply is None or getattr(reply, "message_id", None) in source_ids


def speech_collection_epoch(user_id: int) -> int | None:
    return _speech_slots[user_id].epoch


def clear_user_speech_collection(user_id: int) -> None:
    """Reset/erasure scrubs retained text immediately and cancels its owner."""
    hint = _speech_hints.pop(user_id, None)
    if hint is not None:
        hint.ready.set()
        if not hint.owner.done():
            hint.owner.cancel()
    slot = _speech_slots.pop(user_id, None)
    if slot is not None:
        slot.entries.clear()
        slot.pending_ids.clear()
        slot.finished = True
        slot.changed.set()
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if not slot.owner.done() and slot.owner is not current_task:
            slot.owner.cancel()


def _speech_tail_is_complete(slot: _SpeechSlot) -> bool:
    text = max(slot.entries, key=lambda entry: entry.message.message_id).text
    return not has_hard_speech_boundary(text) or text[-1:].isspace() or text.endswith(tuple('.!?…。！？»"”'))


async def _collect_speech_message(
    user_id: int,
    message: Message,
    *,
    bot=None,
    speech_epoch: int | None,
    load_speech_epoch: Callable[[], Awaitable[int | None]] | None = None,
) -> DebounceResult | None:
    """Keep one bounded, private, explicit source collection owned by its waiter."""
    chat_id = message.chat_id
    slot = _speech_slots.get(user_id)
    if slot is not None and slot.chat_id == chat_id and not slot.finished:
        if slot.epoch != speech_epoch:
            slot.error = "stale_epoch"
            slot.finished = True
        elif (message.text or "").strip().casefold() in ("готово", "done"):
            slot.finish_requested = True
        else:
            if any(item.message.message_id == message.message_id for item in slot.entries):
                return None
            entry = await _build_entry(message, bot=bot)
            size = len(entry.text.encode("utf-8"))
            if len(slot.entries) >= _SPEECH_MAX_PARTS or slot.retained_bytes + size > _SPEECH_MAX_BYTES:
                # Include the rejected source identity so the terminal error
                # explicitly covers every submitted physical part.
                slot.entries.append(entry)
                slot.error = "collection_capacity"
                slot.finished = True
            else:
                slot.entries.append(entry)
                slot.retained_bytes += size
                slot.last_ts = time.monotonic()
        slot.changed.set()
        return None

    entry = await _build_entry(message, bot=bot)
    if slot is not None or len(_speech_slots) >= _SPEECH_MAX_SLOTS:
        return DebounceResult(
            [entry], speech_collection=True, speech_epoch=speech_epoch, speech_error="collection_capacity"
        )
    owner = asyncio.current_task()
    assert owner is not None
    slot = _SpeechSlot([entry], chat_id, speech_epoch, owner, retained_bytes=len(entry.text.encode("utf-8")))
    _speech_slots[user_id] = slot
    hint = _speech_hints.get(user_id)
    if hint is not None and hint.owner is owner:
        slot.pending_ids = hint.pending_ids
        slot.first_ts = hint.first_ts
        hint.collection = slot
        if hint.error:
            slot.error, slot.finished = hint.error, True
        hint.ready.set()
    cancellation = None
    try:
        # Reservation precedes the snapshot read. Concurrent physical sources
        # can be captured while the epoch is still provisional; no provider job
        # is admitted until the owner's exact epoch has been pinned here.
        if load_speech_epoch is not None:
            slot.epoch = await load_speech_epoch()
        slot.notices.append(
            await message.reply_text(
                "Собираю части текста для озвучивания. После длинной части жду продолжение до 15 секунд, "
                "после короткой — 1 секунду. «Готово» подтверждает конец текста. "
                "Если конец длинной части неоднозначен, сохраняю части до 60 секунд. "
                "Отдельное голосовое сообщение будет ответом на каждую исходную часть."
            )
        )
        idle_notice_sent = False
        while not slot.finished:
            # Clear before reading state. A Telegram notice can await network
            # I/O while another admitted physical source sets this event.
            slot.changed.clear()
            remaining_total = _SPEECH_TOTAL_S - (time.monotonic() - slot.first_ts)
            if remaining_total <= 0:
                slot.error = "" if not slot.pending_ids and _speech_tail_is_complete(slot) else "collection_incomplete"
                break
            if slot.pending_ids:
                try:
                    await asyncio.wait_for(slot.changed.wait(), timeout=remaining_total)
                except TimeoutError:
                    pass
                continue
            if slot.finish_requested:
                break
            complete = _speech_tail_is_complete(slot)
            tail_text = max(slot.entries, key=lambda entry: entry.message.message_id).text
            idle_window = _DEFAULT_WINDOW_S if not _near_telegram_limit(tail_text) else _SPEECH_WINDOW_S
            remaining_idle = idle_window - (time.monotonic() - slot.last_ts)
            if remaining_idle <= 0:
                if complete:
                    break
                if not idle_notice_sent:
                    slot.notices.append(
                        await message.reply_text(
                            "Конец длинной части может быть обрезан внутри слова. Пришлите продолжение "
                            "или «Готово», если текст полный. Все части сохранены до конца 60-секундного сбора."
                        )
                    )
                    idle_notice_sent = True
                    continue
                remaining_idle = remaining_total
            try:
                await asyncio.wait_for(slot.changed.wait(), timeout=min(remaining_idle, remaining_total))
            except TimeoutError:
                pass
        return DebounceResult(
            list(slot.entries), speech_collection=True, speech_epoch=slot.epoch, speech_error=slot.error
        )
    except asyncio.CancelledError as error:
        cancellation = error
        raise
    finally:
        if _speech_slots.get(user_id) is slot:
            _speech_slots.pop(user_id, None)
        slot.entries.clear()
        await _retire_speech_notices(slot.notices, cancellation=cancellation)


async def _retire_speech_notices(notices: list[Message], *, cancellation: asyncio.CancelledError | None) -> None:
    async def retire() -> None:
        try:
            await asyncio.wait_for(
                asyncio.gather(*(notice.delete() for notice in notices), return_exceptions=True), timeout=3
            )
        except TimeoutError:
            pass
        finally:
            notices.clear()

    cleanup = asyncio.create_task(retire())
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
    cleanup.result()
    if cancellation is not None:
        raise cancellation


# ── Public API ────────────────────────────────────────────────────────────────


def has_open_slot(user_id: int) -> bool:
    """Return True when user_id has an active debounce window that hasn't fired yet.

    Used by msg_media.py to check if a forwarded photo should be merged into the
    current text debounce slot instead of processed independently.
    """
    slot = _debounce_slots.get(user_id)
    return slot is not None and not slot.ready_event.is_set()


async def inject_forwarded_photo(user_id: int, message: Message, *, bot=None) -> bool:
    """Inject a forwarded photo Message into an existing open debounce slot.

    Called by msg_media.py when a forwarded photo arrives while the user already
    has an active text-debounce window.  Merges the photo into the slot so the
    first waiter (text handler) will receive a DebounceResult containing both
    text and photo entries.  The slot window is NOT restarted — we let the
    existing timer fire to avoid unbounded delays.

    Returns:
        True  → photo was injected (caller in msg_media.py should return early).
        False → no open slot; caller should process photo normally.
    """
    slot = _debounce_slots.get(user_id)
    if slot is None or slot.ready_event.is_set():
        return False

    entry = await _build_entry(message, bot=bot)
    slot.entries.append(entry)
    slot.is_forward_burst = True

    logger.info(
        "Debounce: injected forwarded photo for user %d (slot now %d entries)",
        user_id,
        len(slot.entries),
    )
    return True


async def debounce_message(
    user_id: int,
    message: Message,
    *,
    bot=None,  # telegram.Bot — used for reaction feedback; optional
    speech_epoch: int | None = None,
    load_speech_epoch: Callable[[], Awaitable[int | None]] | None = None,
) -> DebounceResult | None:
    """Accumulate *message* for *user_id* and wait for the debounce window.

    Returns:
        None      → message absorbed; caller must return immediately.
        DebounceResult → window fired; caller proceeds with the merged context.
    """
    if accepts_speech_message(user_id, message) or starts_long_speech(message):
        return await _collect_speech_message(
            user_id, message, bot=bot, speech_epoch=speech_epoch, load_speech_epoch=load_speech_epoch
        )
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

    completed = False
    cancellation = None
    try:
        # Block until window fires, then resolve voice entries within the grace budget.
        await slot.ready_event.wait()
        await _resolve_stt_futures(slot.entries)
        result = DebounceResult(entries=list(slot.entries))
        completed = True
    except asyncio.CancelledError as error:
        cancellation = error
        raise
    finally:
        await _release_slot(user_id, slot, cancel_stt=not completed, cancellation=cancellation)

    if len(slot.entries) > 1:
        logger.info(
            "Debounce: merged %d messages for user %d (fwd=%d, user=%d)",
            len(slot.entries),
            user_id,
            len(result.forwarded_entries),
            len(result.user_entries),
        )

    return result


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _release_slot(
    user_id: int,
    slot: _DebounceSlot,
    *,
    cancel_stt: bool,
    cancellation: asyncio.CancelledError | None = None,
) -> None:
    """Release only this owner's slot and await its cancelled child work."""
    if _debounce_slots.get(user_id) is slot:
        _debounce_slots.pop(user_id, None)
    tasks = [slot.timer_task] if slot.timer_task is not None else []
    if cancel_stt:
        for entry in slot.entries:
            if entry.stt_future is not None and not entry.stt_future.done():
                entry.stt_future.cancel()
            if entry.stt_task is not None:
                tasks.append(entry.stt_task)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        completion = asyncio.gather(*tasks, return_exceptions=True)
        while not completion.done():
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError as error:
                # Keep the cancelled transcription owned until its async
                # cleanup finishes, including another shutdown cancellation.
                cancellation = cancellation or error
        completion.result()
    if cancellation is not None:
        raise cancellation


async def _build_entry(message: Message, *, bot=None) -> _MessageEntry:
    """Extract structured metadata from a telegram.Message into a _MessageEntry."""
    # Performance: `datetime` is already imported at module level — no inline import needed.
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
    text = message.text or message.caption or ""

    # ── Voice STT (fire-and-forget future, resolved after window closes) ─────
    stt_future: asyncio.Future[str] | None = None
    stt_task: asyncio.Task | None = None
    if is_forwarded and message.voice:
        stt_future = asyncio.get_event_loop().create_future()
        stt_task = submit_task(_transcribe_forwarded_voice(message, stt_future))

    # ── Reaction feedback ────────────────────────────────────────────────────
    # Put 👀 on the message to confirm the bot received it into the debounce slot
    if bot is not None:
        submit_task(_set_absorbed_reaction(message, bot))

    return _MessageEntry(
        message=message,
        text=text,
        author_label=author_label,
        forwarded_date=forwarded_date,
        is_forwarded=is_forwarded,
        is_user_authored=is_user_authored,
        stt_future=stt_future,
        stt_task=stt_task,
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
    # Performance: _MONTHS_RU is a module-level tuple — no per-call list allocation.
    if forwarded_date is not None:
        date_str = f"{forwarded_date.day} {_MONTHS_RU[forwarded_date.month]}, {forwarded_date.hour:02d}:{forwarded_date.minute:02d}"
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
        voice_bytes = await get_file_bytes(message.get_bot(), voice_file)

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
            logger.warning("STT grace period exhausted for forwarded voice, inserting stub")
            entry.text = "[Голосовое сообщение — расшифровка обрабатывается]"
            # Don't cancel the future — the background task will finish eventually
