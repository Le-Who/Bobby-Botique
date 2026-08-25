"""Telegram rendering, message splitting, and Long Read publication."""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app.response_delivery.normalization import strip_hallucinated_tool_trace
from app.utils.background_tasks import submit_task
from app.utils.formatting import TelegramFormatter
from app.utils.text_format import sanitize_html_tags, split_text_safe
from app.utils.ux_improvements import wrap_in_expandable_blockquote


class DeliveryKind(StrEnum):
    MESSAGE = "message"
    SPLIT = "split"
    READER = "reader"
    TELEGRAPH = "telegraph"
    DEFERRED = "deferred"
    FAILURE = "failure"


class RendererProtocolError(RuntimeError):
    pass


class TelegramDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramMessageRef:
    chat_id: int
    message_id: int
    message_thread_id: int | None = None


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    kind: DeliveryKind
    message_ids: tuple[int, ...]
    final_message: TelegramMessageRef
    publication_url: str | None = None


class TelegramTransport(Protocol):
    current_ref: TelegramMessageRef

    async def edit(
        self,
        text: str,
        *,
        parse_mode: str | None,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> None: ...

    async def send(
        self,
        text: str,
        *,
        parse_mode: str | None,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> TelegramMessageRef: ...


class TelegramMessageTransport:
    """Private python-telegram-bot transport used by the renderer."""

    def __init__(self, message, *, bot=None, chat_id: int | None = None):
        self._message = message
        self._bot = bot
        self._chat_id = chat_id or getattr(message, "chat_id", None) or message.chat.id
        self.current_ref = self._ref(message)

    @staticmethod
    def _ref(message) -> TelegramMessageRef:
        chat_id = getattr(message, "chat_id", None)
        if chat_id is None:
            chat_id = message.chat.id
        return TelegramMessageRef(
            chat_id=chat_id,
            message_id=message.message_id,
            message_thread_id=getattr(message, "message_thread_id", None),
        )

    async def edit(self, text, *, parse_mode, reply_markup) -> None:
        from telegram import LinkPreviewOptions

        await self._message.edit_text(
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def send(self, text, *, parse_mode, reply_markup) -> TelegramMessageRef:
        from telegram import LinkPreviewOptions

        kwargs = {
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
            "allow_sending_without_reply": True,
            "link_preview_options": LinkPreviewOptions(is_disabled=True),
        }
        try:
            message = await self._message.reply_text(**kwargs)
        except Exception as exc:
            lowered = str(exc).lower()
            if "not found" not in lowered and "message to be replied" not in lowered:
                raise
            bot = self._bot or self._message.get_bot()
            message = await bot.send_message(
                chat_id=self._chat_id,
                message_thread_id=getattr(self._message, "message_thread_id", None),
                **kwargs,
            )
        self._message = message
        self.current_ref = self._ref(message)
        return self.current_ref


class TelegramBotTransport:
    """Send-only transport for deferred responses without a placeholder message."""

    def __init__(self, bot, *, chat_id: int, message_thread_id: int | None = None):
        self._bot = bot
        self._chat_id = chat_id
        self._message_thread_id = message_thread_id
        self.current_ref = TelegramMessageRef(
            chat_id=chat_id,
            message_id=0,
            message_thread_id=message_thread_id,
        )

    async def edit(self, text, *, parse_mode, reply_markup) -> None:
        raise RuntimeError("Send-only Telegram target has no message to edit")

    async def send(self, text, *, parse_mode, reply_markup) -> TelegramMessageRef:
        from telegram import LinkPreviewOptions

        message = await self._bot.send_message(
            chat_id=self._chat_id,
            message_thread_id=self._message_thread_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        self.current_ref = TelegramMessageRef(
            chat_id=getattr(message, "chat_id", self._chat_id),
            message_id=message.message_id,
            message_thread_id=getattr(message, "message_thread_id", self._message_thread_id),
        )
        return self.current_ref


StoreLongMessage = Callable[[str, str], Awaitable[bool]]
CreateTelegraphPage = Callable[[str, str], Awaitable[str | None]]
StoreTelegraphUrl = Callable[[str, str], Awaitable[bool]]
SubmitBackground = Callable[[Coroutine[Any, Any, Any]], Any]


async def _store_long_message(uid: str, text: str) -> bool:
    from app.cache import store_long_message

    return await store_long_message(uid, text)


async def _create_telegraph_page(title: str, text: str) -> str | None:
    from app.utils.telegraph import create_telegraph_page

    return await create_telegraph_page(title, text)


async def _store_telegraph_url(uid: str, url: str) -> bool:
    from app.cache import store_telegraph_url

    return await store_telegraph_url(uid, url)


def _formatted_html(markdown: str) -> str:
    formatted, _ = TelegramFormatter.format_text(markdown)
    return sanitize_html_tags(formatted) or markdown


def _prepend_row(
    base: InlineKeyboardMarkup | None,
    row: list[InlineKeyboardButton],
) -> InlineKeyboardMarkup:
    base_rows = [list(existing_row) for existing_row in (base.inline_keyboard if base else ())]
    return InlineKeyboardMarkup([row, *base_rows])


class TelegramRenderer:
    def __init__(
        self,
        transport: TelegramTransport,
        *,
        message_limit: int = 4000,
        webapp_base_url: str | None = None,
        store_long_message: StoreLongMessage = _store_long_message,
        create_telegraph_page: CreateTelegraphPage = _create_telegraph_page,
        store_telegraph_url: StoreTelegraphUrl = _store_telegraph_url,
        submit_background: SubmitBackground = submit_task,
        telegraph_publication_enabled: bool | None = None,
        private_content: bool = False,
        debounce_seconds: float = 0.6,
        min_chunk_size: int = 24,
    ) -> None:
        self._transport = transport
        self._message_limit = message_limit
        if webapp_base_url is None or telegraph_publication_enabled is None:
            from app.config import settings

            if webapp_base_url is None:
                webapp_base_url = getattr(settings, "WEBAPP_BASE_URL", "")
            if telegraph_publication_enabled is None:
                telegraph_publication_enabled = bool(getattr(settings, "TELEGRAPH_PUBLICATION_ENABLED", False))
        self._webapp_base_url = (webapp_base_url or "").rstrip("/")
        self._store_long_message = store_long_message
        self._create_telegraph_page = create_telegraph_page
        self._store_telegraph_url = store_telegraph_url
        self._submit_background = submit_background
        self._telegraph_publication_enabled = bool(telegraph_publication_enabled)
        self._private_content = private_content
        self._debounce_seconds = debounce_seconds
        self._min_chunk_size = min_chunk_size

    def open(self) -> TelegramRenderSession:
        return TelegramRenderSession(self)

    @staticmethod
    def _is_rate_limited(error: Exception) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in ("429", "flood", "too many requests", "retry_after")
        )

    async def _edit_with_retry(
        self,
        text: str,
        *,
        parse_mode: str | None,
        reply_markup: InlineKeyboardMarkup | None,
        max_attempts: int = 3,
    ) -> None:
        """Bounded retry for Telegram flood control; other failures stay recoverable."""
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                await self._transport.edit(
                    text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if "not modified" in str(error).lower():
                    return
                last_error = error
                if not self._is_rate_limited(error) or attempt == max_attempts - 1:
                    raise
                backoff = (0.5 * (2**attempt)) + random.uniform(0, 0.3)
                self._debounce_seconds = min(self._debounce_seconds * 1.5, 3.0)
                await asyncio.sleep(backoff)
        assert last_error is not None
        raise last_error

    async def _replace_or_send(
        self,
        text: str,
        *,
        parse_mode: str | None,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> tuple[TelegramMessageRef, bool]:
        try:
            await self._edit_with_retry(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            return self._transport.current_ref, False
        except asyncio.CancelledError:
            raise
        except Exception as edit_error:
            logging.warning("Telegram final edit failed; trying send-new recovery: %s", edit_error)
            try:
                ref = await self._transport.send(
                    text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
                return ref, True
            except asyncio.CancelledError:
                raise
            except Exception as send_error:
                raise TelegramDeliveryError(
                    f"Telegram edit and send recovery failed: {send_error}"
                ) from send_error

    async def _split(
        self,
        displayed_text: str,
        actions: InlineKeyboardMarkup | None,
    ) -> DeliveryReceipt:
        html = _formatted_html(displayed_text)
        parts = split_text_safe(html, max_length=self._message_limit)
        if not parts:
            raise TelegramDeliveryError("Telegram split produced no deliverable parts")

        refs: list[TelegramMessageRef] = []
        first_markup = actions if len(parts) == 1 else None
        first_ref, _ = await self._replace_or_send(
            parts[0],
            parse_mode="HTML",
            reply_markup=first_markup,
        )
        refs.append(first_ref)
        for index, part in enumerate(parts[1:], start=1):
            markup = actions if index == len(parts) - 1 else None
            try:
                ref = await self._transport.send(
                    part,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise TelegramDeliveryError(
                    f"Telegram split failed on part {index + 1}: {exc}"
                ) from exc
            refs.append(ref)

        return DeliveryReceipt(
            kind=DeliveryKind.SPLIT if len(refs) > 1 else DeliveryKind.MESSAGE,
            message_ids=tuple(ref.message_id for ref in refs),
            final_message=refs[-1],
        )

    @staticmethod
    def _summary(displayed_text: str) -> str:
        summary = displayed_text[:800].strip()
        if len(displayed_text) > 800:
            summary += "…"
        return wrap_in_expandable_blockquote(_formatted_html(summary))

    async def _reader(
        self,
        *,
        uid: str,
        displayed_text: str,
        actions: InlineKeyboardMarkup | None,
        title: str,
    ) -> DeliveryReceipt | None:
        try:
            stored = await self._store_long_message(uid, displayed_text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.warning("Reader storage raised for uid=%s: %s", uid, exc)
            stored = False
        if not stored:
            return None

        reader_url = f"{self._webapp_base_url}/webapp/reader?id={uid}"
        markup = _prepend_row(
            actions,
            [
                InlineKeyboardButton(
                    "📄 Развернуть статью (Mini App)",
                    web_app=WebAppInfo(url=reader_url),
                )
            ],
        )
        summary = self._summary(displayed_text)
        body = (
            f'{summary}\n\n<i>(...текст превышает лимит. Продолжение доступно '
            f'по кнопке <b>«Развернуть статью»</b> 👇)</i> '
            f'<a href="{reader_url}">&#8203;</a>'
        )
        try:
            ref, _ = await self._replace_or_send(
                body,
                parse_mode="HTML",
                reply_markup=markup,
            )
        except TelegramDeliveryError as exc:
            logging.warning("Reader presentation failed for uid=%s: %s", uid, exc)
            return None

        async def _cold_storage() -> None:
            try:
                url = await self._create_telegraph_page(title, displayed_text)
                if not url:
                    return
                for attempt in range(3):
                    if await self._store_telegraph_url(uid, url):
                        return
                    if attempt < 2:
                        await asyncio.sleep(2**attempt)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.warning("Reader cold-storage task failed uid=%s: %s", uid, exc)

        if self._telegraph_publication_enabled:
            self._submit_background(_cold_storage())
        return DeliveryReceipt(
            kind=DeliveryKind.READER,
            message_ids=(ref.message_id,),
            final_message=ref,
            publication_url=reader_url,
        )

    async def _telegraph(
        self,
        *,
        displayed_text: str,
        actions: InlineKeyboardMarkup | None,
        title: str,
    ) -> DeliveryReceipt | None:
        if not self._telegraph_publication_enabled:
            return None
        try:
            url = await self._create_telegraph_page(title, displayed_text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.warning("Telegraph creation failed: %s", exc)
            return None
        if not url:
            return None

        markup = _prepend_row(
            actions,
            [InlineKeyboardButton("📖 Открыть статью", url=url)],
        )
        body = f'{self._summary(displayed_text)}\n\n📖 <a href="{url}">Читать статью (Instant View)</a>'
        try:
            ref, _ = await self._replace_or_send(
                body,
                parse_mode="HTML",
                reply_markup=markup,
            )
        except TelegramDeliveryError as exc:
            logging.warning("Telegraph presentation failed: %s", exc)
            return None
        return DeliveryReceipt(
            kind=DeliveryKind.TELEGRAPH,
            message_ids=(ref.message_id,),
            final_message=ref,
            publication_url=url,
        )

    async def _finalize(
        self,
        *,
        displayed_text: str,
        title: str,
        actions: InlineKeyboardMarkup | None,
    ) -> DeliveryReceipt:
        formatted = _formatted_html(displayed_text)
        if len(formatted) <= self._message_limit:
            ref, _ = await self._replace_or_send(
                formatted,
                parse_mode="HTML",
                reply_markup=actions,
            )
            return DeliveryReceipt(
                kind=DeliveryKind.MESSAGE,
                message_ids=(ref.message_id,),
                final_message=ref,
            )

        if self._webapp_base_url and not self._private_content:
            reader = await self._reader(
                uid=str(uuid.uuid4()),
                displayed_text=displayed_text,
                actions=actions,
                title=title,
            )
            if reader is not None:
                return reader

        if not self._private_content:
            telegraph = await self._telegraph(
                displayed_text=displayed_text,
                actions=actions,
                title=title,
            )
            if telegraph is not None:
                return telegraph
        return await self._split(displayed_text, actions)


class TelegramRenderSession:
    def __init__(self, renderer: TelegramRenderer):
        self._renderer = renderer
        self._state = "open"
        self._draft_text = ""
        self._pending_chars = 0
        self._last_edit = 0.0
        self._long_read_pending = False

    async def append(self, text: str) -> None:
        if self._state != "open":
            raise RendererProtocolError("Cannot append after renderer finalization")
        if not text:
            return
        self._draft_text += text
        self._pending_chars += len(text)
        if self._long_read_pending:
            return

        visible_draft = strip_hallucinated_tool_trace(self._draft_text)
        formatted = _formatted_html(visible_draft)
        if len(formatted) > self._renderer._message_limit:
            self._long_read_pending = True
            frozen = self._renderer._summary(visible_draft)
            frozen += "\n\n... 📝 <i>[Текст получается очень длинным, формирую статью]</i>"
            try:
                await self._renderer._edit_with_retry(
                    frozen,
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except Exception as exc:
                logging.debug("Could not freeze pending Long Read draft: %s", exc)
            return

        now = time.monotonic()
        if (
            self._pending_chars >= self._renderer._min_chunk_size
            and now - self._last_edit >= self._renderer._debounce_seconds
        ):
            try:
                await self._renderer._edit_with_retry(
                    formatted + " ▌",
                    parse_mode="HTML",
                    reply_markup=None,
                )
                self._last_edit = now
                self._pending_chars = 0
            except Exception as exc:
                logging.debug("Progressive Telegram edit failed: %s", exc)

    async def show_status(
        self,
        text: str,
        actions: InlineKeyboardMarkup | None = None,
    ) -> None:
        if self._state != "open":
            raise RendererProtocolError("Cannot show status after renderer finalization")
        try:
            await self._renderer._edit_with_retry(
                text,
                parse_mode=None,
                reply_markup=actions,
            )
        except Exception as exc:
            logging.debug("Could not show delayed Telegram status: %s", exc)

    async def finalize(
        self,
        *,
        displayed_text: str,
        title: str,
        actions: InlineKeyboardMarkup | None,
    ) -> DeliveryReceipt:
        if self._state != "open":
            raise RendererProtocolError("Renderer session can only be finalized once")
        self._state = "finalized"
        if not displayed_text.strip():
            raise TelegramDeliveryError("Cannot finalize an empty Telegram response")
        return await self._renderer._finalize(
            displayed_text=displayed_text,
            title=title,
            actions=actions,
        )


__all__ = [
    "DeliveryKind",
    "DeliveryReceipt",
    "RendererProtocolError",
    "TelegramDeliveryError",
    "TelegramBotTransport",
    "TelegramMessageRef",
    "TelegramMessageTransport",
    "TelegramRenderSession",
    "TelegramRenderer",
    "TelegramTransport",
]
