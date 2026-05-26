"""Memory relations — graph connections between memories."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from core import verify_token
from db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/memories/{memory_id}/relations", tags=["relations"])

VALID_TYPES = {
    "supports", "contradicts", "elaborates", "generalizes",
    "instance_of", "related", "causes", "depends_on",
}


@router.post("", summary="Create relation between two memories")
async def create_relation(
    memory_id: str,
    target_id: str = Query(..., description="Target memory ID"),
    relation_type: str = Query(..., description=f"One of: {', '.join(sorted(VALID_TYPES))}"),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Create a directed relation from source to target memory."""
    source = await db.fetchval("SELECT id FROM memories WHERE id = $1::uuid", memory_id)
    if not source:
        raise HTTPException(404, "Source memory not found")
    target = await db.fetchval("SELECT id FROM memories WHERE id = $1::uuid", target_id)
    if not target:
        raise HTTPException(404, "Target memory not found")
    if relation_type not in VALID_TYPES:
        raise HTTPException(400, f"Invalid relation_type. Valid: {', '.join(sorted(VALID_TYPES))}")

    rid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    await db.execute(
        "INSERT INTO memory_relations (id, source_id, target_id, relation_type, created_at) VALUES ($1, $2, $3, $4, $5)",
        rid, memory_id, target_id, relation_type, now,
    )
    return {
        "status": "ok",
        "relation": {"id": rid, "source_id": memory_id, "target_id": target_id, "relation_type": relation_type},
    }


@router.get("", summary="Get relations for a memory")
async def get_relations(
    memory_id: str,
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Return outgoing + incoming relations."""
    outgoing = await db.fetch(
        """SELECT id, target_id, relation_type, created_at
           FROM memory_relations WHERE source_id = $1::uuid
           ORDER BY created_at DESC""",
        memory_id,
    )
    incoming = await db.fetch(
        """SELECT id, source_id, relation_type, created_at
           FROM memory_relations WHERE target_id = $1::uuid
           ORDER BY created_at DESC""",
        memory_id,
    )
    return {
        "memory_id": memory_id,
        "outgoing": [dict(r) for r in outgoing],
        "incoming": [dict(r) for r in incoming],
    }


@router.delete("/{relation_id}", summary="Delete a relation")
async def delete_relation(
    memory_id: str,
    relation_id: str,
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Delete a relation owned by this memory."""
    result = await db.execute(
        "DELETE FROM memory_relations WHERE id = $1 AND (source_id = $2::uuid OR target_id = $2::uuid)",
        relation_id, memory_id,
    )
    if "0" in result:
        raise HTTPException(404, "Relation not found")
    return {"status": "ok", "deleted": relation_id}


@router.get("/graph", summary="Traverse relation graph up to depth levels")
async def get_graph(
    memory_id: str,
    depth: int = Query(2, ge=1, le=5),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """BFS traversal of the relation graph from a starting memory."""
    nodes: set[str] = {memory_id}
    edges: list[dict] = []
    current: set[str] = {memory_id}

    for _ in range(depth):
        if not current:
            break
        ids = list(current)
        placeholders = ",".join(f"${i+1}" for i in range(len(ids)))
        rows = await db.fetch(
            f"""SELECT id, source_id, target_id, relation_type
                FROM memory_relations
                WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})""",
            *ids,
        )
        current = set()
        for r in rows:
            edges.append({
                "id": str(r["id"]),
                "source": str(r["source_id"]),
                "target": str(r["target_id"]),
                "type": r["relation_type"],
            })
            current.add(str(r["source_id"]))
            current.add(str(r["target_id"]))
        nodes.update(current)
        current -= nodes

    if nodes:
        ph = ",".join(f"${i+1}" for i in range(len(nodes)))
        mems = await db.fetch(
            f"""SELECT id, content, memory_type, importance
                FROM memories WHERE id IN ({ph})""",
            *list(nodes),
        )
        nodes_data = {
            str(r["id"]): {
                "id": str(r["id"]),
                "content": r["content"][:150],
                "type": r["memory_type"],
                "importance": r["importance"],
            }
            for r in mems
        }
    else:
        nodes_data = {}

    return {"nodes": nodes_data, "edges": edges}
