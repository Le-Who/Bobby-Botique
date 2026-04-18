import abc


class StreamingUIAdapter(abc.ABC):
    """Protocol for dynamic UI updates (e.g., Telegram messages) during streaming."""

    @abc.abstractmethod
    async def edit_message(self, text: str, parse_mode: str | None, reply_markup: object | None = None) -> None:
        """Edit the current message with new text."""

    @abc.abstractmethod
    async def reply_new_message(self, text: str, parse_mode: str | None) -> "StreamingUIAdapter":
        """Start a new message when the current one overflows, returning a new adapter."""

    @property
    @abc.abstractmethod
    def last_message(self) -> object:
        """Get the underlying message object for final actions (like adding buttons)."""


class TelegramMessageAdapter(StreamingUIAdapter):
    """Adapter for python-telegram-bot Message and Bot objects."""

    def __init__(self, message, bot=None, chat_id: int = 0, draft_id: int = 0):
        self._msg = message
        self._bot = bot
        self._chat_id = chat_id
        self._draft_id = draft_id

    async def edit_message(self, text: str, parse_mode: str | None, reply_markup: object | None = None) -> None:
        from telegram import LinkPreviewOptions
        from telegram.error import TelegramError

        try:
            kwargs: dict = {"parse_mode": parse_mode}
            if reply_markup is not None:
                kwargs["reply_markup"] = reply_markup
            kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=True)
            await self._msg.edit_text(text, **kwargs)
        except TelegramError as e:
            if "not modified" not in str(e).lower():
                raise

    async def reply_new_message(self, text: str, parse_mode: str | None) -> "StreamingUIAdapter":
        from telegram import LinkPreviewOptions
        from telegram.error import TelegramError

        # BUG: if original message was deleted, reply_text raises "Message to be replied not found"
        try:
            new_msg = await self._msg.reply_text(
                text,
                parse_mode=parse_mode,
                allow_sending_without_reply=True,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramError as e:
            if "not found" in str(e).lower() and "message to be replied" in str(e).lower():
                # Fallback: original message deleted, just send a new message to the chat
                bot = self._bot or self._msg.get_bot()

                reply_to_message_id = None
                if getattr(self._msg, "reply_to_message", None):
                    reply_to_message_id = self._msg.reply_to_message.message_id

                new_msg = await bot.send_message(
                    chat_id=self._chat_id or getattr(self._msg, "chat_id", None),
                    text=text,
                    parse_mode=parse_mode,
                    reply_to_message_id=reply_to_message_id,
                    allow_sending_without_reply=True,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            else:
                raise

        return TelegramMessageAdapter(
            message=new_msg,
            bot=self._bot,
            chat_id=self._chat_id,
            draft_id=self._draft_id,
        )

    @property
    def last_message(self):
        return self._msg
