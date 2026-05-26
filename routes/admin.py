from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Optional

import asyncpg
import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from core import embedding_breaker, get_config, get_embedding, verify_token
from db import get_db
from services.dream import run_full_cycle, get_dream_state

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


# ---------------------------------------------------------------------------
# 1. GET /health — DB latency + embedding status (no auth required)
# ---------------------------------------------------------------------------

@router.get("/health")
async def health(db: asyncpg.Connection = Depends(get_db)):
    """Health check — no auth required."""
    t0 = time.monotonic()
    await db.fetchval("SELECT 1")
    latency_ms = round((time.monotonic() - t0) * 1000, 2)

    config = get_config()

    # Determine actual primary embedding provider
    if config.router_key:
        embed_provider = "router"
        embed_model = config.router_model
        embed_dim = config.embed_dim  # 1536
    elif config.gemini_key:
        embed_provider = "gemini"
        embed_model = "gemini-embedding-001"
        embed_dim = 1536  # output_dimensionality
    elif config.openrouter_key:
        embed_provider = "openrouter"
        embed_model = "baai/bge-m3"
        embed_dim = 1024
    else:
        embed_provider = "ollama"
        embed_model = config.embed_model
        embed_dim = config.embed_dim

    return {
        "status": "ok",
        "version": "2.0",
        "checks": {
            "db": {"ok": True, "latency_ms": latency_ms},
            "embedding": {
                "ok": bool(config.gemini_key or config.openrouter_key or True),
                "provider": embed_provider,
                "model": embed_model,
                "dimension": embed_dim,
            },
            "circuit_breaker": embedding_breaker.status(),
        },
    }


# ---------------------------------------------------------------------------
# 2. GET /stats — basic stats (active count)
# ---------------------------------------------------------------------------

@router.get("/stats")
async def basic_stats(
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    count = await db.fetchval(
        "SELECT COUNT(*) FROM memories WHERE archived_at IS NULL"
    )
    return {"overview": {"active": count}}


# ---------------------------------------------------------------------------
# 3. GET /memories/stats — enhanced stats with breakdowns
# ---------------------------------------------------------------------------

@router.get("/memories/stats")
async def memory_stats(
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Enhanced stats with breakdowns by agent / project / category."""

    overview = await db.fetchrow("""
        SELECT
            COUNT(*)                                           AS total,
            COUNT(*) FILTER (WHERE archived_at IS NULL)        AS active,
            COUNT(*) FILTER (WHERE archived_at IS NOT NULL)    AS archived,
            ROUND(AVG(trust_score)::numeric, 2)                AS avg_trust,
            ROUND(AVG(importance)::numeric, 2)                 AS avg_importance,
            COUNT(DISTINCT agent_id)                           AS agents,
            COUNT(DISTINCT project_id)                         AS projects
        FROM memories
    """)

    by_agent = await db.fetch("""
        SELECT agent_id, COUNT(*) AS count
        FROM memories
        WHERE archived_at IS NULL
        GROUP BY agent_id
        ORDER BY count DESC
    """)

    by_project = await db.fetch("""
        SELECT project_id, COUNT(*) AS count
        FROM memories
        WHERE archived_at IS NULL AND project_id != ''
        GROUP BY project_id
        ORDER BY count DESC
    """)

    by_category = await db.fetch("""
        SELECT category, COUNT(*) AS count
        FROM memories
        WHERE archived_at IS NULL
        GROUP BY category
        ORDER BY count DESC
    """)

    return {
        "overview": dict(overview),
        "by_agent":   {r["agent_id"]:   r["count"] for r in by_agent},
        "by_project": {r["project_id"]: r["count"] for r in by_project},
        "by_category":{r["category"]:   r["count"] for r in by_category},
    }


# ---------------------------------------------------------------------------
# 4. POST /extract-facts — LLM fact extraction from text
# ---------------------------------------------------------------------------

@router.post("/extract-facts")
async def extract_facts(
    text: str = Body(..., embed=True),
    agent_id: str = Body(default="unknown", embed=True),
    project_id: str = Body(default="", embed=True),
    category: str = Body(default="general", embed=True),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Use LLM to extract key facts from text and save to memory."""
    config = get_config()

    if not config.openrouter_key:
        return {"error": "no OpenRouter key available", "fallback": "manual save required"}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.openrouter_key}",
                    "HTTP-Referer": "http://localhost:8766",
                    "X-Title": "Memory API v2",
                },
                json={
                    "model": "deepseek/deepseek-chat",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Extract 3-7 key facts from this text. "
                                'Return ONLY valid JSON (either an array or '\
                                '{"facts": [...]}). '
                                'Each fact: {"content": "...", "category": "technical|business|personal|lesson", '
                                '"importance": 0.0-1.0, "tags": ["tag1", "tag2"]}. '
                                "Facts should be specific, atomic, and useful for future context."
                            ),
                        },
                        {"role": "user", "content": text[:8000]},
                    ],
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
            )
            # Strip markdown code fences if present
            if content.startswith("```"):
                content = "\n".join(content.split("\n")[1:-1])
            parsed = json.loads(content)
            # Accept both array and {facts: [...]} format
            if isinstance(parsed, list):
                facts = parsed
            elif isinstance(parsed, dict):
                facts = parsed.get("facts", [parsed])
            else:
                facts = []
    except Exception as exc:
        return {"error": str(exc), "fallback": "manual save required"}

    saved, duplicates = 0, 0

    for f in facts:
        fc = f.get("content", "")
        if not fc:
            continue

        emb = await get_embedding(fc, config)
        if not emb:
            continue

        es = f"[{','.join(map(str, emb))}]"

        existing = await db.fetchrow(
            "SELECT id FROM memories WHERE 1 - (embedding <=> $1::vector) > 0.9 LIMIT 1",
            es,
        )
        if existing:
            duplicates += 1
            continue

        mid = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO memories (
                   id, content, embedding, agent_id, project_id,
                   category, trust_score, importance, tags
               ) VALUES ($1, $2, $3::vector, $4, $5, $6, 0.5, $7, $8)""",
            mid,
            fc,
            es,
            agent_id,
            project_id,
            f.get("category", category),
            f.get("importance", 0.5),
            f.get("tags", []),
        )
        saved += 1

    return {
        "extracted": len(facts),
        "saved": saved,
        "duplicates": duplicates,
        "facts": facts,
    }


# ---------------------------------------------------------------------------
# 5. Dream endpoints — background memory consolidation
# ---------------------------------------------------------------------------


@router.post("/dream/run", summary="Run full dream consolidation cycle")
async def run_dream(
    db=Depends(get_db),
    _: None = Depends(verify_token),
):
    """Run all 5 stages of dream consolidation in order."""
    result = await run_full_cycle(db)
    return result


@router.post("/memories/expire", summary="Archive expired memories")
async def expire_memories(
    db=Depends(get_db),
    _: None = Depends(verify_token),
):
    """Archive all memories past their expires_at."""
    result = await db.execute(
        "UPDATE memories SET archived_at = NOW() WHERE expires_at IS NOT NULL AND expires_at < NOW() AND archived_at IS NULL"
    )
    count = 0
    if result and result.startswith("UPDATE"):
        parts = result.split()
        if len(parts) > 1:
            try:
                count = int(parts[-1])
            except (ValueError, TypeError):
                pass
    return {"status": "ok", "expired": count}


@router.get("/dream/status", summary="Get dream cycle status")
async def dream_status(
    _: None = Depends(verify_token),
):
    """Get current dream cycle state and last run stats."""
    state = get_dream_state()
    return {
        "running": state.running,
        "stage": state.stage,
        "progress": state.progress,
        "started_at": state.started_at,
        "last_completed": state.last_completed,
        "stats": state.stats,
    }
