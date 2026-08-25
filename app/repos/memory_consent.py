"""Durable consent generations and provider-use leases for private data.

The generation lives on ``chats.memory_epoch`` and is allocated from a global
PostgreSQL sequence (migration 069), so deleting and recreating an account can
never make an old background snapshot current again.  Provider leases bridge
the deliberate gap between short database transactions and slow network calls:
consent revocation invalidates the generation, then waits for leases from older
generations before acknowledging the operation.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from app.database import (
    clear_user_context,
    db_manager,
    db_query,
    reconnect_database,
    set_user_context,
)

_LEASE_TTL_SECONDS = 120
_LEASE_HEARTBEAT_SECONDS = 30
_LEASE_WAIT_POLL_SECONDS = 0.1
_LEASE_MAX_LIFETIME_SECONDS = 15 * 60


def capture_epoch(chat_state: Any) -> int | None:
    """Return the durable write token when automatic LTM capture is allowed."""
    if (
        chat_state is None
        or bool(getattr(chat_state, "private_data_blocked", False))
        or not bool(getattr(chat_state, "ltm_enabled", False))
    ):
        return None
    return int(getattr(chat_state, "memory_epoch", 0) or 0)


async def _ensure_pool() -> bool:
    if not db_manager.is_connected:
        try:
            await reconnect_database()
        except Exception as exc:
            logging.warning("Private-data lease database reconnect failed: %s", exc)
            return False
    return db_manager.pool is not None


async def is_private_data_snapshot_current(
    user_id: int,
    expected_epoch: int | None,
    *,
    require_ltm: bool,
) -> bool:
    """Return whether an account/chat generation still authorizes the snapshot.

    Missing chat state and missing generation tokens fail closed.  This helper
    is useful for cheap revalidation; external provider calls must use
    :func:`private_data_lease` so revocation also waits for already-started use.
    """
    if expected_epoch is None or not await _ensure_pool():
        return False

    try:
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            try:
                rows = await db_query(
                    """
                    SELECT chat.memory_epoch
                    FROM public.chats AS chat
                    JOIN public.users AS account ON account.user_id = chat.user_id
                    WHERE chat.user_id = $1
                      AND chat.memory_epoch = $2
                      AND chat.private_data_blocked IS FALSE
                      AND ($3::boolean IS FALSE OR chat.ltm_enabled IS TRUE)
                    """,
                    (user_id, expected_epoch, require_ltm),
                    conn=conn,
                )
                return bool(rows)
            finally:
                await clear_user_context(conn=conn)
    except Exception as exc:
        logging.warning("Private-data snapshot check failed closed for user %d: %s", user_id, exc)
        return False


async def resolve_current_epoch(user_id: int, *, require_ltm: bool) -> int | None:
    """Resolve a live generation for an immediate synchronous operation."""
    if not await _ensure_pool():
        return None
    try:
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            try:
                rows = await db_query(
                    """
                    SELECT chat.memory_epoch
                    FROM public.chats AS chat
                    JOIN public.users AS account ON account.user_id = chat.user_id
                    WHERE chat.user_id = $1
                      AND chat.private_data_blocked IS FALSE
                      AND ($2::boolean IS FALSE OR chat.ltm_enabled IS TRUE)
                    """,
                    (user_id, require_ltm),
                    conn=conn,
                )
                if not rows:
                    return None
                return int(rows[0]["memory_epoch"])
            finally:
                await clear_user_context(conn=conn)
    except Exception as exc:
        logging.warning("Private-data generation resolution failed closed for user %d: %s", user_id, exc)
        return None


async def _acquire_private_data_lease(
    user_id: int,
    expected_epoch: int,
    purpose: str,
    require_ltm: bool,
    lease_id: UUID,
) -> bool:
    if not await _ensure_pool():
        return False

    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(user_id, False, conn=conn)
        try:
            # The same lock is taken by disable, clear-memory, and account erase.
            # Therefore either this lease commits first and is waited on, or the
            # barrier commits first and this INSERT observes the new generation.
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            rows = await db_query(
                """
                INSERT INTO public.private_data_leases (
                    lease_id, user_id, memory_epoch, purpose, expires_at
                )
                SELECT $2, chat.user_id, chat.memory_epoch, $3,
                       LEAST(
                           now() + make_interval(secs => $6),
                           now() + make_interval(secs => $7)
                       )
                FROM public.chats AS chat
                JOIN public.users AS account ON account.user_id = chat.user_id
                WHERE chat.user_id = $1
                  AND chat.memory_epoch = $4
                  AND chat.private_data_blocked IS FALSE
                  AND ($5::boolean IS FALSE OR chat.ltm_enabled IS TRUE)
                RETURNING lease_id
                """,
                (
                    user_id,
                    lease_id,
                    purpose,
                    expected_epoch,
                    require_ltm,
                    _LEASE_TTL_SECONDS,
                    _LEASE_MAX_LIFETIME_SECONDS,
                ),
                conn=conn,
            )
            return bool(rows)
        finally:
            await clear_user_context(conn=conn)


async def _renew_private_data_lease(user_id: int, lease_id: UUID) -> bool:
    if not await _ensure_pool():
        return False
    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(user_id, False, conn=conn)
        try:
            rows = await db_query(
                """
                UPDATE public.private_data_leases AS lease
                SET expires_at = LEAST(
                    lease.created_at + make_interval(secs => $4),
                    now() + make_interval(secs => $3)
                )
                FROM public.chats AS chat,
                     public.users AS account
                WHERE lease.user_id = $1
                  AND lease.lease_id = $2
                  AND lease.created_at + make_interval(secs => $4) > now()
                  AND chat.user_id = lease.user_id
                  AND account.user_id = chat.user_id
                  AND chat.memory_epoch = lease.memory_epoch
                  AND chat.private_data_blocked IS FALSE
                  AND (lease.purpose NOT LIKE 'ltm:%' OR chat.ltm_enabled IS TRUE)
                RETURNING lease.lease_id
                """,
                (
                    user_id,
                    lease_id,
                    _LEASE_TTL_SECONDS,
                    _LEASE_MAX_LIFETIME_SECONDS,
                ),
                conn=conn,
            )
            return bool(rows)
        finally:
            await clear_user_context(conn=conn)


async def _lease_heartbeat(
    user_id: int,
    lease_id: UUID,
    owner_task: asyncio.Task[Any],
) -> None:
    try:
        while True:
            await asyncio.sleep(_LEASE_HEARTBEAT_SECONDS)
            if not await _renew_private_data_lease(user_id, lease_id):
                logging.warning("Private-data lease %s could not be renewed", lease_id)
                if not owner_task.done():
                    owner_task.cancel()
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logging.warning("Private-data lease heartbeat failed for user %d: %s", user_id, exc)
        # Do not let the provider outlive a lease which can no longer be renewed.
        # The context-manager finally releases it; TTL remains the crash fallback.
        if not owner_task.done():
            owner_task.cancel()


async def _release_private_data_lease(user_id: int, lease_id: UUID) -> None:
    if not await _ensure_pool():
        return
    try:
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            try:
                await db_query(
                    "DELETE FROM public.private_data_leases WHERE user_id = $1 AND lease_id = $2",
                    (user_id, lease_id),
                    conn=conn,
                )
            finally:
                await clear_user_context(conn=conn)
    except Exception as exc:
        logging.warning("Private-data lease release failed for user %d: %s", user_id, exc)


@asynccontextmanager
async def private_data_lease(
    user_id: int,
    expected_epoch: int | None,
    *,
    purpose: str,
    require_ltm: bool,
) -> AsyncIterator[bool]:
    """Acquire a cross-process lease before sending a private snapshot outside.

    The yielded boolean is false when the account, chat, generation, or required
    LTM consent is no longer current.  Network calls run outside any database
    transaction.  A renewable row remains visible to revocation until ``finally``.
    """
    if expected_epoch is None or not purpose.strip():
        yield False
        return

    lease_id = uuid4()
    try:
        acquired = await _acquire_private_data_lease(
            user_id,
            int(expected_epoch),
            purpose,
            require_ltm,
            lease_id,
        )
    except Exception as exc:
        logging.warning("Private-data lease acquisition failed closed for user %d: %s", user_id, exc)
        acquired = False

    if not acquired:
        yield False
        return

    owner_task = asyncio.current_task()
    if owner_task is None:
        await _release_private_data_lease(user_id, lease_id)
        yield False
        return
    heartbeat = asyncio.create_task(_lease_heartbeat(user_id, lease_id, owner_task))
    try:
        yield True
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        await _release_private_data_lease(user_id, lease_id)


async def wait_for_private_data_leases(
    user_id: int,
    *,
    before_epoch: int,
    ltm_only: bool,
) -> None:
    """Wait until invalidated provider leases finish or expire.

    ``before_epoch`` is the newly allocated barrier.  Filtering on older
    generations prevents an erase request from waiting on a newly recreated
    account that happens to use the same Telegram user id.
    """
    if not await _ensure_pool():
        raise RuntimeError("database pool unavailable while draining private-data leases")

    while True:
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            try:
                await db_query(
                    """
                    DELETE FROM public.private_data_leases
                    WHERE user_id = $1 AND expires_at <= now()
                    """,
                    (user_id,),
                    conn=conn,
                )
                rows = await db_query(
                    """
                    SELECT count(*)::bigint AS active_count
                    FROM public.private_data_leases
                    WHERE user_id = $1
                      AND memory_epoch < $2
                      AND expires_at > now()
                      AND ($3::boolean IS FALSE OR purpose LIKE 'ltm:%')
                    """,
                    (user_id, before_epoch, ltm_only),
                    conn=conn,
                )
            finally:
                await clear_user_context(conn=conn)

        if not rows or int(rows[0]["active_count"] or 0) == 0:
            # Completed leases normally self-delete. This final bounded sweep
            # removes crash leftovers without touching a recreated account's
            # newer generation.
            async with db_manager.pool.acquire() as conn, conn.transaction():
                await set_user_context(user_id, False, conn=conn)
                try:
                    await db_query(
                        """
                        DELETE FROM public.private_data_leases
                        WHERE user_id = $1
                          AND memory_epoch < $2
                          AND ($3::boolean IS FALSE OR purpose LIKE 'ltm:%')
                        """,
                        (user_id, before_epoch, ltm_only),
                        conn=conn,
                    )
                finally:
                    await clear_user_context(conn=conn)
            return
        await asyncio.sleep(_LEASE_WAIT_POLL_SECONDS)


async def restore_private_data_barrier(
    user_id: int,
    *,
    barrier_epoch: int,
    ltm_enabled: bool,
) -> bool:
    """Compensate a failed two-phase privacy operation without stale reuse."""
    if not await _ensure_pool():
        return False
    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(user_id, False, conn=conn)
        try:
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            rows = await db_query(
                """
                UPDATE public.chats
                SET private_data_blocked = FALSE,
                    ltm_enabled = $3,
                    memory_epoch = nextval('memory_consent_epoch_seq')
                WHERE user_id = $1
                  AND memory_epoch = $2
                  AND private_data_blocked IS TRUE
                RETURNING memory_epoch
                """,
                (user_id, barrier_epoch, ltm_enabled),
                conn=conn,
            )
            return bool(rows)
        finally:
            await clear_user_context(conn=conn)


@asynccontextmanager
async def private_data_barrier(
    user_id: int,
    *,
    is_admin: bool,
    ltm_only: bool,
) -> AsyncIterator[tuple[int, bool]]:
    """Block new snapshots, drain invalidated leases, and compensate failures.

    The guard is armed *before* phase 1 starts.  Its local barrier value is set
    before the transaction context commits, so cancellation during commit or
    between phases still restores the exact generation.  A new privacy command
    may safely replace a gate left by a crashed process: generations are global
    and the older operation's verify/compensation is an exact-CAS no-op.
    """
    if not await _ensure_pool():
        raise RuntimeError("database pool unavailable while creating privacy barrier")

    barrier_epoch: int | None = None
    previous_ltm_enabled = True
    try:
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, is_admin, conn=conn)
            try:
                await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
                rows = await db_query(
                    """
                    WITH barrier AS (
                        SELECT nextval('memory_consent_epoch_seq')::bigint AS memory_epoch
                    ), invalidated AS (
                        INSERT INTO public.chats (
                            user_id, ltm_enabled, memory_epoch, private_data_blocked
                        )
                        SELECT account.user_id,
                               COALESCE(chat.ltm_enabled, TRUE),
                               barrier.memory_epoch,
                               TRUE
                        FROM public.users AS account
                        CROSS JOIN barrier
                        LEFT JOIN public.chats AS chat
                          ON chat.user_id = account.user_id
                        WHERE account.user_id = $1
                        ON CONFLICT (user_id) DO UPDATE
                        SET memory_epoch = EXCLUDED.memory_epoch,
                            private_data_blocked = TRUE
                        RETURNING memory_epoch, ltm_enabled
                    )
                    SELECT memory_epoch, ltm_enabled FROM invalidated
                    """,
                    (user_id,),
                    conn=conn,
                )
                if not rows:
                    raise RuntimeError("privacy barrier requires an active account")
                # Assign before transaction.__aexit__: if cancellation lands
                # during COMMIT, the outer guard already knows what to restore.
                barrier_epoch = int(rows[0]["memory_epoch"])
                previous_ltm_enabled = bool(rows[0]["ltm_enabled"])
            finally:
                await clear_user_context(conn=conn)

        await wait_for_private_data_leases(
            user_id,
            before_epoch=barrier_epoch,
            ltm_only=ltm_only,
        )
        yield barrier_epoch, previous_ltm_enabled
    except BaseException:
        if barrier_epoch is not None:
            try:
                restored = await restore_private_data_barrier(
                    user_id,
                    barrier_epoch=barrier_epoch,
                    ltm_enabled=previous_ltm_enabled,
                )
                if not restored:
                    logging.info(
                        "Privacy barrier %d was replaced or completed for user %d",
                        barrier_epoch,
                        user_id,
                    )
            except Exception as restore_error:
                logging.error(
                    "Privacy barrier compensation failed for user %d: %s",
                    user_id,
                    restore_error,
                    exc_info=True,
                )
        raise


@asynccontextmanager
async def compensate_private_data_barrier_on_error(
    user_id: int,
    *,
    barrier_epoch: int,
    ltm_enabled: bool,
) -> AsyncIterator[None]:
    """Restore a barrier if the guarded drain/destructive phase fails."""
    try:
        yield
    except BaseException:
        try:
            restored = await restore_private_data_barrier(
                user_id,
                barrier_epoch=barrier_epoch,
                ltm_enabled=ltm_enabled,
            )
            if not restored:
                logging.error(
                    "Privacy barrier compensation was no longer applicable for user %d",
                    user_id,
                )
        except Exception as restore_error:
            logging.error(
                "Privacy barrier compensation failed for user %d: %s",
                user_id,
                restore_error,
                exc_info=True,
            )
        raise
