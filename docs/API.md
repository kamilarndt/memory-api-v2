# Memory API v2 — Specyfikacja Endpointów

## Common Response Format

```json
{
  "status": "ok|error|duplicate|updated",
  "data": {},
  "error": "message (if status=error)"
}
```

## Auth

All endpoints (except `/health`) accept `x-api-token` header.
- Dev mode (token = `dev-token-change-me` or unset): auth skipped
- Prod mode: `x-api-token` must match `MEMORY_API_TOKEN`

---

## Memory CRUD

### POST /memories
Add a new memory with auto-embedding and dedup check.

**Request:**
```json
{
  "content": "Rafał preferuje wieczorne wizyty",
  "agent_id": "budy",
  "project_id": "shark-barbershop",
  "category": "preference",
  "tags": ["rafal", "preferencje", "terminy"],
  "user_id": "rafal",
  "importance": 0.7,
  "memory_type": "preference",
  "expires_in_hours": 720,
  "extract": false
}
```

**Response 200:**
```json
{"status": "added", "id": "uuid-...', "agent_id": "budy", "embedding_status": "ok"}
```

**Response 200 (duplicate merged):**
```json
{"status": "updated", "id": "uuid-...", "message": "Updated existing memory (duplicate merged)"}
```

When a similar memory exists (cosine > 0.95), the existing memory is updated instead of creating a duplicate. The content is merged, `updated_at` refreshed, `access_count` incremented.

**Behavior:**
1. Generate embedding via Router (text-embedding-3-small, 1536d) with Gemini → OpenRouter → Ollama fallbacks
2. Check dedup: cosine similarity > 0.95 → merge into existing (UPDATE, not INSERT)
3. If `expires_in_hours` is set → compute `expires_at = NOW() + expires_in_hours`
4. If `extract: true` → LLM auto-extracts entities from content → save in `entities` JSONB
5. Insert into `memories` table
6. Extract entities from content → link in `fact_entity_links`

### GET /memories
List memories with filters.

**Query params:** `agent_id`, `project_id`, `category`, `memory_type`, `limit` (default 20), `offset` (default 0)

**Response:**
```json
{
  "memories": [...],
  "total": 1234,
  "page": 1
}
```

### GET /memories/{id}
Single memory detail. Does NOT return embedding bytes.

### PUT /memories/{id}
Update memory fields. Agent must own memory (check `agent_id`).

### DELETE /memories/{id}
Soft delete → `SET archived_at = NOW()`. Agent must own memory.

---

## Search

### POST /search
Hybrid search combining semantic (vector) + keyword (FTS).

**Request:**
```json
{
  "query": "preferencje Rafała",
  "agent_id": null,
  "project_id": null,
  "category": null,
  "limit": 5,
  "cross_agent": true
}
```

**Response:**
```json
{
  "results": [
    {
      "id": "...", "content": "...", "excerpt": "...",
      "category": "...", "agent_id": "...", "project_id": "...",
      "tags": [...], "confidence": 0.85,
      "created_at": "..."
    }
  ],
  "count": 3
}
```

**Algorithm:** RRF (Reciprocal Rank Fusion) combining:
1. Semantic search: top 50 by vector distance
2. Keyword search: top 50 by FTS rank
3. Final: `1/(60+semantic_rank) + 1/(60+keyword_rank)`

### POST /search/compositional
Find memories connected to MULTIPLE entities.

**Request:**
```json
{
  "entities": ["Kamil", "ubekv2"],
  "limit": 5
}
```

Uses PostgreSQL `?&` JSONB operator → memories that have ALL specified entities.

### GET /memories/{id}/related
Find related memories via:
1. Entity overlap first (shared entities)
2. Semantic similarity fallback

---

## Profiles

### POST /profiles/{user_id}
Create/update user profile with JSONB merge.

**Request:**
```json
{
  "preferences": {"language": "polish", "tz": "Europe/Warsaw"},
  "name": "Kamil",
  "role": "developer"
}
```

Uses `ON CONFLICT (user_id) DO UPDATE SET profile_data = profile_data || EXCLUDED.profile_data`.

---

## Relations (Graph)

### POST /memories/{memory_id}/relations
Create a relation between two memories.

**Query params:** `target_id` (required), `relation_type` (required — one of: `supports`, `contradicts`, `elaborates`, `generalizes`, `instance_of`, `related`, `causes`, `depends_on`)

**Response:**
```json
{"status": "ok", "relation": {"id": "uuid", "source_id": "...", "target_id": "...", "relation_type": "supports"}}
```

### GET /memories/{memory_id}/relations
List all outgoing + incoming relations for a memory.

**Query params:** `direction` (`out`/`in`/`both`, default `both`)

**Response:**
```json
{"status": "ok", "relations": [{"id": "uuid", "source_id": "...", "target_id": "...", "relation_type": "supports", "strength": 0.85}]}
```

### DELETE /memories/{memory_id}/relations/{relation_id}
Delete a specific relation.

### GET /memories/{memory_id}/relations/graph?depth=3
BFS traversal of the relation graph up to N levels deep.

**Response:**
```json
{"status": "ok", "nodes": [{"id": "uuid", "content": "...", "depth": 1}], "edges": [{"source": "...", "target": "...", "type": "supports"}]}
```

---

## Dream Service

### POST /api/dream/run
Run the full dream consolidation cycle (all 6 stages).

**Behavior:**
1. LINK — find similar facts (cosine > 0.92) and group by agent
2. CONSOLIDATE — LLM merges duplicates into unified fact
3. REFLECT — LLM generates high-level reflections from groups
4. DECAY — boost trust_score for recently accessed facts
5. BUDGET — archive excess when > 500 facts per agent
6. EXPIRE — archive memories past their `expires_at`

**Response:**
```json
{"status": "completed", "stats": {"link": 5, "consolidated": 2, "reflections": 1, "decayed": 10, "budget_archived": 0, "expired": 3}}
```

### GET /api/dream/status
Get current dream cycle state.

**Response:**
```json
{"status": "idle", "stage": "", "running": false, "last_completed": null, "stats": {}}
```

---

## Setup

### POST /api/setup/fts
Initialize Polish Full-Text Search configuration.

**Behavior:**
1. Enable `unaccent` extension
2. Enable `pg_trgm` extension for fuzzy matching
3. Create `polish_unaccent` text search configuration
4. Create or replace `update_memories_tsv()` trigger function
5. Update existing `tsv` column for all active memories

**Response:**
```json
{"status": "ok", "extensions": ["unaccent", "pg_trgm"], "fts_config": "polish_unaccent", "memories_updated": 1193}
```

---

## Expiry

### POST /memories/expire
Manually archive all memories past their `expires_at`.

**Behavior:**
- Sets `archived_at = NOW()` for memories where `expires_at IS NOT NULL AND expires_at < NOW()`
- Safe to call multiple times (idempotent — already archived are skipped)

**Response:**
```json
{"status": "ok", "expired": 12}
```

---

## Admin & Hygiene

### GET /health
```json
{
  "status": "ok",
  "version": "2.1",
  "checks": {
    "db": {"ok": true, "latency_ms": 0.5},
    "embedding": {"ok": true, "provider": "router", "model": "text-embedding-3-small", "dim": 1536}
  }
}
```

### GET /memories/stats
```json
{
  "overview": {"total": 1193, "active": 1100, "archived": 93, "avg_trust": 0.62, "agents": 3, "projects": 5},
  "by_agent": {"budy": 600, "pi-agent": 400, "claude": 100},
  "by_project": {"ubekv2": 500, "shark-barbershop": 200},
  "by_category": {"technical": 400, "lesson": 200, "turn": 300}
}
```

### POST /memories/decay
```json
{"status": "ok", "updated_count": 45}
```

### POST /extract-facts
```json
{"text": "Kamil powiedział że preferuje asyncpg..."}
```
→ LLM extracts facts, saves to memory. Supports the same `expires_in_hours` and `extract` params as `POST /memories`.
```json
{"extracted": 3, "saved": 2, "duplicates": 1, "facts": [...]}
```

### POST /memories/{id}/feedback
```json
{
  "agent_id": "budy",
  "feedback_type": "helpful",
  "comment": "Good memory, helped solve the problem"
}
```
Updates `trust_score`: `helpful → +0.05`, `not_helpful → -0.10`, `incorrect → -0.3`, `outdated → -0.15`.

### POST /hygiene/run
Full hygiene cycle:
1. Find contradictions (shared entities ≥2 + content similarity <0.3)
2. Flag as `contradictions JSONB`
3. Return flagged count + details

### DELETE /memories/purge
Purge archived memories.
**Query params:** `max_trust=0.2`, `min_age_days=90`, `before_days=30`

### POST /memories/expire
(See Expiry section above — also available under Admin & Hygiene.)

---

## Write Queue

### GET /queue/status
```json
{
  "pending": 5,
  "agents": {"budy": 3, "pi-agent": 2},
  "oldest": "2026-05-12T03:15:00Z"
}
```

### POST /queue/flush
```json
{"status": "ok", "flushed": 5, "failed": 0}
```