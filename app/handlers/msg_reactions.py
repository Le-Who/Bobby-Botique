# /app/handlers/msg_reactions.py
"""Native Telegram reaction handler — ambient feedback via pre-placed reactions.

Architecture:
    The bot pre-places 👍/👎 reactions on its OWN messages (via
    ``set_feedback_reactions`` in ux_improvements.py). Users simply tap
    one of the pre-placed reactions to give feedback — zero friction.

    When a user reacts:
      👍 / ❤️ / 🔥 / 👏  → record positive + bot responds with ❤️ reaction
      👎 / 💩 / 🤮        → record negative + store LTM correction signal

    **Critical filter**: The bot's own pre-placed reactions are ignored
    by checking ``actor.id != bot.id``. Without this, the bot would count
    its own seeds as user feedback.

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

    # ── CRITICAL: filter out bot's own pre-placed reactions ────────────────
    # The bot places 👍/👎 on its messages via set_feedback_reactions().
    # Telegram delivers that as a message_reaction update with actor=bot.
    # We must not count those as user feedback.
    bot_id = context.bot.id
    if user_id == bot_id:
        return

    message_id: int = reaction_update.message_id
    chat_id: int = reaction_update.chat.id

    new_reactions: list = reaction_update.new_reaction or []
    old_reactions: list = reaction_update.old_reaction or []

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
            # Store negative signal in LTM so the bot learns from dislikes.
            # We record "user disliked response to message_id" as a memory
            # that affects future interactions.
            from app.utils.background_tasks import submit_task

            async def _store_negative_signal():
                try:
                    from app.repos.keys import get_available_gemini_key
                    from app.repos.memory import EMBEDDING_MODEL, store_memory

                    key_data = await get_available_gemini_key(model_name=EMBEDDING_MODEL)
                    if not key_data:
                        return
                    await store_memory(
                        user_id,
                        f"[FEEDBACK] Пользователю не понравился ответ (msg_id={message_id}). "
                        "Учитывай это при формировании будущих ответов.",
                        key_data["api_key"],
                        source_type="negative_feedback",
                    )
                except Exception as mem_err:
                    logging.debug("LTM negative signal failed: %s", mem_err)

            submit_task(_store_negative_signal())

            # ── RLHF: penalize graph edges used for this response ─────────
            async def _penalize_edges():
                try:
                    from app.repos.memory import penalize_graph_edges

                    count = await penalize_graph_edges(user_id, penalty=0.10)
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
