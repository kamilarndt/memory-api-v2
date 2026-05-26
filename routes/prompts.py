"""Prompts — prompt storage and search."""

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
router = APIRouter(prefix="/prompts", tags=["prompts"])


@router.post("", summary="Save a prompt")
async def save_prompt(
    content: str = Query(..., description="Prompt content"),
    session_id: Optional[str] = Query(None, description="Session ID"),
    agent_id: str = Query(..., description="Agent identifier"),
    project_id: str = Query("", description="Optional project identifier"),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Save a prompt to the database."""
    if session_id:
        sess = await db.fetchval("SELECT id FROM memory_sessions WHERE id = $1::uuid", session_id)
        if not sess:
            raise HTTPException(404, "Session not found")

    pid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    await db.execute(
        "INSERT INTO memory_prompts (id, session_id, agent_id, project_id, content, created_at) VALUES ($1, $2::uuid, $3, $4, $5, $6)",
        pid, session_id, agent_id, project_id, content, now,
    )
    return {
        "status": "ok",
        "prompt": {
            "id": pid,
            "session_id": session_id,
            "agent_id": agent_id,
            "project_id": project_id,
            "content": content,
            "created_at": now.isoformat(),
        },
    }


@router.get("/search", summary="Search prompts by content")
async def search_prompts(
    q: str = Query(..., description="Search query"),
    limit: int = Query(20, ge=1, le=200),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Search prompts by content using ILIKE."""
    pattern = f"%{q}%"
    rows = await db.fetch(
        """SELECT id, session_id, agent_id, project_id, content, created_at
           FROM memory_prompts
           WHERE content ILIKE $1
           ORDER BY created_at DESC
           LIMIT $2""",
        pattern, limit,
    )
    return {
        "results": [
            {
                "id": str(r["id"]),
                "session_id": str(r["session_id"]) if r["session_id"] else None,
                "agent_id": r["agent_id"],
                "project_id": r["project_id"],
                "content": r["content"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "count": len(rows),
        "query": q,
    }


@router.get("/session/{session_id}", summary="List prompts for a session")
async def list_session_prompts(
    session_id: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """List all prompts belonging to a session."""
    sess = await db.fetchval("SELECT id FROM memory_sessions WHERE id = $1::uuid", session_id)
    if not sess:
        raise HTTPException(404, "Session not found")

    total = await db.fetchval(
        "SELECT COUNT(*) FROM memory_prompts WHERE session_id = $1::uuid", session_id,
    )
    rows = await db.fetch(
        """SELECT id, session_id, agent_id, project_id, content, created_at
           FROM memory_prompts
           WHERE session_id = $1::uuid
           ORDER BY created_at ASC
           LIMIT $2 OFFSET $3""",
        session_id, limit, offset,
    )
    return {
        "session_id": session_id,
        "prompts": [
            {
                "id": str(r["id"]),
                "agent_id": r["agent_id"],
                "project_id": r["project_id"],
                "content": r["content"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }