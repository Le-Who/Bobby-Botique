# /app/state.py
"""
User state management with database persistence.

State is kept in-memory for fast access, but persisted to PostgreSQL
on every mutation so it survives restarts/redeployments.
"""

import asyncio
import logging


class UserState:
    """In-process user state backed by database persistence.

    Runtime-only fields (not persisted):
      - lock: asyncio.Lock
      - last_document_message_id: Telegram message ID
      - _loaded_from_db: whether DB state has been loaded
      - _dirty: whether state needs to be saved
    """

    __slots__ = (
        "lock",
        # Persisted fields
        "document_mode",
        "selected_document_id",
        "last_document_message_id",
        "awaiting_custom_role_input",
        "generated_role",
        "last_custom_role_prompt",
        "generating_custom_role",
        "last_sent_message_text",
        # Manual role creation fields
        "awaiting_manual_role_title",
        "awaiting_manual_role_prompt",
        "manual_role_title",
        "manual_role_prompt",
        # Internal bookkeeping
        "_loaded_from_db",
        "_dirty",
        "_user_id",
    )

    def __init__(self, user_id: int = 0):
        self.lock = asyncio.Lock()
        self._user_id = user_id
        # Persisted fields
        self.document_mode = False
        self.selected_document_id: int | None = None
        self.last_document_message_id: int | None = None
        self.awaiting_custom_role_input: bool = False
        self.generated_role: dict | None = None
        self.last_custom_role_prompt: str | None = None
        self.generating_custom_role: bool = False
        self.last_sent_message_text: str | None = None
        # Manual role creation
        self.awaiting_manual_role_title: bool = False
        self.awaiting_manual_role_prompt: bool = False
        self.manual_role_title: str = ""
        self.manual_role_prompt: str = ""
        # Internal
        self._loaded_from_db: bool = False
        self._dirty: bool = False


# =============================================================================
# STATE STORE — In-memory dictionary with DB backing
# =============================================================================


class _UserStateStore:
    """User state store with LRU eviction and lazy DB loading.

    Uses LRUCache to cap in-memory state at ``maxsize`` users.
    Evicted entries are silently re-loaded from DB on next access
    via ``_ensure_loaded``, so no data is lost.
    """

    def __init__(self, maxsize: int = 10_000):
        from cachetools import LRUCache

        self._states: LRUCache = LRUCache(maxsize=maxsize)

    def __getitem__(self, user_id: int) -> UserState:
        try:
            return self._states[user_id]
        except KeyError:
            state = UserState(user_id)
            self._states[user_id] = state
            return state

    def __contains__(self, user_id: int) -> bool:
        return user_id in self._states


USER_STATES = _UserStateStore()


# =============================================================================
# DB PERSISTENCE — load/save helpers
# =============================================================================


async def _ensure_loaded(state: UserState) -> UserState:
    """Load persisted state from DB on first access (lazy)."""
    if state._loaded_from_db or state._user_id == 0:
        return state

    async with state.lock:
        # Double-check after acquiring lock to avoid redundant loads
        if state._loaded_from_db:
            return state  # type: ignore[unreachable]  # double-check lock pattern

        try:
            from app.repos.users import load_user_state

            data = await load_user_state(state._user_id)
            if data:
                state.document_mode = data.get("document_mode", False)
                state.selected_document_id = data.get("selected_document_id")
                state.awaiting_custom_role_input = data.get("awaiting_custom_role_input", False)
                state.generated_role = data.get("generated_role")
                state.last_custom_role_prompt = data.get("last_custom_role_prompt")
                state.generating_custom_role = data.get("generating_custom_role", False)
                state.last_sent_message_text = data.get("last_sent_message_text")
                state.awaiting_manual_role_title = data.get("awaiting_manual_role_title", False)
                state.awaiting_manual_role_prompt = data.get("awaiting_manual_role_prompt", False)
                state.manual_role_title = data.get("manual_role_title", "")
                state.manual_role_prompt = data.get("manual_role_prompt", "")
        except Exception as e:
            logging.warning("Could not load state for %s: %s", state._user_id, e)

        state._loaded_from_db = True
    return state


async def _persist(state: UserState) -> None:
    """Save state to DB. Fire-and-forget style (errors are logged, not raised)."""
    if state._user_id == 0:
        return

    try:
        from app.repos.users import save_user_state

        await save_user_state(
            user_id=state._user_id,
            document_mode=state.document_mode,
            selected_document_id=state.selected_document_id,
            awaiting_custom_role_input=state.awaiting_custom_role_input,
            generated_role=state.generated_role,
            last_custom_role_prompt=state.last_custom_role_prompt,
            generating_custom_role=state.generating_custom_role,
            last_sent_message_text=state.last_sent_message_text,
            awaiting_manual_role_title=state.awaiting_manual_role_title,
            awaiting_manual_role_prompt=state.awaiting_manual_role_prompt,
            manual_role_title=state.manual_role_title,
            manual_role_prompt=state.manual_role_prompt,
        )
    except Exception as e:
        logging.warning("Could not persist state for %s: %s", state._user_id, e)


def _schedule_persist(state: UserState) -> None:
    """Schedule a non-blocking persistence task on the running event loop."""

    def _on_done(task):
        exc = task.exception() if not task.cancelled() else None
        if exc:
            logging.warning("Persist task failed for user %s: %s", state._user_id, exc)

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_persist(state))
        task.add_done_callback(_on_done)
    except RuntimeError:
        # No running event loop (e.g. during tests) — skip persistence
        pass


# =============================================================================
# PUBLIC API — Drop-in compatible with the old state.py interface
# =============================================================================


def get_user_state(user_id: int) -> UserState:
    """Get the in-memory state for a user."""
    return USER_STATES[user_id]


def get_user_lock(user_id: int) -> asyncio.Lock:
    """Get the lock for a user."""
    return get_user_state(user_id).lock


# --- Async load API (call once per handler, early) ---


async def ensure_state_loaded(user_id: int) -> UserState:
    """Ensure the user's persisted state is loaded from DB.

    Call this at the start of message/callback handlers to hydrate
    persisted fields lazily. Fast no-op if already loaded.
    """
    state = get_user_state(user_id)
    return await _ensure_loaded(state)


# --- Document mode ---


def set_document_mode(user_id: int, enabled: bool, document_id: int | None = None):
    """Set document mode for a user and persist to DB."""
    state = get_user_state(user_id)
    state.document_mode = enabled
    state.selected_document_id = document_id if enabled else None
    _schedule_persist(state)


def clear_document_state(user_id: int):
    """Clear document state and persist."""
    state = get_user_state(user_id)
    state.document_mode = False
    state.selected_document_id = None
    state.last_document_message_id = None
    _schedule_persist(state)


# --- Custom role creation ---


def begin_custom_role_creation(user_id: int):
    state = get_user_state(user_id)
    state.awaiting_custom_role_input = True
    state.generated_role = None
    state.last_custom_role_prompt = None
    state.generating_custom_role = False
    _schedule_persist(state)


def set_generated_role(user_id: int, role: dict):
    state = get_user_state(user_id)
    state.generated_role = role
    state.awaiting_custom_role_input = False
    _schedule_persist(state)


def clear_custom_role_state(user_id: int):
    state = get_user_state(user_id)
    state.awaiting_custom_role_input = False
    state.generated_role = None
    state.last_custom_role_prompt = None
    state.generating_custom_role = False
    _schedule_persist(state)


def set_last_custom_role_prompt(user_id: int, prompt: str):
    state = get_user_state(user_id)
    state.last_custom_role_prompt = prompt
    _schedule_persist(state)


def get_last_custom_role_prompt(user_id: int) -> str | None:
    return get_user_state(user_id).last_custom_role_prompt


def set_generating_custom_role(user_id: int, value: bool):
    state = get_user_state(user_id)
    state.generating_custom_role = value
    _schedule_persist(state)


def is_awaiting_custom_role_input(user_id: int) -> bool:
    return get_user_state(user_id).awaiting_custom_role_input


def get_generated_role(user_id: int) -> dict | None:
    return get_user_state(user_id).generated_role


# --- Document mode queries ---


def is_in_document_mode(user_id: int) -> bool:
    """Check if user is in document mode."""
    return get_user_state(user_id).document_mode


def get_selected_document_id(user_id: int) -> int | None:
    """Get the selected document ID."""
    return get_user_state(user_id).selected_document_id


# --- Last message (for retry button) ---


def set_last_sent_message(user_id: int, text: str):
    """Store the last sent message for retry functionality."""
    state = get_user_state(user_id)
    state.last_sent_message_text = text
    _schedule_persist(state)


def get_last_sent_message(user_id: int) -> str | None:
    return get_user_state(user_id).last_sent_message_text


# --- Manual role creation ---


def begin_manual_role_creation(user_id: int):
    """Start manual role creation — awaiting title input."""
    state = get_user_state(user_id)
    state.awaiting_manual_role_title = True
    state.awaiting_manual_role_prompt = False
    state.manual_role_title = ""
    state.manual_role_prompt = ""
    _schedule_persist(state)


def set_manual_role_title(user_id: int, title: str):
    """Set the manual role title and transition to prompt input."""
    state = get_user_state(user_id)
    state.manual_role_title = title
    state.awaiting_manual_role_title = False
    state.awaiting_manual_role_prompt = True
    _schedule_persist(state)


def set_manual_role_prompt(user_id: int, prompt: str):
    """Store the manual role prompt text."""
    state = get_user_state(user_id)
    state.manual_role_prompt = prompt
    _schedule_persist(state)


def finish_manual_role_input(user_id: int):
    """Mark manual role input as complete (title+prompt collected).

    Flips awaiting flags to False but KEEPS title and prompt
    so the save callback can read them.
    """
    state = get_user_state(user_id)
    state.awaiting_manual_role_title = False
    state.awaiting_manual_role_prompt = False
    _schedule_persist(state)


def clear_manual_role_state(user_id: int):
    """Clear ALL manual role creation state (call after save/cancel)."""
    state = get_user_state(user_id)
    state.awaiting_manual_role_title = False
    state.awaiting_manual_role_prompt = False
    state.manual_role_title = ""
    state.manual_role_prompt = ""
    _schedule_persist(state)


def is_awaiting_manual_role_title(user_id: int) -> bool:
    return get_user_state(user_id).awaiting_manual_role_title


def is_awaiting_manual_role_prompt(user_id: int) -> bool:
    return get_user_state(user_id).awaiting_manual_role_prompt


def get_manual_role_title(user_id: int) -> str:
    return get_user_state(user_id).manual_role_title


def get_manual_role_prompt(user_id: int) -> str:
    return get_user_state(user_id).manual_role_prompt
