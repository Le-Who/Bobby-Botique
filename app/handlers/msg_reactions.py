# /app/handlers/msg_reactions.py
"""Native Telegram reaction handler — ambient feedback via organic reactions.

Architecture:
    **Primary feedback channel**: Inline keyboard buttons (👍/👎) handled
    by ``cb_feedback.feedback_callback``. Those buttons are appended to every
    AI response message.

    **Secondary/fallback channel** (this module): Users can also react to bot
    messages with native Telegram reactions (long-press → reaction picker).
    This handler captures those organic reactions.

    When a user reacts:
      👍 / ❤️ / 🔥 / 👏  → record positive + bot responds with ❤️ reaction
      👎 / 💩 / 🤮        → record negative + store LTM correction signal

    **Safety filter**: The bot may still set single reactions (e.g. ❤️ on
    upvote). We filter ``actor.id == bot.id`` to avoid counting those as
    user feedback.

    Reactions arrive via ``message_reaction`` updates (UpdateType.MESSAGE_REACTION).
    We handle ONLY ``update.message_reaction`` (individual reactions), not
    ``message_reaction_count`` (anonymous aggregate counts in channels).

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

    **Bot-self-reaction filter**: The bot pre-places 👍/👎 on its own
    messages.  When IT does so, Telegram delivers a ``message_reaction``
    update with ``actor == bot``.  We MUST skip those to avoid counting
    the bot's own reactions as user feedback.
    """
    reaction_update = update.message_reaction
    if not reaction_update:
        return

    # Only care about reactions where the actor is known
    actor = reaction_update.actor_chat or reaction_update.user
    if not actor:
        return

    user_id: int = actor.id

    # ── Filter out bot's own reactions ─────────────────────────────────────
    # The bot may set single reactions (e.g. ❤️ on upvote via cb_feedback).
    # Telegram delivers those as message_reaction updates with actor=bot.
    # We must not count those as user feedback.
    bot_id = context.bot.id
    if user_id == bot_id:
        return

    message_id: int = reaction_update.message_id
    chat_id: int = reaction_update.chat.id

    new_reactions = list(reaction_update.new_reaction or [])
    old_reactions = list(reaction_update.old_reaction or [])

    # Collect emojis that were *added* in this update
    new_emojis = {r.emoji for r in new_reactions if hasattr(r, "emoji")}
    old_emojis = {r.emoji for r in old_reactions if hasattr(r, "emoji")}
    added_emojis = new_emojis - old_emojis

    if not added_emojis:
        return

    # Pick the first classifiable emoji
    rating: str | None = None
    for emoji in added_emojis:
        rating = _classify_reaction(emoji)
        if rating:
            break

    if rating is None:
        return

    logging.info(
        "Reaction feedback: user=%s message=%s rating=%s emoji=%s",
        user_id,
        message_id,
        rating,
        added_emojis,
    )

    # ── Save feedback to DB ───────────────────────────────────────────────
    try:
        await save_feedback(user_id, message_id, rating)
    except Exception as e:
        logging.warning("Reaction feedback save failed for user %s: %s", user_id, e)

    # ── Post-feedback actions (fire-and-forget) ───────────────────────────
    try:
        if rating == "up":
            # Respond with ❤️ on the user's reaction as acknowledgement
            from app.utils.ux_improvements import set_message_reaction

            await set_message_reaction(context.bot, chat_id, message_id, "❤️")

        elif rating == "down":
            from app.utils.background_tasks import submit_task

            # ── RLHF: penalize graph edges used for this response ─────────
            async def _penalize_edges():
                try:
                    from app.repos.memory import (
                        get_response_retrieved_edge_ids,
                        penalize_graph_edges,
                    )

                    edge_ids = get_response_retrieved_edge_ids(user_id, message_id)
                    count = await penalize_graph_edges(user_id, edge_ids=edge_ids, penalty=0.10)
                    if count > 0:
                        logging.info(
                            "RLHF: user %d downvote penalized %d graph edges",
                            user_id,
                            count,
                        )
                except Exception as pen_err:
                    logging.debug("RLHF edge penalization skipped: %s", pen_err)

            submit_task(_penalize_edges())
    except Exception as action_err:
        logging.debug("Post-feedback action failed (non-critical): %s", action_err)


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
