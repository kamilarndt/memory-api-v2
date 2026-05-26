#!/usr/bin/env python3
"""Migrate embedding vectors from 1024d to 1536d.

Usage:
    cd /home/ArndtOs/Tools/memory-api-v2
    source venv/bin/activate
    python scripts/migrate-embed-dim.py
"""

import asyncio
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def migrate():
    from core import get_config, get_embedding
    from db import _pool

    config = get_config()

    if not _pool:
        logger.error("Database not initialized. Start the server first or initialize pool.")
        return

    async with _pool.acquire() as conn:
        # 1. Check current dimension
        dim_result = await conn.fetchval("""
            SELECT coalesce(
                (SELECT dimension
                 FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'memories'
                   AND column_name = 'embedding'),
                0
            )
        """)
        logger.info("Current embedding dimension: %s", dim_result)

        # 2. Find memories with old-dimension embeddings
        # Check a sample to see what dimension they have
        sample = await conn.fetch("SELECT id, length(embedding::text) as len FROM memories LIMIT 5")
        for r in sample:
            logger.info("Memory %s: embedding length %d", r['id'], r['len'])

        # 3. Re-embed memories that need it (low trust or old format)
        to_reembed = await conn.fetch(
            "SELECT id, content FROM memories WHERE (trust_score < 0.5 OR trust_score IS NULL) AND content IS NOT NULL LIMIT 50"
        )
        logger.info("Memories to re-embed: %d", len(to_reembed))

        reembedded = 0
        for row in to_reembed:
            emb = await get_embedding(row["content"], config)
            if emb and len(emb) == 1536:
                es = f"[{','.join(map(str, emb))}]"
                await conn.execute(
                    "UPDATE memories SET embedding = $1::vector, trust_score = COALESCE(trust_score, 0.5) WHERE id = $2",
                    es, row["id"],
                )
                reembedded += 1
                if reembedded % 10 == 0:
                    logger.info("Re-embedded %d memories...", reembedded)

        logger.info("Migration complete: %d memories re-embedded to 1536d", reembedded)


if __name__ == "__main__":
    asyncio.run(migrate())
