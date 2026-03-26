"""Request deduplication middleware.

Prevents duplicate AI requests from the same user within a short window,
guarding against Telegram double-tap and network retries.

Usage in handler decorators:
    from app.middleware.dedup import is_duplicate_request

    if await is_duplicate_request(user_id, message_text):
        await update.message.reply_text("⏳ Запрос уже обрабатывается…")
        return
"""

import hashlib
import logging
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

# {user_id: {request_hash: timestamp}}
_recent_requests: dict[int, dict[str, float]] = defaultdict(dict)

# Dedup window in seconds — requests with the same hash within this window are duplicates
DEDUP_WINDOW_SECONDS: float = 3.0

# Max tracked hashes per user (prevent unbounded growth)
_MAX_TRACKED_PER_USER: int = 20


def _hash_request(text: str) -> str:
    """Create a short hash of the request text for comparison."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


def _cleanup_stale(user_id: int) -> None:
    """Remove expired entries for a user."""
    now = time.monotonic()
    user_hashes = _recent_requests.get(user_id)
    if not user_hashes:
        return
    expired = [h for h, ts in user_hashes.items() if now - ts > DEDUP_WINDOW_SECONDS * 2]
    for h in expired:
        del user_hashes[h]


async def is_duplicate_request(user_id: int, message_text: str) -> bool:
    """Check if this request is a duplicate (same text within DEDUP_WINDOW).

    Returns True if the request should be skipped (duplicate detected).
    """
    if not message_text or not message_text.strip():
        return False

    request_hash = _hash_request(message_text.strip())
    now = time.monotonic()

    # Cleanup old entries periodically
    _cleanup_stale(user_id)

    user_hashes = _recent_requests[user_id]

    # Check for duplicate
    last_seen = user_hashes.get(request_hash)
    if last_seen is not None and (now - last_seen) < DEDUP_WINDOW_SECONDS:
        logger.info("Dedup: blocked duplicate request from user %s (hash=%s)", user_id, request_hash)
        return True

    # Record this request
    user_hashes[request_hash] = now

    # Evict oldest if too many tracked
    if len(user_hashes) > _MAX_TRACKED_PER_USER:
        oldest_hash = min(user_hashes, key=user_hashes.get)  # type: ignore[arg-type]
        del user_hashes[oldest_hash]

    return False


def clear_user_dedup(user_id: int) -> None:
    """Clear dedup state for a user (e.g., after /newchat)."""
    _recent_requests.pop(user_id, None)
