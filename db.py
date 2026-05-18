"""Memory API v2 — asyncpg connection pool."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

from core import Config, get_config

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool(config: Config | None = None) -> asyncpg.Pool:
    global _pool
    config = config or get_config()

    _pool = await asyncpg.create_pool(
        dsn=config.dsn,
        min_size=2,
        max_size=20,
        command_timeout=30,
        max_inactive_connection_lifetime=300,
    )

    async with _pool.acquire() as conn:
        val = await conn.fetchval("SELECT 1")
        assert val == 1

    logger.info(
        "Database pool created: %s (min=%d, max=%d)",
        config.db_name, _pool.get_min_size(), _pool.get_max_size(),
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        logger.info("Database pool closed")
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Get the current pool. Raises if not initialized."""
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    return _pool


async def get_db():
    """FastAPI dependency — acquire/release a connection.

    NOTE: Do NOT use @asynccontextmanager here. FastAPI handles
    async yield deps natively — decorating breaks it.
    """
    pool = get_pool()
    conn = await pool.acquire()
    try:
        yield conn
    finally:
        await pool.release(conn)
