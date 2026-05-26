"""Memory API v2 — Setup routes: FTS, extensions, migrations."""

from __future__ import annotations

import logging
from fastapi import APIRouter, Depends

from core import verify_token
from db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/setup", tags=["setup"])


@router.post("/fts", summary="Initialize Polish FTS configuration")
async def setup_fts(
    db=Depends(get_db),
    _: None = Depends(verify_token),
):
    """Create unaccent extension + Polish FTS config + tsv trigger on memories."""
    await db.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    await db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Create Polish unaccent text search configuration
    config_exists = await db.fetchval("""
        SELECT 1 FROM pg_ts_config WHERE cfgname = 'polish_unaccent'
    """)
    if not config_exists:
        await db.execute("CREATE TEXT SEARCH CONFIGURATION polish_unaccent ( COPY = pg_catalog.simple )")
        await db.execute("""
            ALTER TEXT SEARCH CONFIGURATION polish_unaccent
            ALTER MAPPING FOR hword, hword_part, word
            WITH unaccent, simple
        """)

    # Check if tsv column exists on memories
    col_exists = await db.fetchval("""
        SELECT EXISTS (
            SELECT FROM information_schema.columns
            WHERE table_name = 'memories' AND column_name = 'tsv'
        )
    """)

    if not col_exists:
        await db.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS tsv tsvector")
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_tsv
            ON memories USING GIN(tsv)
        """)

    # Create trigger function
    await db.execute("""
        CREATE OR REPLACE FUNCTION update_memories_tsv() RETURNS trigger AS $$
        BEGIN
            NEW.tsv :=
                to_tsvector('english', COALESCE(NEW.content, '')) ||
                to_tsvector('polish_unaccent', COALESCE(NEW.content, ''));
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    # Drop and recreate trigger (idempotent)
    await db.execute("DROP TRIGGER IF EXISTS trg_memories_tsv ON memories")
    await db.execute("""
        CREATE TRIGGER trg_memories_tsv
        BEFORE INSERT OR UPDATE OF content ON memories
        FOR EACH ROW EXECUTE FUNCTION update_memories_tsv()
    """)

    # Backfill existing rows
    result = await db.execute("""
        UPDATE memories SET tsv =
            to_tsvector('english', COALESCE(content, '')) ||
            to_tsvector('polish_unaccent', COALESCE(content, ''))
        WHERE tsv IS NULL
    """)
    updated = result.split()[-1] if result else "0"

    return {"status": "ok", "fts_configured": True, "rows_backfilled": int(updated)}
