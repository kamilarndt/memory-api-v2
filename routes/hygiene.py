"""Memory API v2 — Hygiene routes: feedback, decay, contradictions, entity resolution, purge."""

from __future__ import annotations

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Body, Query

from core import verify_token
from db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["hygiene"])


# ── Feedback ─────────────────────────────────────────────────────────────────

@router.post("/memories/{memory_id}/feedback", summary="Record feedback and update trust_score")
async def add_feedback(
    memory_id: str,
    agent_id: str = Body(..., embed=True),
    feedback_type: str = Body(..., embed=True),
    comment: str = Body(default="", embed=True),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Record feedback on a memory and recalculate its trust_score from the last 10 entries.

    Deltas baked into scoring:
      helpful     -> 1.0
      not_helpful -> 0.5
      incorrect   -> 0.0
      outdated    -> 0.3
    """
    allowed = {"helpful", "not_helpful", "incorrect", "outdated"}
    if feedback_type not in allowed:
        raise HTTPException(400, f"feedback_type must be one of {allowed}")

    # Verify memory exists
    mem = await db.fetchrow("SELECT id FROM memories WHERE id = $1", memory_id)
    if not mem:
        raise HTTPException(404, "Memory not found")

    await db.execute(
        "INSERT INTO memory_feedback (memory_id, agent_id, feedback_type, comment, created_at) "
        "VALUES ($1, $2, $3, $4, NOW())",
        memory_id, agent_id, feedback_type, comment,
    )

    # Recalculate trust from the last 10 feedback entries
    row = await db.fetchrow(
        """
        SELECT AVG(CASE
            WHEN feedback_type = 'helpful'     THEN 1.0
            WHEN feedback_type = 'not_helpful' THEN 0.5
            WHEN feedback_type = 'incorrect'   THEN 0.0
            WHEN feedback_type = 'outdated'    THEN 0.3
            ELSE 0.5
        END) as avg_score
        FROM (
            SELECT feedback_type
            FROM memory_feedback
            WHERE memory_id = $1
            ORDER BY created_at DESC
            LIMIT 10
        ) sub
        """,
        memory_id,
    )

    if row and row["avg_score"] is not None:
        new_trust = float(row["avg_score"])
        # Clamp to [0, 1]
        new_trust = max(0.0, min(1.0, new_trust))
        await db.execute(
            "UPDATE memories SET trust_score = $1, updated_at = NOW() WHERE id = $2",
            new_trust, memory_id,
        )
    else:
        new_trust = None

    return {
        "status": "feedback_recorded",
        "memory_id": memory_id,
        "trust_score": new_trust,
    }


# ── Temporal Decay ───────────────────────────────────────────────────────────

@router.post("/memories/decay", summary="Apply temporal decay to trust scores")
async def run_temporal_decay(
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Decay trust_score based on age using a 1-year half-life.

    trust_score *= 0.5^(age_in_years)
    Only touches non-archived memories with trust_score < 0.8.
    """
    result = await db.execute(
        """
        UPDATE memories SET
            trust_score = GREATEST(0,
                trust_score * POWER(
                    0.5,
                    EXTRACT(EPOCH FROM (NOW() - created_at)) / 31536000.0
                )
            ),
            updated_at = NOW()
        WHERE archived_at IS NULL
          AND trust_score IS NOT NULL
          AND trust_score < 0.8
        """
    )
    count = int(result.split()[-1]) if result.startswith("UPDATE") else 0
    logger.info("Temporal decay: %d memories updated", count)
    return {"status": "ok", "updated_count": count}


# ── Contradiction Detection ─────────────────────────────────────────────────

@router.post("/hygiene/run", summary="Run contradiction detection across memories")
async def run_hygiene(
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Find contradictory memories: shared entities + low content similarity.

    Flags both memories in each contradictory pair.
    """
    rows = await db.fetch(
        """
        SELECT m1.id        AS id1,
               m2.id        AS id2,
               m1.content   AS c1,
               m2.content   AS c2,
               m1.trust_score AS t1,
               m2.trust_score AS t2,
               m1.entities  AS e1,
               m2.entities  AS e2,
               (m1.embedding <=> m2.embedding) AS vec_dist
        FROM memories m1
        JOIN memories m2 ON m1.id < m2.id
        WHERE m1.archived_at IS NULL
          AND m2.archived_at IS NULL
          AND m1.trust_score > 0.5
          AND m2.trust_score > 0.5
          AND m1.entities IS NOT NULL
          AND m2.entities IS NOT NULL
          AND m1.entities::jsonb ?& array(SELECT jsonb_array_elements_text(m2.entities::jsonb))
          AND (1 - (m1.embedding <=> m2.embedding)) < 0.3
        LIMIT 100
        """
    )

    contradictions: list[dict] = []
    for r in rows:
        pair = {
            "memory_a": str(r["id1"]),
            "memory_b": str(r["id2"]),
            "trust_a": float(r["t1"]) if r["t1"] else None,
            "trust_b": float(r["t2"]) if r["t2"] else None,
            "vector_distance": round(float(r["vec_dist"]), 4),
        }
        contradictions.append(pair)

        # Flag both sides
        await db.execute(
            "UPDATE memories "
            "SET contradictions = COALESCE(contradictions, '[]'::jsonb) || $1::jsonb "
            "WHERE id = $2",
            f'["{r["id2"]}"]', str(r["id1"]),
        )
        await db.execute(
            "UPDATE memories "
            "SET contradictions = COALESCE(contradictions, '[]'::jsonb) || $1::jsonb "
            "WHERE id = $2",
            f'["{r["id1"]}"]', str(r["id2"]),
        )

    logger.info("Contradiction detection: %d pairs found", len(contradictions))
    return {
        "status": "ok",
        "contradictions_found": len(contradictions),
        "pairs": contradictions,
    }


# ── Entity Resolution ────────────────────────────────────────────────────────

@router.post("/entities/resolve", summary="Merge duplicate entities with similar names")
async def resolve_entities(
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Identify and merge entities whose names are identical (case-folded) or
    have high pg_trgm similarity (>0.8).  Keeps the lower entity_id, migrates
    all fact_entity_links, and deletes the duplicate.
    """
    rows = await db.fetch(
        """
        SELECT e1.entity_id AS id1, e2.entity_id AS id2,
               e1.name AS n1, e2.name AS n2
        FROM entities e1
        JOIN entities e2 ON e1.entity_id < e2.entity_id
        WHERE LOWER(e1.name) = LOWER(e2.name)
           OR similarity(e1.name, e2.name) > 0.8
        LIMIT 50
        """
    )

    merged = 0
    for r in rows:
        # Migrate links from id2 -> id1
        await db.execute(
            "UPDATE fact_entity_links SET entity_id = $1 WHERE entity_id = $2",
            r["id1"], r["id2"],
        )
        await db.execute("DELETE FROM entities WHERE entity_id = $1", r["id2"])
        merged += 1

    logger.info("Entity resolution: %d entities merged", merged)
    return {"status": "ok", "resolved": len(rows), "merged": merged}


# ── Purge ────────────────────────────────────────────────────────────────────

@router.delete("/memories/purge", summary="Archive low-trust memories and delete old archived")
async def purge_memories(
    max_trust: float = Query(default=0.2, description="Archive memories below this trust score"),
    min_age_days: int = Query(default=90, description="Only archive memories older than N days"),
    archived_before_days: int = Query(default=30, description="Permanently delete memories archived before N days ago"),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Two-phase cleanup:

    1. Archive low-trust, old memories (or zero-access memories older than 180 days).
    2. Permanently delete memories that have been archived longer than N days.
    """
    # Phase 1: Archive
    r1 = await db.execute(
        """
        UPDATE memories
        SET archived_at = NOW(), updated_at = NOW()
        WHERE archived_at IS NULL
          AND (
            (trust_score IS NOT NULL
             AND trust_score < $1
             AND created_at < NOW() - ($2 || ' days')::interval)
            OR
            (access_count = 0
             AND created_at < NOW() - 180 * interval '1 day')
          )
        """,
        max_trust, min_age_days,
    )
    archived = int(r1.split()[-1]) if r1.startswith("UPDATE") else 0

    # Phase 2: Permanently delete old archived
    r2 = await db.execute(
        """
        DELETE FROM memories
        WHERE archived_at IS NOT NULL
          AND archived_at < NOW() - ($1 || ' days')::interval
        """,
        archived_before_days,
    )
    deleted = int(r2.split()[-1]) if r2.startswith("DELETE") else 0

    logger.info("Purge: %d archived, %d permanently deleted", archived, deleted)
    return {"status": "ok", "archived": archived, "deleted": deleted}
