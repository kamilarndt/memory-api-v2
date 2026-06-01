"""Memory API v2 — Legacy alias endpoints for Pi Agent compatibility.

All database operations are delegated to MemoryRepository to eliminate
duplicated SQL and vector formatting logic.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Body

from core import get_config, get_embedding, verify_token
from db import get_db
from repositories.memory import MemoryRepository

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
    db=Depends(get_db),
    _: None = Depends(verify_token),
):
    """Save a fact with pi_memory_key — legacy endpoint for Pi Agent."""
    config = get_config()
    repo = MemoryRepository()

    emb = await get_embedding(content, config)
    if not emb:
        raise HTTPException(500, "Embedding failed")

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    memory_type = "lesson" if pi_memory_key.startswith("lesson") else "factual"

    # Dedup by semantic similarity (existing pi-agent behavior: reject duplicates)
    existing = await repo.check_semantic_duplicate(db, emb)
    if existing:
        return {"status": "duplicate", "existing_id": existing}

    mid = str(uuid.uuid4())
    await repo.insert_memory(
        conn=db, mid=mid, content=content, emb=emb,
        agent_id=agent_id, project_id=project_id,
        user_id="default", category=category, tags=tag_list,
        importance=importance, memory_type=memory_type,
        pi_memory_key=pi_memory_key,
    )
    return {"status": "added", "id": mid, "pi_memory_key": pi_memory_key}


@router.post("/pi-search", summary="Pi Agent search with pi_memory_key filter")
async def pi_search(
    query: str = Body(..., embed=True),
    pi_memory_key: str = Body(default="", embed=True),
    limit: int = Body(default=5, embed=True),
    db=Depends(get_db),
):
    """Search memories with optional pi_memory_key filter (wildcard supported)."""
    config = get_config()
    repo = MemoryRepository()

    emb = await get_embedding(query, config)
    if not emb:
        raise HTTPException(500, "Embedding failed")

    rows = await repo.search_by_pi_key(db, emb, query, pi_memory_key, limit)

    results = []
    for r in rows:
        d = dict(r)
        d["excerpt"] = (
            d["content"][:300] + "..." if len(d["content"]) >= 300 else d["content"]
        )
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
    db=Depends(get_db),
    _: None = Depends(verify_token),
):
    """Legacy save_fact — delegates to pi-remember logic via repository."""
    config = get_config()
    repo = MemoryRepository()

    emb = await get_embedding(content, config)
    if not emb:
        raise HTTPException(500, "Embedding failed")

    existing = await repo.check_semantic_duplicate(db, emb)
    if existing:
        return {"status": "duplicate", "existing_id": existing}

    mid = str(uuid.uuid4())
    await repo.insert_memory(
        conn=db, mid=mid, content=content, emb=emb,
        agent_id=agent_id, project_id="",
        user_id="default", category=category, tags=[],
        importance=0.5, memory_type="factual",
        pi_memory_key=pi_memory_key,
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