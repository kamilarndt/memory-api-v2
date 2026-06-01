"""
DreamService — background memory consolidation orchestrator.

Stages (run in order via POST /api/dream/run):
  1. LINK       — Find similar facts (cosine > 0.92) per agent
  2. CONSOLIDATE — LLM merges duplicate/scattered facts
  3. REFLECT    — LLM generates high-level patterns and insights
  4. DECAY      — Boost recall on hot facts, weaken cold ones
  5. BUDGET     — Prune excess facts per agent (default max: 500)

Usage:
    POST /api/dream/run     → run full cycle, returns stats
    GET  /api/dream/status  → current state + last run stats
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import httpx

from core import get_config, get_embedding
from repositories.memory import MemoryRepository
from repositories.prompts import MERGE_FACTS_SYSTEM, REFLECT_SYSTEM

logger = logging.getLogger(__name__)


# ── State ────────────────────────────────────────────────────────────────────

@dataclass
class DreamState:
    """Current dream cycle state — observable via GET /api/dream/status."""
    running: bool = False
    stage: str = ""
    progress: float = 0.0
    started_at: Optional[str] = None
    last_completed: Optional[str] = None
    stats: dict = field(default_factory=dict)


_state = DreamState()


def get_dream_state() -> DreamState:
    """Return current DreamState (thread-safe singleton)."""
    return _state


# ── LLM helpers ─────────────────────────────────────────────────────────────

async def _llm_merge(content1: str, content2: str, config) -> Optional[str]:
    """Call OpenRouter to merge two similar facts into one."""
    if not config.openrouter_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.openrouter_key}",
                    "HTTP-Referer": "http://localhost:8766",
                    "X-Title": "Memory API v2",
                },
                json={
                    "model": "openai/gpt-4o-mini:free",
                    "messages": [
                        {
                            "role": "system",
                            "content": MERGE_FACTS_SYSTEM,
                        },
                        {"role": "user", "content": f"Fact 1: {content1}\nFact 2: {content2}"},
                    ],
                    "max_tokens": 300,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning("LLM merge failed: %s", e)
        return None


async def _llm_reflect(contents: list[str], config) -> list[str]:
    """Call LLM to generate high-level reflections from a batch of facts."""
    if not config.openrouter_key or not contents:
        return []
    try:
        contents_str = "\n".join(f"- {c}" for c in contents[:20])
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.openrouter_key}",
                    "HTTP-Referer": "http://localhost:8766",
                    "X-Title": "Memory API v2",
                },
                json={
                    "model": "openai/gpt-4o-mini:free",
                    "messages": [
                        {
                            "role": "system",
                            "content": REFLECT_SYSTEM,
                        },
                        {"role": "user", "content": f"Memories:\n{contents_str}"},
                    ],
                    "max_tokens": 500,
                },
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
            # Try to parse as JSON
            if text.startswith("[") and text.endswith("]"):
                return json.loads(text)
            # Fallback: split by lines
            return [line.strip("- \"'") for line in text.split("\n") if line.strip()]
    except Exception as e:
        logger.warning("LLM reflect failed: %s", e)
        return []


# ── Stage 1: LINK ───────────────────────────────────────────────────────────

async def stage_link(db: asyncpg.Connection, max_pairs: int = 100) -> int:
    """LINK: Find similar facts (cosine > 0.92) grouped by agent_id."""
    rows = await db.fetch(
        """
        SELECT m1.id AS id1, m2.id AS id2,
               m1.content AS c1, m2.content AS c2,
               m1.agent_id AS agent,
               (1 - (m1.embedding <=> m2.embedding))::double precision AS similarity
        FROM memories m1
        JOIN memories m2 ON m1.id < m2.id
        WHERE m1.archived_at IS NULL
          AND m2.archived_at IS NULL
          AND m1.agent_id = m2.agent_id
          AND m1.embedding IS NOT NULL
          AND m2.embedding IS NOT NULL
          AND (1 - (m1.embedding <=> m2.embedding)) > 0.92
        ORDER BY similarity DESC
        LIMIT $1
        """,
        max_pairs,
    )

    pairs = []
    for r in rows:
        pairs.append({
            "id1": str(r["id1"]),
            "id2": str(r["id2"]),
            "c1": r["c1"],
            "c2": r["c2"],
            "agent": r["agent"],
            "similarity": float(r["similarity"]),
        })

    # Store pairs in state for subsequent stages
    _state.stats["linked_pairs"] = pairs
    logger.info("Dream LINK: %d similar pairs found", len(pairs))
    return len(pairs)


# ── Stage 2: CONSOLIDATE ────────────────────────────────────────────────────

async def stage_consolidate(db: asyncpg.Connection, max_merges: int = 20) -> int:
    """CONSOLIDATE: LLM merges duplicate/scattered facts into one."""
    config = get_config()
    pairs = _state.stats.get("linked_pairs", [])
    if not pairs:
        return 0

    consolidated = 0
    for pair in pairs[:max_merges]:
        merged = await _llm_merge(pair["c1"], pair["c2"], config)
        if merged and len(merged) > 10:
            # Re-embed the merged content
            emb = await get_embedding(merged, config)
            if emb:
                es = MemoryRepository.format_vector(emb)
                # Update first memory with merged content + new embedding
                await db.execute(
                    """UPDATE memories
                       SET content = $1, embedding = $2::vector, updated_at = NOW()
                       WHERE id = $3""",
                    merged, es, pair["id1"],
                )
                # Archive the duplicate
                await db.execute(
                    "UPDATE memories SET archived_at = NOW() WHERE id = $1",
                    pair["id2"],
                )
                consolidated += 1

    logger.info("Dream CONSOLIDATE: %d facts merged", consolidated)
    return consolidated


# ── Stage 3: REFLECT ────────────────────────────────────────────────────────

async def stage_reflect(db: asyncpg.Connection, max_reflections: int = 5) -> int:
    """REFLECT: LLM generates high-level patterns from facts per agent."""
    config = get_config()

    # Find agents with enough active facts to reflect upon
    agents = await db.fetch(
        """
        SELECT agent_id, COUNT(*) AS cnt
        FROM memories
        WHERE archived_at IS NULL
        GROUP BY agent_id
        HAVING COUNT(*) > 10
        ORDER BY cnt DESC
        LIMIT $1
        """,
        max_reflections,
    )

    if not agents:
        return 0

    reflections_created = 0
    for row in agents:
        agent_id = str(row["agent_id"])
        recent = await db.fetch(
            """SELECT content FROM memories
               WHERE agent_id = $1 AND archived_at IS NULL
               ORDER BY created_at DESC LIMIT 20""",
            agent_id,
        )
        if not recent:
            continue

        contents = [r["content"] for r in recent]
        insights = await _llm_reflect(contents, config)

        for insight in insights:
            if insight and len(insight) > 10:
                emb = await get_embedding(insight, config)
                if emb:
                    now = datetime.now(timezone.utc).isoformat()
                    es = MemoryRepository.format_vector(emb)
                    await db.execute(
                        """INSERT INTO memories
                           (id, agent_id, content, embedding, memory_type, importance, created_at, updated_at)
                           VALUES ($1, $2, $3, $4::vector, 'reflection', 0.9, $5, $5)""",
                        f"ref-{now.replace(':', '-')}-{agent_id}",
                        agent_id,
                        insight,
                        es,
                        now,
                    )
                    reflections_created += 1

    logger.info("Dream REFLECT: %d reflections created", reflections_created)
    return reflections_created


# ── Stage 4: DECAY ─────────────────────────────────────────────────────────

async def stage_decay(db: asyncpg.Connection) -> int:
    """DECAY: Boost frequently-recalled facts, weaken cold ones."""
    # Boost: frequently recalled → trust_score += 0.1
    boosted = await db.execute(
        """
        UPDATE memories
        SET trust_score = LEAST(1.0, COALESCE(trust_score, 0.5) + 0.1),
            updated_at = NOW()
        WHERE archived_at IS NULL
          AND access_count IS NOT NULL
          AND access_count > 10
          AND COALESCE(trust_score, 0.5) < 1.0
        """
    )

    # Decay: never recalled + old → trust_score -= 0.1
    decayed = await db.execute(
        """
        UPDATE memories
        SET trust_score = GREATEST(0, COALESCE(trust_score, 0.5) - 0.1),
            updated_at = NOW()
        WHERE archived_at IS NULL
          AND (access_count IS NULL OR access_count = 0)
          AND created_at < NOW() - INTERVAL '30 days'
          AND COALESCE(trust_score, 0.5) > 0
        """
    )

    # Reset access_count for next cycle (prevent unbounded boost)
    await db.execute("UPDATE memories SET access_count = 0 WHERE access_count > 0")

    # Parse row counts from SQL status messages
    def _parse_count(result: str) -> int:
        # PG returns "UPDATE <count>" or "<count>" as string
        if result.startswith("UPDATE"):
            parts = result.split()
            return int(parts[-1]) if len(parts) > 1 else 0
        try:
            return int(result)
        except (ValueError, TypeError):
            return 0

    total = _parse_count(boosted) + _parse_count(decayed)
    logger.info("Dream DECAY: %d memories affected (boosted=%s, decayed=%s)",
                total, _parse_count(boosted), _parse_count(decayed))
    return total


# ── Stage 5: BUDGET ────────────────────────────────────────────────────────

async def stage_budget(db: asyncpg.Connection, max_per_agent: int = 500) -> int:
    """BUDGET: Prune excess facts per agent when over limit."""
    agents = await db.fetch(
        """SELECT agent_id, COUNT(*) AS cnt
           FROM memories
           WHERE archived_at IS NULL
           GROUP BY agent_id
           HAVING COUNT(*) > $1""",
        max_per_agent,
    )

    trimmed = 0
    for row in agents:
        agent_id = str(row["agent_id"])
        excess = int(row["cnt"]) - max_per_agent
        # Archive oldest, lowest importance, lowest trust first
        await db.execute(
            """WITH to_archive AS (
                 SELECT id FROM memories
                 WHERE agent_id = $1 AND archived_at IS NULL
                 ORDER BY COALESCE(importance, 0) ASC,
                          COALESCE(trust_score, 0) ASC,
                          created_at ASC
                 LIMIT $2
               )
               UPDATE memories SET archived_at = NOW()
               WHERE id IN (SELECT id FROM to_archive)""",
            agent_id, excess,
        )
        trimmed += excess

    # Also archive any expired memories (expires_at < NOW())
    expired = await db.execute(
        "UPDATE memories SET archived_at = NOW() WHERE expires_at IS NOT NULL AND expires_at < NOW() AND archived_at IS NULL"
    )
    # Extract count from result string e.g. "UPDATE 3"
    expired_count = int(expired.split()[-1]) if expired and expired.startswith("UPDATE") else 0
    if expired_count > 0:
        logger.info("Dream BUDGET: %d expired memories archived", expired_count)

    trimmed += expired_count
    logger.info("Dream BUDGET: %d excess memories archived (incl. %d expired)", trimmed, expired_count)
    return trimmed


# ── Orchestrator ────────────────────────────────────────────────────────────

async def run_full_cycle(db: asyncpg.Connection) -> dict:
    """Run all 5 dream stages in order. Returns summary stats."""
    if _state.running:
        return {"status": "already_running"}

    _state.running = True
    _state.started_at = datetime.now(timezone.utc).isoformat()
    _state.stats = {}

    try:
        # Stage 1: LINK — find similar pairs
        _state.stage = "link"
        _state.progress = 0.0
        link_count = await stage_link(db)
        _state.stats["link"] = link_count
        logger.info("Dream cycle: LINK done (%d pairs)", link_count)

        # Stage 2: CONSOLIDATE — merge duplicates
        _state.stage = "consolidate"
        _state.progress = 0.25
        consolidated = await stage_consolidate(db)
        _state.stats["consolidated"] = consolidated
        logger.info("Dream cycle: CONSOLIDATE done (%d merged)", consolidated)

        # Stage 3: REFLECT — generate insights
        _state.stage = "reflect"
        _state.progress = 0.50
        reflections = await stage_reflect(db)
        _state.stats["reflections"] = reflections
        logger.info("Dream cycle: REFLECT done (%d created)", reflections)

        # Stage 4: DECAY — score adjustments
        _state.stage = "decay"
        _state.progress = 0.75
        decayed = await stage_decay(db)
        _state.stats["decayed"] = decayed
        logger.info("Dream cycle: DECAY done (%d affected)", decayed)

        # Stage 5: BUDGET — prune excess
        _state.stage = "budget"
        _state.progress = 0.90
        budget = await stage_budget(db)
        _state.stats["budget_archived"] = budget
        logger.info("Dream cycle: BUDGET done (%d archived)", budget)

        # Stage 6: EXPIRE — archive expired memories
        _state.stage = "expire"
        _state.progress = 0.95

        expire_result = await db.execute(
            "UPDATE memories SET archived_at = NOW() WHERE expires_at IS NOT NULL AND expires_at < NOW() AND archived_at IS NULL"
        )
        expired = 0
        if expire_result and expire_result.startswith("UPDATE"):
            parts = expire_result.split()
            if len(parts) > 1:
                try:
                    expired = int(parts[-1])
                except (ValueError, TypeError):
                    pass
        _state.stats["expired"] = expired
        if expired:
            logger.info("Dream EXPIRE: %d memories archived", expired)

        _state.stage = "complete"
        _state.progress = 1.0
        _state.last_completed = datetime.now(timezone.utc).isoformat()

        return {"status": "completed", "stats": _state.stats}

    except Exception as e:
        logger.exception("Dream cycle failed at stage %s", _state.stage)
        _state.stage = f"error:{_state.stage}"
        return {"status": "failed", "stage": _state.stage, "error": str(e)}
    finally:
        _state.running = False
