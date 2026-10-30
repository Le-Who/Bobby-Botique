"""Database ownership for a date/language; no provider calls under row locks."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid4

from app import database as db


class TarotGenerationConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class GenerationTicket:
    date: date
    language: str
    revision: int
    token: UUID


@asynccontextmanager
async def transaction(conn=None):
    if conn is not None:
        async with conn.transaction():
            yield conn
        return
    if db.db_manager.pool is None:
        raise RuntimeError("Database unavailable")
    async with db.db_manager.pool.acquire() as acquired, acquired.transaction():
        yield acquired


async def begin_generation(day: date, language: str, *, conn=None) -> GenerationTicket:
    from app.games.daily_execution import checkpoint

    await checkpoint()
    token = uuid4()
    async with transaction(conn) as acquired:
        row = await acquired.fetchrow(
            """INSERT INTO public.tarot_daily_generation(reading_date,language,revision,token)
               VALUES($1,$2,1,$3) ON CONFLICT(reading_date,language) DO UPDATE
               SET revision=tarot_daily_generation.revision+1,token=EXCLUDED.token,updated_at=NOW()
               RETURNING revision""",
            day,
            language,
            token,
        )
    return GenerationTicket(day, language, row["revision"], token)


async def finish_generation(ticket: GenerationTicket, *, conn=None) -> bool:
    async with transaction(conn) as acquired:
        row = await acquired.fetchrow(
            """UPDATE public.tarot_daily_generation SET token=NULL,updated_at=NOW()
               WHERE reading_date=$1 AND language=$2 AND revision=$3 AND token=$4 RETURNING revision""",
            ticket.date,
            ticket.language,
            ticket.revision,
            ticket.token,
        )
        return row is not None


async def lock_generation(conn, day: date, language: str, ticket: GenerationTicket | None) -> bool:
    await conn.execute(
        """INSERT INTO public.tarot_daily_generation(reading_date,language,revision)
                          VALUES($1,$2,1) ON CONFLICT DO NOTHING""",
        day,
        language,
    )
    row = await conn.fetchrow(
        """SELECT revision,token FROM public.tarot_daily_generation
                                WHERE reading_date=$1 AND language=$2 FOR UPDATE""",
        day,
        language,
    )
    if ticket is None:
        return row["token"] is None
    if (
        ticket.date != day
        or ticket.language != language
        or row["revision"] != ticket.revision
        or row["token"] != ticket.token
    ):
        raise TarotGenerationConflict("revision_conflict")
    return True
