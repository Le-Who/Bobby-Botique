import abc


class StreamingUIAdapter(abc.ABC):
    """Protocol for dynamic UI updates (e.g., Telegram messages) during streaming."""

    @abc.abstractmethod
    async def edit_message(self, text: str, parse_mode: str) -> None:
        """Edit the current message with new text."""

    @abc.abstractmethod
    async def send_draft(self, text: str, parse_mode: str) -> None:
        """Send a lightweight draft update (if supported by UI)."""

    @abc.abstractmethod
    async def reply_new_message(self, text: str, parse_mode: str) -> "StreamingUIAdapter":
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

    async def edit_message(self, text: str, parse_mode: str) -> None:
        from telegram.error import TelegramError
        try:
            await self._msg.edit_text(text, parse_mode=parse_mode)
        except TelegramError as e:
            if "not modified" not in str(e).lower():
                raise

    async def send_draft(self, text: str, parse_mode: str) -> None:
        if not self._bot:
            raise ValueError("Draft mode requires a bot instance")
        from telegram.error import TelegramError
        try:
            await self._bot.send_message_draft(
                chat_id=self._chat_id,
                draft_id=self._draft_id,
                text=text,
                parse_mode=parse_mode,
            )
        except TelegramError as e:
            if "not modified" not in str(e).lower():
                raise

    async def reply_new_message(self, text: str, parse_mode: str) -> "StreamingUIAdapter":
        new_msg = await self._msg.reply_text(text, parse_mode=parse_mode)
        return TelegramMessageAdapter(
            message=new_msg,
            bot=self._bot,
            chat_id=self._chat_id,
            draft_id=self._draft_id
        )

    @property
    def last_message(self):
        return self._msg
