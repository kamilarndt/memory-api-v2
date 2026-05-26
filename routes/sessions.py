"""Sessions — conversation session management."""

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
router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", summary="Create a new session")
async def create_session(
    agent_id: str = Query(..., description="Agent identifier"),
    project_id: str = Query("", description="Optional project identifier"),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Create a new active session."""
    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    await db.execute(
        "INSERT INTO memory_sessions (id, agent_id, project_id, started_at) VALUES ($1, $2, $3, $4)",
        sid, agent_id, project_id, now,
    )
    return {
        "status": "active",
        "session": {
            "id": sid,
            "agent_id": agent_id,
            "project_id": project_id,
            "started_at": now.isoformat(),
        },
    }


@router.post("/{id}/end", summary="End a session")
async def end_session(
    id: str,
    summary: str = Query("", description="Session summary"),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """End a session with an optional summary."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        "UPDATE memory_sessions SET status = 'ended', ended_at = $1, summary = $2 WHERE id = $3::uuid AND status = 'active'",
        now, summary, id,
    )
    if "0" in result:
        raise HTTPException(404, "Session not found or already ended")
    return {"status": "ended", "ended_at": now.isoformat(), "session_id": id}


@router.get("/{id}", summary="Get session details")
async def get_session(
    id: str,
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Get session details with observation count."""
    row = await db.fetchrow(
        """SELECT s.*,
                  (SELECT COUNT(*) FROM memories WHERE session_id = s.id) AS observation_count
           FROM memory_sessions s WHERE s.id = $1::uuid""",
        id,
    )
    if not row:
        raise HTTPException(404, "Session not found")
    return {
        "id": str(row["id"]),
        "agent_id": row["agent_id"],
        "project_id": row["project_id"],
        "status": row["status"],
        "summary": row["summary"],
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
        "observation_count": row["observation_count"],
    }


@router.get("/{id}/timeline", summary="Get session timeline")
async def get_timeline(
    id: str,
    before: int = Query(5, ge=0, le=100, description="Observations before focus"),
    after: int = Query(5, ge=0, le=100, description="Observations after focus"),
    focus_id: Optional[str] = Query(None, description="Focus memory ID for centering"),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Get chronological observation timeline for a session, optionally centered around a focus ID."""
    # Verify session exists
    session = await db.fetchval("SELECT id FROM memory_sessions WHERE id = $1::uuid", id)
    if not session:
        raise HTTPException(404, "Session not found")

    if focus_id:
        # Center around a specific memory
        focus_time = await db.fetchval(
            "SELECT created_at FROM memories WHERE id = $1::uuid", focus_id,
        )
        if not focus_time:
            raise HTTPException(404, "Focus memory not found in session")

        before_rows = await db.fetch(
            """SELECT id, content, memory_type, importance, created_at
               FROM memories
               WHERE session_id = $1::uuid AND created_at < $2
               ORDER BY created_at DESC
               LIMIT $3""",
            id, focus_time, before,
        )
        after_rows = await db.fetch(
            """SELECT id, content, memory_type, importance, created_at
               FROM memories
               WHERE session_id = $1::uuid AND created_at >= $2
               ORDER BY created_at ASC
               LIMIT $3""",
            id, focus_time, after,
        )

        focus_row = await db.fetchrow(
            "SELECT id, content, memory_type, importance, created_at FROM memories WHERE id = $1::uuid",
            focus_id,
        )

        timeline = (
            [dict(r) for r in reversed(before_rows)]
            + ([dict(focus_row)] if focus_row else [])
            + [dict(r) for r in after_rows]
        )
    else:
        # Return most recent observations
        rows = await db.fetch(
            """SELECT id, content, memory_type, importance, created_at
               FROM memories
               WHERE session_id = $1::uuid
               ORDER BY created_at DESC
               LIMIT $2""",
            id, before + after + 1,
        )
        timeline = [dict(r) for r in reversed(rows)]

    # Convert UUID/datetime to strings
    for entry in timeline:
        entry["id"] = str(entry["id"])
        entry["created_at"] = entry["created_at"].isoformat() if entry["created_at"] else None

    return {
        "session_id": id,
        "observations": timeline,
        "count": len(timeline),
    }


@router.get("", summary="List sessions")
async def list_sessions(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    project_id: Optional[str] = Query(None, description="Filter by project"),
    agent_id: Optional[str] = Query(None, description="Filter by agent"),
    status: Optional[str] = Query(None, description="Filter by status"),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """List sessions with pagination and optional filters."""
    conditions = []
    params: list = []
    idx = 1

    if project_id:
        conditions.append(f"project_id = ${idx}::text")
        params.append(project_id)
        idx += 1
    if agent_id:
        conditions.append(f"agent_id = ${idx}::text")
        params.append(agent_id)
        idx += 1
    if status:
        conditions.append(f"status = ${idx}::text")
        params.append(status)
        idx += 1

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    count_sql = f"SELECT COUNT(*) FROM memory_sessions {where}"
    total = await db.fetchval(count_sql, *params)

    data_sql = f"""
        SELECT id, agent_id, project_id, status, summary, started_at, ended_at
        FROM memory_sessions {where}
        ORDER BY started_at DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """
    params.append(limit)
    params.append(offset)
    rows = await db.fetch(data_sql, *params)

    return {
        "sessions": [
            {
                "id": str(r["id"]),
                "agent_id": r["agent_id"],
                "project_id": r["project_id"],
                "status": r["status"],
                "summary": r["summary"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
            }
            for r in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.delete("/{id}", summary="Delete a session")
async def delete_session(
    id: str,
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Delete a session. Refuses if it has observations."""
    obs_count = await db.fetchval(
        "SELECT COUNT(*) FROM memories WHERE session_id = $1::uuid", id,
    )
    if obs_count and obs_count > 0:
        raise HTTPException(
            409,
            f"Cannot delete session {id}: has {obs_count} observations. Archive memories first.",
        )
    result = await db.execute("DELETE FROM memory_sessions WHERE id = $1::uuid", id)
    if "0" in result:
        raise HTTPException(404, "Session not found")
    return {"status": "ok", "deleted": id}