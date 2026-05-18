# Memory API v2 — Specyfikacja Endpointów

## Common Response Format

```json
{
  "status": "ok|error|duplicate",
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
  "memory_type": "preference"
}
```

**Response 200:**
```json
{"status": "added", "id": "uuid-...', "agent_id": "budy", "embedding_status": "ok"}
```

**Response 200 (duplicate):**
```json
{"status": "duplicate", "existing_id": "uuid-...", "similarity": 0.97}
```

**Behavior:**
1. Generate embedding via OpenRouter (baai/bge-m3) or Ollama fallback
2. Check dedup: cosine similarity > 0.95 → skip, return existing
3. Insert into `memories` table
4. Extract entities from content → link in `fact_entity_links`

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

## Admin & Hygiene

### GET /health
```json
{
  "status": "ok",
  "version": "2.0",
  "checks": {
    "db": {"ok": true, "latency_ms": 0.5},
    "embedding": {"ok": true, "provider": "openrouter", "model": "baai/bge-m3"}
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
→ LLM extracts facts, saves to memory.
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