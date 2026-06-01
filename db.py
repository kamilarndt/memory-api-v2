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

        # Auto-create FTS extensions on startup
        await conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

        # Add columns for full Memory API v2 functionality
        await conn.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
        await conn.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS memory_relations_count INTEGER DEFAULT 0")
        await conn.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS session_id UUID")

        # Create memory_relations table for graph relations (no conflict - already exists)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_relations (
                id UUID PRIMARY KEY,
                source_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                target_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                relation_type TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_mr_source ON memory_relations(source_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_mr_target ON memory_relations(target_id)")

        # Sessions table — existing table has text IDs, add columns if missing
        await conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)")
        for col in ["agent_id TEXT", "project_id TEXT DEFAULT ''", "status TEXT DEFAULT 'active'",
                     "summary TEXT DEFAULT ''", "started_at TIMESTAMPTZ DEFAULT NOW()", "ended_at TIMESTAMPTZ"]:
            try:
                await conn.execute(f"ALTER TABLE sessions ADD COLUMN IF NOT EXISTS {col}")
            except Exception:
                pass
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id)")
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id)")
        except Exception:
            pass

        # Conflict verdicts table (no FK to sessions — text vs UUID mismatch with existing DB)
# Memory conflict verdicts table (prefixed for consistency)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_conflict_verdicts (
                id UUID PRIMARY KEY,
                source_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                target_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                verdict TEXT NOT NULL CHECK (verdict IN ('contradictory', 'supportive', 'independent', 'superseded')),
                reason TEXT DEFAULT '',
                evidence TEXT DEFAULT '',
                confidence REAL DEFAULT 0.5,
                model TEXT DEFAULT '',
                session_id UUID REFERENCES memory_sessions(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_mcv_source ON memory_conflict_verdicts(source_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_mcv_target ON memory_conflict_verdicts(target_id)")
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mcv_unique_pair
            ON memory_conflict_verdicts(source_id, target_id)
        """)
        # Memory prompts table (prefixed for consistency)
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_prompts (
                    id UUID PRIMARY KEY,
                    session_id UUID REFERENCES memory_sessions(id),
                    agent_id TEXT NOT NULL,
                    project_id TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_mp_session ON memory_prompts(session_id)")
        except Exception as e:
            logger.warning("Schema: memory_prompts skipped (%s)", e)

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
