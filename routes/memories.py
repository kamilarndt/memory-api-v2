"""Memory API v2 — Memory CRUD + Search routes.

Router handlers focus on HTTP protocol only. All database operations
are delegated to MemoryRepository. Prompts are isolated in
repositories/prompts.py.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from core import get_config, get_embedding, verify_token
from db import get_db
from repositories.memory import MemoryRepository
from repositories.prompts import ENTITY_EXTRACTION_SYSTEM

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memories", tags=["memories"])


# ── Models (Pydantic) ────────────────────────────────────────────────────────

from pydantic import BaseModel


class AddMemoryRequest(BaseModel):
    content: str
    agent_id: str = "unknown"
    project_id: str = ""
    category: str = "general"
    tags: list[str] = []
    user_id: str = "default"
    importance: float = 0.5
    memory_type: str = "factual"
    pi_memory_key: str = ""
    session_id: Optional[str] = None
    expires_in_hours: Optional[int] = None
    extract: bool = False


class SearchRequest(BaseModel):
    query: str
    agent_id: Optional[str] = None
    project_id: Optional[str] = None
    category: Optional[str] = None
    limit: int = 5
    cross_agent: bool = True
    compact: bool = False


class CompositionalSearchRequest(BaseModel):
    entities: list[str]
    limit: int = 5


class UpdateMemoryRequest(BaseModel):
    content: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[list[str]] = None
    project_id: Optional[str] = None
    importance: Optional[float] = None
    memory_type: Optional[str] = None


class FeedbackRequest(BaseModel):
    agent_id: str
    feedback_type: str
    comment: str = ""


class ExtractFactsRequest(BaseModel):
    text: str
    agent_id: str = "unknown"
    project_id: str = ""
    category: str = "general"


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _extract_entities(content: str, openrouter_key: str) -> list[str]:
    """Isolated helper: call OpenRouter for entity extraction.

    Returns empty list on any failure — entity extraction is best-effort.
    """
    if not openrouter_key or not content:
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openrouter_key}",
                    "HTTP-Referer": "http://localhost:8766",
                    "X-Title": "Memory API v2",
                },
                json={
                    "model": "deepseek/deepseek-chat",
                    "messages": [
                        {"role": "system", "content": ENTITY_EXTRACTION_SYSTEM},
                        {"role": "user", "content": content},
                    ],
                    "max_tokens": 200,
                },
            )
            text = resp.json()["choices"][0]["message"]["content"].strip()
            # Strip markdown code block fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[-1] if "\n" in text else text.replace("```json", "").replace("```", "")
                text = text.strip().removesuffix("```").strip()
            if text.startswith("[") and text.endswith("]"):
                return json.loads(text)
    except Exception as e:
        logger.warning("Entity extraction failed (non-blocking): %s", e)
    return []


# ── Compact List ─────────────────────────────────────────────────────────────


@router.get("/compact", summary="List memories compact (public)")
async def list_memories_compact(
    agent_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    memory_type: Optional[str] = Query(None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db=Depends(get_db),
):
    """List memories with compact fields only. Public endpoint."""
    wheres = ["archived_at IS NULL"]
    params: list = []
    i = 1

    for col, val in [("agent_id", agent_id), ("project_id", project_id),
                     ("memory_type", memory_type)]:
        if val:
            wheres.append(f"{col} = ${i}")
            params.append(val)
            i += 1

    where = " AND ".join(wheres)
    params.extend([limit, offset])

    rows = await db.fetch(
        f"SELECT id, pi_memory_key, memory_type, importance, created_at "
        f"FROM memories WHERE {where} ORDER BY created_at DESC LIMIT ${i} OFFSET ${i+1}",
        *params,
    )
    results = []
    for r in rows:
        d = dict(r)
        if d.get("pi_memory_key") is None:
            d["pi_memory_key"] = ""
        results.append(d)
    return {"memories": results, "count": len(results)}


# ── CRUD ─────────────────────────────────────────────────────────────────────


@router.post("", summary="Add a memory")
async def add_memory(
    req: AddMemoryRequest,
    db=Depends(get_db),
    _: None = Depends(verify_token),
):
    """Create or update a memory with semantic dedup and optional entity extraction."""
    config = get_config()
    repo = MemoryRepository()

    # 1. Embed
    emb = await get_embedding(req.content, config)
    if not emb:
        raise HTTPException(500, "Embedding generation failed")

    # 2. Upsert by pi_memory_key (exact match, highest priority)
    if req.pi_memory_key:
        existing = await repo.find_by_pi_key(db, req.pi_memory_key, req.agent_id, req.project_id)
        if existing:
            await repo.inline_upsert_memory(db, existing, req.content, req.session_id, req.expires_in_hours)
            return {"status": "updated", "id": existing, "revision": True}

    # 3. Semantic dedup — merge instead of reject
    existing = await repo.check_semantic_duplicate(db, emb)
    if existing:
        await repo.inline_upsert_memory(db, existing, req.content, req.session_id, req.expires_in_hours)
        return {"status": "updated", "id": existing, "agent_id": req.agent_id}

    # 4. Fresh insert
    mid = str(uuid.uuid4())
    await repo.insert_memory(
        conn=db, mid=mid, content=req.content, emb=emb,
        agent_id=req.agent_id, project_id=req.project_id,
        user_id=req.user_id, category=req.category, tags=req.tags,
        importance=req.importance, memory_type=req.memory_type,
        pi_memory_key=req.pi_memory_key, session_id=req.session_id,
        expires_in_hours=req.expires_in_hours,
    )

    # 5. Conditional entity enrichment
    if req.extract:
        entities = await _extract_entities(req.content, config.openrouter_key)
        if entities:
            await repo.update_entities(db, mid, entities)

    return {"status": "added", "id": mid, "agent_id": req.agent_id}


@router.get("", summary="List memories with filters")
async def list_memories(
    agent_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    memory_type: Optional[str] = Query(None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db=Depends(get_db),
):
    """List active memories with optional filters."""
    repo = MemoryRepository()
    wheres = ["archived_at IS NULL"]
    params: list = []
    i = 1

    for col, val in [("agent_id", agent_id), ("project_id", project_id),
                     ("category", category), ("memory_type", memory_type)]:
        if val:
            wheres.append(f"{col} = ${i}")
            params.append(val)
            i += 1

    where = " AND ".join(wheres)
    params.extend([limit, offset])

    rows = await db.fetch(
        f"SELECT id, content, category, agent_id, project_id, user_id, tags, "
        f"source_file, importance, trust_score, memory_type, created_at "
        f"FROM memories WHERE {where} ORDER BY created_at DESC LIMIT ${i} OFFSET ${i+1}",
        *params,
    )
    return {
        "memories": [repo.serialize_row(dict(r)) for r in rows],
        "count": len(rows),
    }


@router.get("/{memory_id}", summary="Get single memory")
async def get_memory(
    memory_id: str,
    db=Depends(get_db),
):
    """Fetch a single memory by ID."""
    repo = MemoryRepository()
    row = await repo.get_by_id(db, memory_id)
    if not row:
        raise HTTPException(404, "Memory not found")
    return {"memory": repo.serialize_row(row)}


@router.put("/{memory_id}", summary="Update memory (agent must own)")
async def update_memory(
    memory_id: str,
    req: UpdateMemoryRequest,
    db=Depends(get_db),
    _: None = Depends(verify_token),
):
    """Update specific fields on a memory."""
    repo = MemoryRepository()
    updates = {}
    for col in ("content", "category", "project_id", "importance", "memory_type"):
        val = getattr(req, col)
        if val is not None:
            updates[col] = val
    if req.tags is not None:
        updates["tags"] = req.tags

    if not updates:
        raise HTTPException(400, "No fields to update")

    updated = await repo.update_memory_fields(db, memory_id, **updates)
    if not updated:
        raise HTTPException(404, "Memory not found or archived")
    return {"status": "updated", "memory": updated}


@router.delete("/{memory_id}", summary="Soft delete memory (agent must own)")
async def delete_memory(
    memory_id: str,
    db=Depends(get_db),
    _: None = Depends(verify_token),
):
    """Archive (soft-delete) a memory."""
    repo = MemoryRepository()
    if not await repo.soft_delete(db, memory_id):
        raise HTTPException(404, "Memory not found or already archived")
    return {"status": "archived", "memory_id": memory_id}


@router.post("/{memory_id}/feedback", summary="Submit feedback on a memory")
async def submit_feedback(
    memory_id: str,
    req: FeedbackRequest,
    db=Depends(get_db),
    _: None = Depends(verify_token),
):
    """Update trust_score based on feedback type.

    - helpful   → trust_score = 1.0
    - unhelpful → trust_score = 0.5
    - incorrect → trust_score = 0.0
    - outdated  → trust_score = 0.3
    """
    repo = MemoryRepository()

    # Verify memory exists
    row = await repo.get_by_id(db, memory_id)
    if not row:
        raise HTTPException(404, "Memory not found")

    # Map feedback type to trust score
    trust_map = {
        "helpful": 1.0,
        "unhelpful": 0.5,
        "incorrect": 0.0,
        "outdated": 0.3,
    }
    if req.feedback_type not in trust_map:
        raise HTTPException(
            400,
            f"Invalid feedback_type '{req.feedback_type}'. "
            f"Must be one of: {', '.join(trust_map)}",
        )

    new_trust = trust_map[req.feedback_type]
    updated = await repo.update_memory_fields(
        db, memory_id, trust_score=new_trust,
    )
    if not updated:
        raise HTTPException(404, "Memory not found or archived")

    logger.info(
        "Feedback %s on memory %s by agent %s — trust_score → %.1f",
        req.feedback_type, memory_id, req.agent_id, new_trust,
    )

    return {
        "status": "ok",
        "memory_id": memory_id,
        "new_trust_score": new_trust,
    }


# ── Full Memory Detail ───────────────────────────────────────────────────────


@router.get("/{memory_id}/full", summary="Get full memory detail (public)")
async def get_memory_full(
    memory_id: str,
    db=Depends(get_db),
):
    """Return full record (without embedding/tsv) plus relations_count. Public."""
    repo = MemoryRepository()
    row = await db.fetchrow(
        """SELECT id, content, agent_id, project_id, user_id, category, tags,
                  source_file, importance, trust_score, memory_type, pi_memory_key,
                  session_id, entities, access_count, expires_at, created_at, updated_at,
                  archived_at, memory_relations_count
           FROM memories
           WHERE id = $1""",
        memory_id,
    )
    if not row:
        raise HTTPException(404, "Memory not found")
    return {"memory": repo.serialize_row(dict(row))}


# ── Search ───────────────────────────────────────────────────────────────────

search_router = APIRouter(tags=["search"])


@search_router.post("/search", summary="Hybrid search: semantic + keyword (RRF)")
async def search_memories(
    req: SearchRequest,
    db=Depends(get_db),
):
    """Hybrid search with RRF fusion."""
    config = get_config()
    emb = await get_embedding(req.query, config)
    if not emb:
        raise HTTPException(500, "Embedding generation failed")

    repo = MemoryRepository()
    rows = await repo.hybrid_search(
        conn=db, emb=emb, query_text=req.query,
        agent_id=req.agent_id, project_id=req.project_id,
        category=req.category, cross_agent=req.cross_agent,
        limit=req.limit,
    )

    results = []
    for r in rows:
        d = dict(r)
        d["confidence"] = round(float(d.pop("rrf_score")), 4)
        if req.compact:
            results.append({
                "id": d["id"],
                "pi_memory_key": d.get("pi_memory_key") or "",
                "memory_type": d["memory_type"],
                "confidence": d["confidence"],
                "created_at": d["created_at"],
            })
        else:
            d["excerpt"] = (
                d["content"][:300] + "..."
                if len(d["content"]) >= 300
                else d["content"]
            )
            results.append(d)

    return {"results": results, "count": len(results)}


@search_router.post("/search/compositional", summary="Find memories with ALL specified entities")
async def compositional_search(
    req: CompositionalSearchRequest,
    db=Depends(get_db),
):
    """Find memories containing ALL specified entities (AND logic)."""
    if not req.entities:
        raise HTTPException(400, "entities list required")
    repo = MemoryRepository()
    rows = await repo.compositional_search(db, req.entities, req.limit)
    return {
        "results": [repo.serialize_row(dict(r)) for r in rows],
        "count": len(rows),
    }


@search_router.get("/memories/{memory_id}/related", summary="Find related memories")
async def get_related_memories(
    memory_id: str,
    limit: int = Query(default=10, ge=1, le=50),
    db=Depends(get_db),
):
    """Find related memories: entity overlap first, then semantic fallback."""
    repo = MemoryRepository()
    results = await repo.get_related(db, memory_id, limit)
    return {"results": results, "count": len(results)}