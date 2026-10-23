"""asyncpg connection pool, created at app startup and shared across requests."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from .config import settings

_pool: asyncpg.Pool | None = None


async def connect() -> None:
    """Open the shared connection pool. Called once on startup."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.dsn,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            command_timeout=30,
        )


async def disconnect() -> None:
    """Close the pool. Called once on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialised")
    return _pool


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """A single connection inside a transaction, for a check plus the write
    that depends on it."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            yield conn


async def fetch(query: str, *args,
                conn: asyncpg.Connection | None = None) -> list[asyncpg.Record]:
    if conn is not None:
        return await conn.fetch(query, *args)
    async with pool().acquire() as c:
        return await c.fetch(query, *args)


async def fetchrow(query: str, *args,
                   conn: asyncpg.Connection | None = None) -> asyncpg.Record | None:
    if conn is not None:
        return await conn.fetchrow(query, *args)
    async with pool().acquire() as c:
        return await c.fetchrow(query, *args)
