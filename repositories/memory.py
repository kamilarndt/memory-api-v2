"""Memory API v2 — Repository layer for database operations.

Encapsulates all direct PostgreSQL access. Router handlers and MCP
tools call repository methods instead of executing raw SQL. This
centralises schema changes, vector formatting, and dedup logic.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)


class MemoryRepository:
    """Thin repository over an asyncpg connection.

    Every method accepts a connection so callers (FastAPI dependency,
    pool.acquire, etc.) retain full control over transaction boundaries.
    """

# ── Vector helpers ────────────────────────────────────────────────────────

    @staticmethod
    def format_vector(emb: list[float]) -> str:
        """Standardises vector formatting for pgvector literal."""
        return f"[{','.join(str(v) for v in emb)}]"

    # ── Dedup / lookup ────────────────────────────────────────────────────────

    async def check_semantic_duplicate(
        self,
        conn: asyncpg.Connection,
        emb: list[float],
        threshold: float = 0.95,
    ) -> Optional[str]:
        """Return existing memory ID if a semantic duplicate exists."""
        es = self.format_vector(emb)
        row = await conn.fetchrow(
            "SELECT id FROM memories "
            "WHERE 1 - (embedding <=> $1::vector) > $2 AND archived_at IS NULL "
            "LIMIT 1",
            es, threshold,
        )
        return str(row["id"]) if row else None

    async def find_by_pi_key(
        self,
        conn: asyncpg.Connection,
        pi_memory_key: str,
        agent_id: str,
        project_id: str,
    ) -> Optional[str]:
        """Return existing memory ID if pi_memory_key match found (active)."""
        if not pi_memory_key:
            return None
        row = await conn.fetchrow(
            "SELECT id FROM memories "
            "WHERE pi_memory_key = $1 AND agent_id = $2 AND project_id = $3 "
            "AND archived_at IS NULL "
            "LIMIT 1",
            pi_memory_key, agent_id, project_id,
        )
        return str(row["id"]) if row else None

    # ── CRUD ──────────────────────────────────────────────────────────────────

    async def inline_upsert_memory(
        self,
        conn: asyncpg.Connection,
        memory_id: str,
        content: str,
        session_id: Optional[str] = None,
        expires_in_hours: Optional[int] = None,
    ) -> None:
        """Update content and metadata of an existing memory (smart inline dedup)."""
        await conn.execute(
            "UPDATE memories SET "
            "content = $1, "
            "session_id = COALESCE($2, session_id), "
            "updated_at = NOW(), "
            "access_count = COALESCE(access_count, 0) + 1 "
            "WHERE id = $3",
            content, session_id, memory_id,
        )
        if expires_in_hours is not None:
            await conn.execute(
                "UPDATE memories SET expires_at = NOW() + make_interval(hours => $1) "
                "WHERE id = $2",
                expires_in_hours, memory_id,
            )

    async def insert_memory(
        self,
        conn: asyncpg.Connection,
        mid: str,
        content: str,
        emb: list[float],
        agent_id: str,
        project_id: str,
        user_id: str,
        category: str,
        tags: list[str],
        importance: float,
        memory_type: str,
        pi_memory_key: str = "",
        session_id: Optional[str] = None,
        expires_in_hours: Optional[int] = None,
    ) -> None:
        """Insert a completely new memory fact into the database."""
        es = self.format_vector(emb)
        await conn.execute(
            "INSERT INTO memories "
            "(id, content, embedding, agent_id, project_id, user_id, category, tags, "
            " importance, memory_type, pi_memory_key, session_id, expires_at, created_at) "
            "VALUES ($1, $2, $3::vector, $4, $5, $6, $7, $8, $9, $10, $11, $12, "
            "        CASE WHEN $13::int IS NOT NULL "
            "             THEN NOW() + make_interval(hours => $13) ELSE NULL END, "
            "        NOW())",
            mid, content, es, agent_id, project_id,
            user_id, category, tags, importance, memory_type,
            pi_memory_key, session_id, expires_in_hours,
        )

    async def update_entities(
        self,
        conn: asyncpg.Connection,
        memory_id: str,
        entities: list[str],
    ) -> None:
        """Update the entities JSONB block for a specific memory."""
        await conn.execute(
            "UPDATE memories SET entities = $1::jsonb WHERE id = $2",
            json.dumps(entities), memory_id,
        )

    async def update_memory_fields(
        self,
        conn: asyncpg.Connection,
        memory_id: str,
        **fields,
    ) -> Optional[dict]:
        """Generic field update. Pass col=value pairs.

        Returns the updated row as a dict, or None if not found.
        """
        if not fields:
            return None
        set_clauses = []
        params = []
        i = 1
        for col, val in fields.items():
            if val is not None:
                set_clauses.append(f"{col} = ${i}")
                params.append(val)
                i += 1
        if not set_clauses:
            return None
        set_clauses.append("updated_at = NOW()")
        params.append(memory_id)
        row = await conn.fetchrow(
            f"UPDATE memories SET {', '.join(set_clauses)} "
            f"WHERE id = ${i} AND archived_at IS NULL "
            f"RETURNING id, content, category, agent_id, project_id, importance, memory_type",
            *params,
        )
        return dict(row) if row else None

    async def soft_delete(
        self,
        conn: asyncpg.Connection,
        memory_id: str,
    ) -> bool:
        """Archive a memory (soft delete). Returns True if affected."""
        result = await conn.execute(
            "UPDATE memories SET archived_at = NOW() "
            "WHERE id = $1 AND archived_at IS NULL",
            memory_id,
        )
        return result != "UPDATE 0"

    async def get_by_id(
        self,
        conn: asyncpg.Connection,
        memory_id: str,
    ) -> Optional[dict]:
        """Return full memory row (minus embedding/tsv)."""
        row = await conn.fetchrow(
            "SELECT * FROM memories WHERE id = $1", memory_id,
        )
        return dict(row) if row else None

    # ── Search ────────────────────────────────────────────────────────────────

    async def hybrid_search(
        self,
        conn: asyncpg.Connection,
        emb: list[float],
        query_text: str,
        agent_id: Optional[str] = None,
        project_id: Optional[str] = None,
        category: Optional[str] = None,
        cross_agent: bool = True,
        limit: int = 10,
    ) -> list[dict]:
        """Hybrid search: semantic (cosine) + keyword (ts_rank) with RRF fusion."""
        es = self.format_vector(emb)
        wheres = ["m.archived_at IS NULL"]
        params = []
        i = 1

        agent = None if cross_agent else agent_id
        for col, val in [("m.agent_id", agent),
                         ("m.project_id", project_id),
                         ("m.category", category)]:
            if val:
                wheres.append(f"{col} = ${i}")
                params.append(val)
                i += 1

        where = " AND ".join(wheres)
        rows = await conn.fetch(
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
                       ROW_NUMBER() OVER (
                           ORDER BY ts_rank(m.tsv, websearch_to_tsquery('english', ${i+1})) DESC
                       ) as rank
                FROM memories m
                WHERE (m.tsv @@ websearch_to_tsquery('english', ${i+1})
                       OR similarity(m.content, ${i+1}) > 0.3)
                  AND {where}
                LIMIT 50
            )
            SELECT m.id, m.content, m.category, m.agent_id, m.project_id, m.user_id,
                   m.tags, m.source_file, m.importance, m.trust_score, m.memory_type,
                   m.created_at, m.pi_memory_key,
                   (COALESCE(1.0 / (60 + s.rank), 0.0) +
                    COALESCE(1.0 / (60 + k.rank), 0.0)) as rrf_score
            FROM semantic_search s
            FULL OUTER JOIN keyword_search k ON s.id = k.id
            JOIN memories m ON m.id = COALESCE(s.id, k.id)
            ORDER BY rrf_score DESC
            LIMIT ${i+2}
            """,
            *(params + [es, query_text, limit]),
        )
        return [dict(r) for r in rows]

    async def search_by_pi_key(
        self,
        conn: asyncpg.Connection,
        emb: list[float],
        query_text: str,
        pi_memory_key: str = "",
        limit: int = 5,
    ) -> list[dict]:
        """Pi Agent search with optional pi_memory_key filter (wildcard support)."""
        es = self.format_vector(emb)
        where = "m.archived_at IS NULL AND m.embedding IS NOT NULL"
        params: list = []
        i = 1

        if pi_memory_key:
            if pi_memory_key.endswith(".*"):
                where += f" AND m.pi_memory_key LIKE ${i}"
                params.append(pi_memory_key.replace(".*", ".%"))
            else:
                where += f" AND m.pi_memory_key = ${i}"
                params.append(pi_memory_key)
            i += 1

        rows = await conn.fetch(
            f"""
            WITH semantic_raw AS (
                SELECT m.id,
                       1 - (m.embedding <=> ${i}::vector) as cosine_sim
                FROM memories m WHERE {where}
                ORDER BY cosine_sim DESC LIMIT 50
            ),
            keyword_search AS (
                SELECT m.id,
                       ROW_NUMBER() OVER (
                           ORDER BY ts_rank(m.tsv, websearch_to_tsquery('simple', ${i+1})) DESC
                       ) as rank
                FROM memories m
                WHERE m.tsv @@ websearch_to_tsquery('simple', ${i+1})
                  AND {where}
                LIMIT 50
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
            *(params + [es, query_text, limit]),
        )
        return [dict(r) for r in rows]

    async def compositional_search(
        self,
        conn: asyncpg.Connection,
        entities: list[str],
        limit: int = 5,
    ) -> list[dict]:
        """Find memories with ALL specified entities (AND logic)."""
        if not entities:
            return []
        placeholders = ",".join([f"${i+2}" for i in range(len(entities))])
        rows = await conn.fetch(
            f"""
            SELECT id, content, agent_id, project_id, category, trust_score, importance,
                   entities, memory_type, created_at
            FROM memories
            WHERE archived_at IS NULL
              AND entities::jsonb ?& ARRAY[{placeholders}]
            ORDER BY trust_score DESC, created_at DESC
            LIMIT $1
            """,
            limit, *entities,
        )
        return [dict(r) for r in rows]

    async def get_related(
        self,
        conn: asyncpg.Connection,
        memory_id: str,
        limit: int = 10,
    ) -> list[dict]:
        """Find related memories: entity overlap first, then semantic fallback."""
        row = await conn.fetchrow(
            "SELECT embedding, entities FROM memories WHERE id = $1", memory_id,
        )
        if not row:
            return []

        results: list[dict] = []
        entities_val = row["entities"]

        # 1. Entity overlap
        if entities_val:
            entities_list = (
                entities_val
                if isinstance(entities_val, list)
                else json.loads(entities_val) if isinstance(entities_val, str)
                else []
            )
            if entities_list:
                placeholders = ",".join([f"${i+2}" for i in range(len(entities_list))])
                rows = await conn.fetch(
                    f"""
                    SELECT id, content, agent_id, project_id, category, trust_score, created_at
                    FROM memories WHERE id != $1 AND archived_at IS NULL
                      AND entities::jsonb ?| ARRAY[{placeholders}]
                    ORDER BY trust_score DESC LIMIT ${len(entities_list)+3}
                    """,
                    memory_id, *[str(e) for e in entities_list], limit,
                )
                results = [dict(r) for r in rows]

        # 2. Semantic similarity fallback
        if len(results) < limit and row["embedding"]:
            emb = row["embedding"]
            es = self.format_vector(emb) if isinstance(emb, list) else emb
            existing_ids = {r["id"] for r in results}

            id_filter = ""
            id_params = []
            param_idx = len(results) + 2
            for eid in existing_ids:
                id_filter += f" AND id != ${param_idx}"
                id_params.append(eid)
                param_idx += 1

            rows = await conn.fetch(
                f"""
                SELECT id, content, agent_id, project_id, category, trust_score, created_at,
                       1 - (embedding <=> ${param_idx}::vector) as similarity
                FROM memories WHERE id != $1 AND archived_at IS NULL {id_filter}
                ORDER BY similarity DESC LIMIT ${param_idx + 1}
                """,
                memory_id, *id_params, es, limit - len(results),
            )
            for r in rows:
                d = dict(r)
                if d["id"] not in existing_ids:
                    results.append(d)

        return results[:limit]

    # ── Serialization helper ──────────────────────────────────────────────────

    @staticmethod
    def serialize_row(row: dict) -> dict:
        """Convert raw dict to safe response, removing heavy fields."""
        d = dict(row)
        d.pop("embedding", None)
        d.pop("tsv", None)
        for k, v in d.items():
            if isinstance(v, (set, frozenset)):
                d[k] = list(v)
        return d