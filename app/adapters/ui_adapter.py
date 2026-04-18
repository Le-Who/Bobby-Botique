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
        from telegram.error import TelegramError

        try:
            kwargs: dict = {"parse_mode": parse_mode}
            if reply_markup is not None:
                kwargs["reply_markup"] = reply_markup
            await self._msg.edit_text(text, **kwargs)
        except TelegramError as e:
            err_msg = str(e).lower()
            if "not modified" in err_msg:
                return

            if "not found" in err_msg or "deleted" in err_msg:
                # Fallback: original message deleted, send a new one
                bot = self._bot or self._msg.get_bot()
                chat_id = self._chat_id or self._msg.chat_id

                # Try to preserve reply context if the original was a reply
                reply_to_id = None
                if hasattr(self._msg, "reply_to_message") and self._msg.reply_to_message:
                    reply_to_id = self._msg.reply_to_message.message_id

                new_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_id,
                    allow_sending_without_reply=True,
                )
                # Update internal reference so subsequent edits work on the new message
                self._msg = new_msg
            else:
                raise

    async def reply_new_message(self, text: str, parse_mode: str | None) -> "StreamingUIAdapter":
        from telegram.error import TelegramError

        try:
            new_msg = await self._msg.reply_text(
                text,
                parse_mode=parse_mode,
                allow_sending_without_reply=True,
            )
        except TelegramError as e:
            err_msg = str(e).lower()
            if "not found" in err_msg and "message to be replied" in err_msg:
                # Fallback: original message deleted, just send a new message to the chat
                bot = self._bot or self._msg.get_bot()

                # Try to preserve reply context
                reply_to_id = None
                if hasattr(self._msg, "reply_to_message") and self._msg.reply_to_message:
                    reply_to_id = self._msg.reply_to_message.message_id

                new_msg = await bot.send_message(
                    chat_id=self._chat_id or self._msg.chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_to_message_id=reply_to_id,
                    allow_sending_without_reply=True,
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
