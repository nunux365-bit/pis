"""Skip-locked row claims. Released on transaction commit/rollback — never advisory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResourceClaim
from app.db.session import AsyncSessionLocal

PROSIGHT_ACTIONABLES_CLAIM = "prosight:actionables"


def refsync_claim_name(domain: str) -> str:
    return f"refsync:{domain}"


async def ensure_claim_row(name: str) -> None:
    """Insert the claim row in its own committed txn so later SKIP LOCKED can see it.

    Do not call this while another session holds the row — PostgreSQL
    ``INSERT ON CONFLICT`` waits on that tuple. Use :func:`acquire_claim`.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(
            insert(ResourceClaim).values(name=name).on_conflict_do_nothing()
        )
        await session.commit()


async def try_claim_row(session: AsyncSession, name: str) -> bool:
    """True if this txn holds ``name`` until commit/rollback. Miss means skip, not wait."""
    result = await session.execute(
        select(ResourceClaim.name)
        .where(ResourceClaim.name == name)
        .with_for_update(skip_locked=True)
    )
    return result.scalar_one_or_none() is not None


async def acquire_claim(session: AsyncSession, name: str) -> bool:
    """Take ``name`` with SKIP LOCKED. Missing row is inserted once, then retried.

    If the row exists but is locked, returns False immediately (does not wait).
    """
    if await try_claim_row(session, name):
        return True
    exists = await session.scalar(
        select(ResourceClaim.name).where(ResourceClaim.name == name)
    )
    if exists is not None:
        return False
    await ensure_claim_row(name)
    return await try_claim_row(session, name)


@asynccontextmanager
async def claimed_session(name: str) -> AsyncIterator[AsyncSession | None]:
    """Yield a session that holds ``name``, or None if another writer has it."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            if not await acquire_claim(session, name):
                yield None
            else:
                yield session
