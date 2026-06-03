"""Conflict verdicts — contradiction detection between memories."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from core import verify_token
from db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/conflicts", tags=["conflicts"])


@router.get("", summary="List conflict verdicts")
async def list_verdicts(
    project: Optional[str] = Query(None, description="Filter by project"),
    status: Optional[str] = Query(None, description="Filter by verdict type"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """List all conflict verdicts with pagination."""
    conditions = []
    params: list = []
    idx = 1

    if project:
        conditions.append(f"cv.project_id = ${idx}::text")
        params.append(project)
        idx += 1
    if status:
        conditions.append(f"cv.verdict = ${idx}::text")
        params.append(status)
        idx += 1

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    count_sql = f"SELECT COUNT(*) FROM memory_conflict_verdicts cv {where}"
    total = await db.fetchval(count_sql, *params)

    data_sql = f"""
        SELECT cv.*, s.project_id
        FROM memory_conflict_verdicts cv
        LEFT JOIN memory_sessions s ON cv.session_id = s.id
        {where}
        ORDER BY cv.created_at DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """
    params.append(limit)
    params.append(offset)
    rows = await db.fetch(data_sql, *params)

    return {
        "verdicts": [
            {
                "id": str(r["id"]),
                "source_id": str(r["source_id"]),
                "target_id": str(r["target_id"]),
                "verdict": r["verdict"],
                "reason": r["reason"],
                "evidence": r["evidence"],
                "confidence": r["confidence"],
                "model": r["model"],
                "session_id": str(r["session_id"]) if r["session_id"] else None,
                "project_id": r["project_id"] if "project_id" in r else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/judge", summary="Record an agent verdict")
async def judge_verdict(
    source_id: str = Query(..., description="Source memory ID"),
    target_id: str = Query(..., description="Target memory ID"),
    verdict: str = Query(..., description="One of: contradictory, supportive, independent, superseded"),
    reason: str = Query("", description="Reason for the verdict"),
    evidence: str = Query("", description="Supporting evidence"),
    confidence: float = Query(0.5, ge=0.0, le=1.0, description="Confidence score 0-1"),
    model: str = Query("", description="Model used for judgment"),
    session_id: Optional[str] = Query(None, description="Optional session ID"),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Record a conflict verdict between two memories."""
    VALID_VERDICTS = {"contradictory", "supportive", "independent", "superseded"}
    if verdict not in VALID_VERDICTS:
        raise HTTPException(400, f"Invalid verdict. Valid: {', '.join(sorted(VALID_VERDICTS))}")
    
    # Validate UUID format before DB queries
    for name, val in [("source_id", source_id), ("target_id", target_id)]:
        if val is None:
            raise HTTPException(400, f"Invalid UUID for {name}: None (Python None)")
        if val == "None":
            raise HTTPException(400, f"Invalid UUID for {name}: 'None' (string)")
        if not val:
            raise HTTPException(400, f"Invalid UUID for {name}: empty")
        try:
            uuid.UUID(str(val))
        except (ValueError, AttributeError) as e:
            raise HTTPException(400, f"Invalid UUID for {name}: '{val}' - {e}")

    try:
        source = await db.fetchval("SELECT id FROM memories WHERE id = $1::uuid", source_id)
    except Exception as e:
        raise HTTPException(400, f"Source query failed: {e}")
    if not source:
        raise HTTPException(404, "Source memory not found")
    target = await db.fetchval("SELECT id FROM memories WHERE id = $1::uuid", target_id)
    if not target:
        raise HTTPException(404, "Target memory not found")

    if session_id:
        sess = await db.fetchval("SELECT id FROM memory_sessions WHERE id = $1::uuid", session_id)
        if not sess:
            raise HTTPException(404, "Session not found")

    # Check existing verdict
    existing = await db.fetchrow(
        "SELECT id, verdict FROM memory_conflict_verdicts WHERE source_id = $1::uuid AND target_id = $2::uuid",
        source_id, target_id,
    )
    if existing:
        raise HTTPException(
            409,
            f"Verdict already exists between these memories: {existing['verdict']} (id={existing['id']}). Use PUT to update.",
        )

    vid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    await db.execute(
        """INSERT INTO memory_conflict_verdicts (id, source_id, target_id, verdict, reason, evidence, confidence, model, session_id, created_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::uuid, $10)""",
        vid, source_id, target_id, verdict, reason, evidence, confidence, model,
        session_id, now,
    )
    return {
        "status": "ok",
        "verdict": {
            "id": vid,
            "source_id": source_id,
            "target_id": target_id,
            "verdict": verdict,
            "reason": reason,
            "evidence": evidence,
            "confidence": confidence,
            "model": model,
            "session_id": session_id,
            "created_at": now.isoformat(),
        },
    }


@router.post("/compare", summary="Compare two memories by ID")
async def compare_memories(
    id1: str = Query(..., description="First memory ID"),
    id2: str = Query(..., description="Second memory ID"),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Compare two observations by their IDs and return any existing verdict."""
    row1 = await db.fetchrow(
        "SELECT id, content, memory_type, importance, created_at FROM memories WHERE id = $1::uuid",
        id1,
    )
    if not row1:
        raise HTTPException(404, f"Memory {id1} not found")
    row2 = await db.fetchrow(
        "SELECT id, content, memory_type, importance, created_at FROM memories WHERE id = $1::uuid",
        id2,
    )
    if not row2:
        raise HTTPException(404, f"Memory {id2} not found")

    # Look up existing verdict in either direction
    verdict = await db.fetchrow(
        """SELECT id, source_id, target_id, verdict, reason, evidence, confidence, model, created_at
           FROM memory_conflict_verdicts
           WHERE (source_id = $1::uuid AND target_id = $2::uuid)
              OR (source_id = $2::uuid AND target_id = $1::uuid)
           ORDER BY created_at DESC
           LIMIT 1""",
        id1, id2,
    )

    analysis = {
        "memory_a": {
            "id": str(row1["id"]),
            "content": row1["content"],
            "type": row1["memory_type"],
            "importance": row1["importance"],
            "created_at": row1["created_at"].isoformat() if row1["created_at"] else None,
        },
        "memory_b": {
            "id": str(row2["id"]),
            "content": row2["content"],
            "type": row2["memory_type"],
            "importance": row2["importance"],
            "created_at": row2["created_at"].isoformat() if row2["created_at"] else None,
        },
    }

    if verdict:
        return {
            "analysis": analysis,
            "existing_verdict": {
                "id": str(verdict["id"]),
                "verdict": verdict["verdict"],
                "reason": verdict["reason"],
                "evidence": verdict["evidence"],
                "confidence": verdict["confidence"],
                "model": verdict["model"],
                "created_at": verdict["created_at"].isoformat() if verdict["created_at"] else None,
            },
        }
    else:
        return {
            "analysis": analysis,
            "existing_verdict": None,
            "note": "No verdict exists between these two memories. Use POST /conflicts/judge to create one.",
        }


@router.get("/stats", summary="Aggregate verdict stats")
async def verdict_stats(
    project: Optional[str] = Query(None, description="Filter by project"),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Aggregate counts by verdict type."""
    if project:
        rows = await db.fetch(
            """SELECT cv.verdict, COUNT(*) as count
               FROM memory_conflict_verdicts cv
               LEFT JOIN memory_sessions s ON cv.session_id = s.id
               WHERE s.project_id = $1::text
               GROUP BY cv.verdict
               ORDER BY count DESC""",
            project,
        )
    else:
        rows = await db.fetch(
            """SELECT verdict, COUNT(*) as count
               FROM memory_conflict_verdicts
               GROUP BY verdict
               ORDER BY count DESC"""
        )

    total = sum(r["count"] for r in rows)
    breakdown = {r["verdict"]: r["count"] for r in rows}
    return {
        "total": total,
        "breakdown": breakdown,
        "project": project,
    }


@router.post("/scan", summary="Scan project for conflict candidates")
async def scan_conflicts(
    project: Optional[str] = Query(None, description="Project to scan"),
    limit: int = Query(10, ge=1, le=100, description="Max candidate pairs"),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Scan a project for memory pairs that might be in conflict.

    Pairs are selected by: same project (via session), matching memory types,
    and not already having a verdict.
    """
    if project:
        # Get memories from sessions of this project
        mem_rows = await db.fetch(
            """SELECT m.id, m.content, m.memory_type, m.importance, m.created_at
               FROM memories m
               JOIN memory_sessions s ON m.session_id = s.id
               WHERE s.project_id = $1::text
                 AND m.archived_at IS NULL
               ORDER BY m.created_at DESC
               LIMIT $2""",
            project, limit * 3,
        )
    else:
        mem_rows = await db.fetch(
            """SELECT m.id, m.content, m.memory_type, m.importance, m.created_at
               FROM memories m
               WHERE m.archived_at IS NULL
               ORDER BY m.created_at DESC
               LIMIT $1""",
            limit * 3,
        )

    memories = [
        {
            "id": str(r["id"]),
            "content": r["content"],
            "type": r["memory_type"],
            "importance": r["importance"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in mem_rows
    ]

    # Generate candidate pairs that don't already have a verdict
    candidates = []
    seen_pairs: set[tuple[str, str]] = set()

    for i in range(len(memories)):
        for j in range(i + 1, len(memories)):
            if len(candidates) >= limit:
                break
            a = memories[i]
            b = memories[j]
            pair_key = tuple(sorted([a["id"], b["id"]]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            # Check if verdict exists in either direction
            existing = await db.fetchval(
                """SELECT 1 FROM memory_conflict_verdicts
                   WHERE (source_id = $1::uuid AND target_id = $2::uuid)
                      OR (source_id = $2::uuid AND target_id = $1::uuid)""",
                a["id"], b["id"],
            )
            if existing:
                continue

            candidates.append({
                "memory_a": a,
                "memory_b": b,
            })

    return {
        "candidates": candidates,
        "count": len(candidates),
        "project": project,
    }