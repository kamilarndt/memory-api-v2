"""Memory API v2 — Legacy alias endpoints for Pi Agent compatibility."""

from __future__ import annotations

from typing import Optional
import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Body
from core import get_config, get_embedding, verify_token
from db import get_db

router = APIRouter(tags=["pi-compat"])


@router.post("/pi-remember", summary="Pi Agent save fact with pi_memory_key")
async def pi_remember(
    content: str = Body(..., embed=True),
    pi_memory_key: str = Body(default="", alias="key", embed=True),
    agent_id: str = Body(default="pi-agent", embed=True),
    project_id: str = Body(default="", embed=True),
    category: str = Body(default="general", embed=True),
    importance: float = Body(default=0.5, embed=True),
    tags: str = Body(default="", embed=True),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Save a fact with pi_memory_key — legacy endpoint for Pi Agent."""
    config = get_config()
    emb = await get_embedding(content, config)
    if not emb:
        raise HTTPException(500, "Embedding failed")

    es = f"[{','.join(str(v) for v in emb)}]"
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    # Dedup
    existing = await db.fetchrow(
        "SELECT id FROM memories WHERE 1 - (embedding <=> $1::vector) > 0.95 AND archived_at IS NULL LIMIT 1",
        es,
    )
    if existing:
        return {"status": "duplicate", "existing_id": str(existing["id"])}

    import uuid
    mid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO memories (id, content, embedding, agent_id, project_id, user_id, "
        "category, tags, importance, memory_type, pi_memory_key, created_at) "
        "VALUES ($1, $2, $3::vector, $4, $5, $6, $7, $8, $9, $10, $11, NOW())",
        mid, content, es, agent_id, project_id, "default",
        category, tag_list, importance, "lesson" if pi_memory_key.startswith("lesson") else "factual",
        pi_memory_key,
    )
    return {"status": "added", "id": mid, "pi_memory_key": pi_memory_key}


@router.post("/pi-search", summary="Pi Agent search with pi_memory_key filter")
async def pi_search(
    query: str = Body(..., embed=True),
    pi_memory_key: str = Body(default="", embed=True),
    limit: int = Body(default=5, embed=True),
    db: asyncpg.Connection = Depends(get_db),
):
    """Search memories with optional pi_memory_key filter."""
    config = get_config()
    emb = await get_embedding(query, config)
    if not emb:
        raise HTTPException(500, "Embedding failed")

    es = f"[{','.join(str(v) for v in emb)}]"
    where = "m.archived_at IS NULL AND m.embedding IS NOT NULL"
    params: list = []
    i = 1

    if pi_memory_key:
        # Support wildcard: "lesson.*" → LIKE 'lesson.%'
        if pi_memory_key.endswith(".*"):
            where += f" AND m.pi_memory_key LIKE ${i}"
            params.append(pi_memory_key.replace(".*", ".%"))
        else:
            where += f" AND m.pi_memory_key = ${i}"
            params.append(pi_memory_key)
        i += 1

    rows = await db.fetch(
        f"""
        WITH semantic_raw AS (
            SELECT m.id,
                   1 - (m.embedding <=> ${i}::vector) as cosine_sim
            FROM memories m WHERE {where}
            ORDER BY cosine_sim DESC LIMIT 50
        ),
        keyword_search AS (
            SELECT m.id, ROW_NUMBER() OVER (ORDER BY ts_rank(m.tsv, websearch_to_tsquery('simple', ${i+1})) DESC) as rank
            FROM memories m WHERE m.tsv @@ websearch_to_tsquery('simple', ${i+1}) AND {where} LIMIT 50
        )
        SELECT m.id, m.content, m.category, m.agent_id, m.project_id, m.pi_memory_key,
               m.tags, m.trust_score, m.created_at,
               s.cosine_sim,
               COALESCE(1.0 / (10.0 + k.rank), 0.0) as kw_score
        FROM semantic_raw s
        LEFT JOIN keyword_search k ON s.id = k.id
        JOIN memories m ON m.id = s.id
        ORDER BY GREATEST(s.cosine_sim, COALESCE(1.0 / (10.0 + k.rank), 0.0)) DESC
        LIMIT ${i+2}
        """,
        *(params + [es, query, limit]),
    )

    results = []
    for r in rows:
        d = dict(r)
        d["excerpt"] = (d["content"][:300] + "..." if len(d["content"]) >= 300 else d["content"])
        # confidence = max(cosine_sim, kw_score) — daje 0.0-1.0 zamiast RRF 0.0-0.033
        raw_cosine = d.pop("cosine_sim", None)
        raw_kw = d.pop("kw_score", None)
        cosine = float(raw_cosine) if raw_cosine is not None else 0.0
        kw = float(raw_kw) if raw_kw is not None else 0.0
        d["confidence"] = round(max(cosine, kw), 4)
        results.append(d)

    return {"results": results, "count": len(results)}


@router.post("/save_fact", summary="Legacy save_fact endpoint")
async def save_fact(
    content: str = Body(..., embed=True),
    pi_memory_key: str = Body(default="", alias="key", embed=True),
    agent_id: str = Body(default="pi-agent", embed=True),
    category: str = Body(default="lesson", embed=True),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Legacy save_fact — redirects to pi-remember logic."""
    config = get_config()
    emb = await get_embedding(content, config)
    if not emb:
        raise HTTPException(500, "Embedding failed")

    es = f"[{','.join(str(v) for v in emb)}]"
    existing = await db.fetchrow(
        "SELECT id FROM memories WHERE 1 - (embedding <=> $1::vector) > 0.95 AND archived_at IS NULL LIMIT 1",
        es,
    )
    if existing:
        return {"status": "duplicate", "existing_id": str(existing["id"])}

    import uuid
    mid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO memories (id, content, embedding, agent_id, project_id, user_id, "
        "category, tags, importance, memory_type, pi_memory_key, created_at) "
        "VALUES ($1, $2, $3::vector, $4, $5, $6, $7, $8, $9, $10, $11, NOW())",
        mid, content, es, agent_id, "", "default",
        category, [], 0.5, "factual", pi_memory_key,
    )
    return {"status": "saved", "id": mid, "pi_memory_key": pi_memory_key}


@router.get("/notebooklm-ask", summary="Legacy endpoint (stub)")
async def notebooklm_ask_stub():
    """Legacy NotebookLM endpoint — not implemented in v2."""
    return {"status": "not_implemented", "message": "Use Memory API search instead"}


@router.post("/notebooklm-ask", summary="Legacy endpoint (stub)")
async def notebooklm_ask_post_stub():
    """Legacy NotebookLM endpoint — not implemented in v2."""
    return {"status": "not_implemented", "message": "Use Memory API search instead"}
