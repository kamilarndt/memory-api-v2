"""Memory API v2 — Memory CRUD + Search routes."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from core import Config, get_config, get_embedding, verify_token
from db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memories", tags=["memories"])


# ── Models (Pydantic) ────────────────────────────────────────────────────────

from pydantic import BaseModel, field_validator


class AddMemoryRequest(BaseModel):
    content: str
    agent_id: str = "unknown"
    project_id: str = ""
    category: str = "general"
    tags: list[str] = []
    user_id: str = "default"
    importance: float = 0.5
    memory_type: str = "factual"
    pi_memory_key: str = ""  # e.g. "pref.commit_style", "lesson.dont_use_echo"


class SearchRequest(BaseModel):
    query: str
    agent_id: Optional[str] = None
    project_id: Optional[str] = None
    category: Optional[str] = None
    limit: int = 5
    cross_agent: bool = True


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
    feedback_type: str  # helpful | not_helpful | incorrect | outdated
    comment: str = ""


class ExtractFactsRequest(BaseModel):
    text: str
    agent_id: str = "unknown"
    project_id: str = ""
    category: str = "general"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _emb_str(emb: list[float]) -> str:
    return f"[{','.join(str(v) for v in emb)}]"


def _serialize_row(row: dict) -> dict:
    """Convert asyncpg Record to plain dict, removing heavy fields."""
    d = dict(row)
    d.pop("embedding", None)
    if "tsv" in d:
        d.pop("tsv")
    # Convert arrays/timestamps to plain types
    for k, v in d.items():
        if isinstance(v, (set, frozenset)):
            d[k] = list(v)
    return d


async def _check_duplicate(
    db: asyncpg.Connection, emb: list[float], threshold: float = 0.95,
) -> Optional[str]:
    """Return existing memory ID if duplicate found."""
    es = _emb_str(emb)
    row = await db.fetchrow(
        "SELECT id FROM memories "
        "WHERE 1 - (embedding <=> $1::vector) > $2 AND archived_at IS NULL "
        "LIMIT 1",
        es, threshold,
    )
    return row["id"] if row else None


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.post("", summary="Add a memory")
async def add_memory(
    req: AddMemoryRequest,
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    config = get_config()
    emb = await get_embedding(req.content, config)
    if not emb:
        raise HTTPException(500, "Embedding generation failed")

    # Dedup check
    existing = await _check_duplicate(db, emb)
    if existing:
        return {"status": "duplicate", "existing_id": str(existing), "similarity": 0.95}

    mid = str(uuid.uuid4())
    es = _emb_str(emb)

    await db.execute(
        """INSERT INTO memories
           (id, content, embedding, agent_id, project_id, user_id, category, tags,
            importance, memory_type, pi_memory_key, created_at)
           VALUES ($1, $2, $3::vector, $4, $5, $6, $7, $8, $9, $10, $11, NOW())""",
        mid, req.content, es, req.agent_id, req.project_id,
        req.user_id, req.category, req.tags, req.importance, req.memory_type, req.pi_memory_key,
    )

    return {"status": "added", "id": mid, "agent_id": req.agent_id}


@router.get("", summary="List memories with filters")
async def list_memories(
    agent_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    memory_type: Optional[str] = Query(None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: asyncpg.Connection = Depends(get_db),
):
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
    return {"memories": [_serialize_row(dict(r)) for r in rows], "count": len(rows)}


@router.get("/{memory_id}", summary="Get single memory")
async def get_memory(
    memory_id: str,
    db: asyncpg.Connection = Depends(get_db),
):
    row = await db.fetchrow(
        "SELECT * FROM memories WHERE id = $1", memory_id,
    )
    if not row:
        raise HTTPException(404, "Memory not found")
    return {"memory": _serialize_row(dict(row))}


@router.put("/{memory_id}", summary="Update memory (agent must own)")
async def update_memory(
    memory_id: str,
    req: UpdateMemoryRequest,
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    fields = []
    params: list = []
    i = 1

    for col in ("content", "category", "project_id", "importance", "memory_type"):
        val = getattr(req, col)
        if val is not None:
            fields.append(f"{col} = ${i}")
            params.append(val)
            i += 1

    if req.tags is not None:
        fields.append(f"tags = ${i}")
        params.append(req.tags)
        i += 1

    if not fields:
        raise HTTPException(400, "No fields to update")

    fields.append("updated_at = NOW()")
    params.append(memory_id)

    row = await db.fetchrow(
        f"UPDATE memories SET {', '.join(fields)} "
        f"WHERE id = ${i} AND archived_at IS NULL "
        f"RETURNING id, content, category, agent_id, project_id, importance, memory_type",
        *params,
    )
    if not row:
        raise HTTPException(404, "Memory not found or archived")
    return {"status": "updated", "memory": dict(row)}


@router.delete("/{memory_id}", summary="Soft delete memory (agent must own)")
async def delete_memory(
    memory_id: str,
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    result = await db.execute(
        "UPDATE memories SET archived_at = NOW() WHERE id = $1 AND archived_at IS NULL",
        memory_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(404, "Memory not found or already archived")
    return {"status": "archived", "memory_id": memory_id}


# ── Search ───────────────────────────────────────────────────────────────────

search_router = APIRouter(tags=["search"])


@search_router.post("/search", summary="Hybrid search: semantic + keyword (RRF)")
async def search_memories(
    req: SearchRequest,
    db: asyncpg.Connection = Depends(get_db),
):
    config = get_config()
    emb = await get_embedding(req.query, config)
    if not emb:
        raise HTTPException(500, "Embedding generation failed")

    es = _emb_str(emb)
    wheres = ["m.archived_at IS NULL"]
    params: list = []
    i = 1

    agent_id = None if req.cross_agent else req.agent_id
    for col, val in [("m.agent_id", agent_id), ("m.project_id", req.project_id),
                     ("m.category", req.category)]:
        if val:
            wheres.append(f"{col} = ${i}")
            params.append(val)
            i += 1

    where = " AND ".join(wheres)

    rows = await db.fetch(
        f"""
        WITH semantic_search AS (
            SELECT m.id,
                   ROW_NUMBER() OVER (ORDER BY m.embedding <=> ${i}::vector) as rank
            FROM memories m
            WHERE {where}
            LIMIT 50
        ),
        keyword_search AS (
            SELECT m.id,
                   ROW_NUMBER() OVER (ORDER BY ts_rank(m.tsv, websearch_to_tsquery('simple', ${i+1})) DESC) as rank
            FROM memories m
            WHERE m.tsv @@ websearch_to_tsquery('simple', ${i+1})
              AND {where}
            LIMIT 50
        )
        SELECT m.id, m.content, m.category, m.agent_id, m.project_id, m.user_id,
               m.tags, m.source_file, m.importance, m.trust_score, m.memory_type, m.created_at,
               (COALESCE(1.0 / (60 + s.rank), 0.0) + COALESCE(1.0 / (60 + k.rank), 0.0)) as rrf_score
        FROM semantic_search s
        FULL OUTER JOIN keyword_search k ON s.id = k.id
        JOIN memories m ON m.id = COALESCE(s.id, k.id)
        ORDER BY rrf_score DESC
        LIMIT ${i+2}
        """,
        *(params + [es, req.query, req.limit]),
    )

    results = []
    for r in rows:
        d = dict(r)
        d["excerpt"] = (d["content"][:300] + "..." if len(d["content"]) >= 300 else d["content"])
        d["confidence"] = round(float(d.pop("rrf_score")), 4)
        results.append(d)

    return {"results": results, "count": len(results)}


@search_router.post("/search/compositional", summary="Find memories with ALL specified entities")
async def compositional_search(
    req: CompositionalSearchRequest,
    db: asyncpg.Connection = Depends(get_db),
):
    if not req.entities:
        raise HTTPException(400, "entities list required")

    placeholders = ",".join([f"${i+2}" for i in range(len(req.entities))])
    rows = await db.fetch(
        f"""
        SELECT id, content, agent_id, project_id, category, trust_score, importance,
               entities, memory_type, created_at
        FROM memories
        WHERE archived_at IS NULL
          AND entities::jsonb ?& ARRAY[{placeholders}]
        ORDER BY trust_score DESC, created_at DESC
        LIMIT $1
        """,
        req.limit, *req.entities,
    )

    return {"results": [_serialize_row(dict(r)) for r in rows], "count": len(rows)}


@search_router.get("/memories/{memory_id}/related", summary="Find related memories")
async def get_related_memories(
    memory_id: str,
    limit: int = Query(default=10, ge=1, le=50),
    db: asyncpg.Connection = Depends(get_db),
):
    row = await db.fetchrow(
        "SELECT embedding, entities FROM memories WHERE id = $1", memory_id,
    )
    if not row:
        raise HTTPException(404, "Memory not found")

    results = []

    # 1. Entity overlap first
    entities_val = row["entities"]
    if entities_val:
        entities_list = entities_val if isinstance(entities_val, list) else json.loads(entities_val) if isinstance(entities_val, str) else []

        if entities_list:
            placeholders = ",".join([f"${i+2}" for i in range(len(entities_list))])
            rows = await db.fetch(
                f"""
                SELECT id, content, agent_id, project_id, category, trust_score, created_at
                FROM memories WHERE id != $1 AND archived_at IS NULL
                  AND entities::jsonb ?| ARRAY[{placeholders}]
                ORDER BY trust_score DESC LIMIT ${len(entities_list)+3}
                """,
                memory_id, *[str(e) for e in entities_list], limit,
            )
            results = [_serialize_row(dict(r)) for r in rows]

    # 2. Semantic similarity fallback
    if len(results) < limit:
        emb = row["embedding"]
        if emb:
            es = _emb_str(emb) if isinstance(emb, list) else emb
            existing_ids = [r["id"] for r in results]

            id_filter = ""
            id_params = []
            for idx, eid in enumerate(existing_ids):
                id_filter += f" AND id != ${len(results) + idx + 2}"
                id_params.append(eid)

            rows = await db.fetch(
                f"""
                SELECT id, content, agent_id, project_id, category, trust_score, created_at,
                       1 - (embedding <=> ${len(results) + len(id_params) + 1}::vector) as similarity
                FROM memories WHERE id != $1 AND archived_at IS NULL {id_filter}
                ORDER BY similarity DESC LIMIT ${len(results) + len(id_params) + 2}
                """,
                memory_id, *id_params, es, limit - len(results),
            )
            for r in rows:
                d = dict(r)
                if d["id"] not in existing_ids:
                    results.append(d)

    return {"results": results[:limit], "count": len(results[:limit])}
