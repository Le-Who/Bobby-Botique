# /app/handlers/msg_reactions.py
"""Native Telegram reaction handler — replaces inline 👍/👎 buttons with ambient feedback.

When a user reacts to a bot message:
  👍 / ❤️ / 🔥 / 👏  → record positive feedback (no UI noise)
  👎 / 💩 / 🤮        → record negative feedback + silently update metrics

Design decision:
    Reactions arrive via ``message_reaction`` updates (UpdateType.MESSAGE_REACTION).
    We handle ONLY ``update.message_reaction`` (individual reactions), not
    ``message_reaction_count`` (anonymous aggregate counts in channels).

    We do NOT pop up any confirmations or toasts — reactions are ambient.
    The feedback is silently written to the same ``save_feedback`` table as
    the old inline button system, so reporting is unchanged.

    This handler is registered with ``MessageReactionHandler`` (PTB 20.8+)
    which provides ``message_reaction_types=MESSAGE_REACTION_UPDATED`` to
    receive only per-user reactions (not anonymous counts).
"""

__all__ = ["handle_message_reaction", "register"]

import logging

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageReactionHandler

from app.repos.users import save_feedback

# Emoji sets for sentiment classification
_POSITIVE_EMOJI = frozenset({"👍", "❤️", "🔥", "🥰", "👏", "🎉", "🤩", "💯", "⚡", "🏆", "🫡", "✅"})
_NEGATIVE_EMOJI = frozenset({"👎", "💩", "🤮", "🤬", "😤", "😡", "🖕"})


def _classify_reaction(emoji: str) -> str | None:
    """Return 'up', 'down', or None (neutral/unknown reactions we ignore)."""
    if emoji in _POSITIVE_EMOJI:
        return "up"
    if emoji in _NEGATIVE_EMOJI:
        return "down"
    return None


async def handle_message_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process a user reaction to a bot message.

    PTB delivers this via update.message_reaction (MessageReactionUpdated).
    We check new_reaction to see what was added, and old_reaction to detect removals.
    Only bot messages are processed (we check actor vs bot identity).
    """
    reaction_update = update.message_reaction
    if not reaction_update:
        return

    # Only care about reactions to messages in private/group chats where user is known
    actor = reaction_update.actor_chat or reaction_update.user
    if not actor:
        return

    user_id: int = actor.id
    message_id: int = reaction_update.message_id

    new_reactions: list = reaction_update.new_reaction or []
    old_reactions: list = reaction_update.old_reaction or []

    # Collect emojis that were *added* in this update
    new_emojis = {r.emoji for r in new_reactions if hasattr(r, "emoji")}
    old_emojis = {r.emoji for r in old_reactions if hasattr(r, "emoji")}
    added_emojis = new_emojis - old_emojis

    if not added_emojis:
        # User removed a reaction or changed to a neutral one we don't track
        return

    # Pick the first classifiable emoji
    rating: str | None = None
    for emoji in added_emojis:
        rating = _classify_reaction(emoji)
        if rating:
            break

    if rating is None:
        # Neutral/unknown emoji — we don't record it
        return

    logging.info(
        "Reaction feedback: user=%s message=%s rating=%s emoji=%s",
        user_id,
        message_id,
        rating,
        added_emojis,
    )

    try:
        await save_feedback(user_id, message_id, rating)
    except Exception as e:
        logging.warning("Reaction feedback save failed for user %s: %s", user_id, e)


def register(application: Application) -> None:
    """Register the reaction handler on the application.

    Uses MESSAGE_REACTION_UPDATED to receive per-user reactions only
    (excludes anonymous channel reaction counts which have no user_id).
    block=False: reactions are fire-and-forget, no need to block update processing.
    """
    application.add_handler(
        MessageReactionHandler(
            handle_message_reaction,
            message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_UPDATED,
            block=False,
        )
    )
