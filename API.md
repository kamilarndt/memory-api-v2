# Memory API v2 — REST API Documentation

**Base URL:** `http://localhost:8765`

**Auth:** All endpoints except `/health` accept an `x-api-token` header.
- Dev mode (token = `dev-token-change-me` or unset): auth is skipped
- Prod mode: `x-api-token` must match `MEMORY_API_TOKEN` env var

**Embedding:** Automatic on every write and search. Provider chain:
1. **Router** (primary, localhost:18881, `text-embedding-3-small`, 1536d → truncated to 1024d)
2. **Gemini** (`gemini-embedding-001`, `output_dimensionality=1536`)
3. **OpenRouter** (`baai/bge-m3`, 1024d)
4. **Ollama** (`nomic-embed-text`, configurable)

Circuit breaker: 5 failures → 120s cooldown → auto-recover.

**Content-Type:** `application/json` for all POST/PUT requests.

---

## 1. Health

### `GET /health`
No auth required. Returns DB latency, embedding provider info, and circuit breaker status.

**Response 200:**
```json
{
  "status": "ok",
  "version": "2.0",
  "checks": {
    "db": { "ok": true, "latency_ms": 1.23 },
    "embedding": { "ok": true, "provider": "router", "model": "text-embedding-3-small", "dimension": 1536 },
    "circuit_breaker": { "state": "closed", "failures": 0, "cooldown_remaining": 0.0 }
  }
}
```

---

## 2. Memories (CRUD)

### `POST /memories`
Add a memory with auto-embedding and semantic dedup (cosine > 0.95 merges into existing).

**Request body:**
| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `content` | string | **yes** | — | Memory content |
| `agent_id` | string | no | `"unknown"` | Agent identifier |
| `project_id` | string | no | `""` | Project identifier |
| `category` | string | no | `"general"` | Category tag |
| `tags` | string[] | no | `[]` | Tags for filtering |
| `user_id` | string | no | `"default"` | User identifier |
| `importance` | float | no | `0.5` | Importance 0.0–1.0 |
| `memory_type` | string | no | `"factual"` | Type: factual, preference, lesson, etc. |
| `pi_memory_key` | string | no | `""` | Structured key e.g. `"pref.commit_style"` (upsert if exists) |
| `session_id` | string | no | `null` | Session UUID |
| `expires_in_hours` | int | no | `null` | TTL in hours; null = never expires |
| `extract` | bool | no | `false` | Auto-extract entities via LLM |

**Response 200 (added):**
```json
{ "status": "added", "id": "uuid-...", "agent_id": "pi-agent" }
```

**Response 200 (upserted by pi_memory_key):**
```json
{ "status": "updated", "id": "uuid-...", "revision": true }
```

**Response 200 (semantic duplicate merged):**
```json
{ "status": "updated", "id": "uuid-...", "agent_id": "pi-agent" }
```

---

### `GET /memories`
List memories with filters. Sorted by `created_at DESC`.

**Query params:**
| Param | Type | Default | Description |
|---|---|---|---|
| `agent_id` | string | — | Filter by agent |
| `project_id` | string | — | Filter by project |
| `category` | string | — | Filter by category |
| `memory_type` | string | — | Filter by memory type |
| `limit` | int | `20` | Max results (1–100) |
| `offset` | int | `0` | Pagination offset |

**Response 200:**
```json
{
  "memories": [
    {
      "id": "uuid",
      "content": "...",
      "category": "general",
      "agent_id": "pi-agent",
      "project_id": "my-project",
      "user_id": "default",
      "tags": ["tag1"],
      "source_file": null,
      "importance": 0.5,
      "trust_score": null,
      "memory_type": "factual",
      "created_at": "2026-05-25T12:00:00+00:00"
    }
  ],
  "count": 1
}
```

---

### `GET /memories/{memory_id}`
Get a single memory by ID. Does not return embedding or tsv fields.

**Response 200:**
```json
{
  "memory": { "id": "uuid", "content": "...", "category": "general", ... }
}
```

**Response 404:** `{ "detail": "Memory not found" }`

---

### `PUT /memories/{memory_id}`
Update a memory. Agent must own the memory (by agent_id).

**Request body (all optional):**
```json
{
  "content": "Updated content",
  "category": "technical",
  "tags": ["new", "tags"],
  "project_id": "new-project",
  "importance": 0.8,
  "memory_type": "lesson"
}
```

**Response 200:**
```json
{ "status": "updated", "memory": { "id": "uuid", "content": "...", ... } }
```

**Response 404:** `{ "detail": "Memory not found or archived" }`

---

### `DELETE /memories/{memory_id}`
Soft-delete a memory (sets `archived_at = NOW()`).

**Response 200:**
```json
{ "status": "archived", "memory_id": "uuid" }
```

---

### `GET /memories/{memory_id}/full`
Get full memory detail (without embedding/tsv). Public endpoint. Includes `memory_relations_count`.

**Response 200:**
```json
{
  "memory": {
    "id": "uuid",
    "content": "...",
    "agent_id": "pi-agent",
    "project_id": "my-project",
    "category": "general",
    "tags": [],
    "importance": 0.5,
    "memory_type": "factual",
    "pi_memory_key": "pref.example",
    "session_id": "uuid",
    "entities": ["entity1"],
    "access_count": 3,
    "expires_at": null,
    "created_at": "...",
    "updated_at": "...",
    "archived_at": null,
    "memory_relations_count": 2
  }
}
```

---

### `GET /memories/compact`
List memories with compact fields only (id, pi_memory_key, memory_type, importance, created_at). Public endpoint.

**Query params:** Same as `GET /memories`.

**Response 200:**
```json
{
  "memories": [
    { "id": "uuid", "pi_memory_key": "pref.example", "memory_type": "factual", "importance": 0.5, "created_at": "..." }
  ],
  "count": 1
}
```

---

## 3. Search

### `POST /search`
Hybrid search: semantic (pgvector cosine) + keyword (PostgreSQL FTS) combined via RRF (Reciprocal Rank Fusion).

**Request body:**
| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | string | **yes** | — | Search query |
| `agent_id` | string | no | `null` | Agent filter (ignored if cross_agent=true) |
| `project_id` | string | no | `null` | Project filter |
| `category` | string | no | `null` | Category filter |
| `limit` | int | no | `5` | Max results |
| `cross_agent` | bool | no | `true` | Include all agents |
| `compact` | bool | no | `false` | Compact response (no content) |

**Response 200 (normal):**
```json
{
  "results": [
    {
      "id": "uuid",
      "content": "...",
      "excerpt": "First 300 chars...",
      "category": "general",
      "agent_id": "pi-agent",
      "project_id": "my-project",
      "user_id": "default",
      "tags": [],
      "source_file": null,
      "importance": 0.5,
      "trust_score": null,
      "memory_type": "factual",
      "created_at": "...",
      "pi_memory_key": null,
      "confidence": 0.8521
    }
  ],
  "count": 3
}
```

**Response 200 (compact=true):**
```json
{
  "results": [
    {
      "id": "uuid",
      "pi_memory_key": "pref.example",
      "memory_type": "factual",
      "confidence": 0.8521,
      "created_at": "..."
    }
  ],
  "count": 3
}
```

### `POST /search/compositional`
Find memories that have ALL specified entities in their `entities` JSONB array. Uses PostgreSQL `?&` operator.

**Request body:**
| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `entities` | string[] | **yes** | — | Entity names (all must match) |
| `limit` | int | no | `5` | Max results |

**Response 200:**
```json
{
  "results": [ { "id": "uuid", "content": "...", "entities": ["entity1", "entity2"], ... } ],
  "count": 2
}
```

---

## 4. Sessions

### `POST /sessions`
Create a new active session.

**Query params:**
| Param | Type | Required | Description |
|---|---|---|---|
| `agent_id` | string | **yes** | Agent identifier |
| `project_id` | string | no | Optional project identifier |

**Response 200:**
```json
{
  "status": "active",
  "session": { "id": "uuid", "agent_id": "pi-agent", "project_id": "my-project", "started_at": "..." }
}
```

### `POST /sessions/{id}/end`
End a session with an optional summary.

**Query params:**
| Param | Type | Required | Description |
|---|---|---|---|
| `summary` | string | no | Session summary text |

**Response 200:**
```json
{ "status": "ended", "ended_at": "...", "session_id": "uuid" }
```

### `GET /sessions/{id}`
Get session details with observation count.

**Response 200:**
```json
{
  "id": "uuid",
  "agent_id": "pi-agent",
  "project_id": "my-project",
  "status": "active",
  "summary": null,
  "started_at": "...",
  "ended_at": null,
  "observation_count": 5
}
```

### `GET /sessions/{id}/timeline`
Get chronological observation timeline for a session, optionally centered around a focus memory ID.

**Query params:**
| Param | Type | Default | Description |
|---|---|---|---|
| `before` | int | `5` | Observations before focus (0–100) |
| `after` | int | `5` | Observations after focus (0–100) |
| `focus_id` | string | — | Focus memory ID for centering |

**Response 200:**
```json
{
  "session_id": "uuid",
  "observations": [
    { "id": "uuid", "content": "...", "memory_type": "factual", "importance": 0.5, "created_at": "..." }
  ],
  "count": 11
}
```

### `GET /sessions`
List sessions with pagination and filters.

**Query params:**
| Param | Type | Default | Description |
|---|---|---|---|
| `limit` | int | `20` | Max results (1–200) |
| `offset` | int | `0` | Pagination offset |
| `project_id` | string | — | Filter by project |
| `agent_id` | string | — | Filter by agent |
| `status` | string | — | Filter by status ('active', 'ended') |

**Response 200:**
```json
{
  "sessions": [ { "id": "uuid", "agent_id": "pi-agent", "status": "active", ... } ],
  "total": 42,
  "limit": 20,
  "offset": 0
}
```

### `DELETE /sessions/{id}`
Delete a session. Refuses if it has observations (archive memories first).

**Response 200:** `{ "status": "ok", "deleted": "uuid" }`

**Response 409:** `{ "detail": "Cannot delete session uuid: has 5 observations. Archive memories first." }`

---

## 5. Conflicts

### `GET /conflicts`
List conflict verdicts with pagination.

**Query params:**
| Param | Type | Default | Description |
|---|---|---|---|
| `project` | string | — | Filter by project |
| `status` | string | — | Filter by verdict type |
| `limit` | int | `20` | Max results (1–200) |
| `offset` | int | `0` | Pagination offset |

**Response 200:**
```json
{
  "verdicts": [
    {
      "id": "uuid",
      "source_id": "uuid",
      "target_id": "uuid",
      "verdict": "contradictory",
      "reason": "...",
      "evidence": "...",
      "confidence": 0.85,
      "model": "gpt-4o",
      "session_id": "uuid",
      "project_id": "my-project",
      "created_at": "..."
    }
  ],
  "total": 5,
  "limit": 20,
  "offset": 0
}
```

### `POST /conflicts/judge`
Record an agent verdict between two memories.

**Query params:**
| Param | Type | Required | Description |
|---|---|---|---|
| `source_id` | string | **yes** | Source memory ID |
| `target_id` | string | **yes** | Target memory ID |
| `verdict` | string | **yes** | One of: `contradictory`, `supportive`, `independent`, `superseded` |
| `reason` | string | no | Reason for verdict |
| `evidence` | string | no | Supporting evidence |
| `confidence` | float | no | 0.0–1.0 (default: 0.5) |
| `model` | string | no | Model used for judgment |
| `session_id` | string | no | Optional session ID |

**Response 200:**
```json
{
  "status": "ok",
  "verdict": { "id": "uuid", "source_id": "uuid", "target_id": "uuid", "verdict": "contradictory", ... }
}
```

**Response 409:** `{ "detail": "Verdict already exists between these memories..." }`

### `POST /conflicts/compare`
Compare two memories by ID and return any existing verdict and full analysis of both.

**Query params:**
| Param | Type | Required | Description |
|---|---|---|---|
| `id1` | string | **yes** | First memory ID |
| `id2` | string | **yes** | Second memory ID |

**Response 200:**
```json
{
  "analysis": {
    "memory_a": { "id": "uuid", "content": "...", "type": "factual", "importance": 0.5, "created_at": "..." },
    "memory_b": { ... }
  },
  "existing_verdict": { "id": "uuid", "verdict": "supportive", ... }
}
```

### `GET /conflicts/stats`
Aggregate verdict counts by type, optionally filtered by project.

**Response 200:**
```json
{
  "total": 10,
  "breakdown": { "contradictory": 4, "supportive": 3, "independent": 2, "superseded": 1 },
  "project": null
}
```

### `POST /conflicts/scan`
Scan a project (or all memories) for candidate memory pairs that might need a verdict. Pairs are selected by: same project (via session), matching memory types, not already having a verdict.

**Query params:**
| Param | Type | Default | Description |
|---|---|---|---|
| `project` | string | — | Project to scan |
| `limit` | int | `10` | Max candidate pairs (1–100) |

**Response 200:**
```json
{
  "candidates": [
    {
      "memory_a": { "id": "uuid", "content": "...", "type": "factual", "importance": 0.5, "created_at": "..." },
      "memory_b": { ... }
    }
  ],
  "count": 3,
  "project": "my-project"
}
```

---

## 6. Prompts

### `POST /prompts`
Save a prompt to the database.

**Query params:**
| Param | Type | Required | Description |
|---|---|---|---|
| `content` | string | **yes** | Prompt content |
| `session_id` | string | no | Session UUID |
| `agent_id` | string | **yes** | Agent identifier |
| `project_id` | string | no | Optional project identifier |

**Response 200:**
```json
{
  "status": "ok",
  "prompt": { "id": "uuid", "session_id": null, "agent_id": "pi-agent", "project_id": "", "content": "...", "created_at": "..." }
}
```

### `GET /prompts/search`
Search prompts by content using ILIKE.

**Query params:**
| Param | Type | Required | Default | Description |
|---|---|---|---|---|
| `q` | string | **yes** | — | Search query (substring match) |
| `limit` | int | no | `20` | Max results (1–200) |

**Response 200:**
```json
{
  "results": [ { "id": "uuid", "session_id": "uuid", "agent_id": "pi-agent", "project_id": "", "content": "...", "created_at": "..." } ],
  "count": 2,
  "query": "deploy"
}
```

### `GET /prompts/session/{session_id}`
List prompts for a given session, ordered by `created_at ASC`.

**Query params:**
| Param | Type | Default | Description |
|---|---|---|---|
| `limit` | int | `50` | Max results (1–500) |
| `offset` | int | `0` | Pagination offset |

**Response 200:**
```json
{
  "session_id": "uuid",
  "prompts": [ { "id": "uuid", "agent_id": "pi-agent", "project_id": "", "content": "...", "created_at": "..." } ],
  "total": 12,
  "limit": 50,
  "offset": 0
}
```

---

## 7. Relations (Graph)

### `POST /memories/{memory_id}/relations`
Create a directed relation from source to target memory.

**Query params:**
| Param | Type | Required | Description |
|---|---|---|---|
| `target_id` | string | **yes** | Target memory UUID |
| `relation_type` | string | **yes** | One of: `supports`, `contradicts`, `elaborates`, `generalizes`, `instance_of`, `related`, `causes`, `depends_on` |

**Response 200:**
```json
{
  "status": "ok",
  "relation": { "id": "uuid", "source_id": "uuid", "target_id": "uuid", "relation_type": "supports" }
}
```

### `GET /memories/{memory_id}/relations`
Return outgoing + incoming relations for a memory.

**Response 200:**
```json
{
  "memory_id": "uuid",
  "outgoing": [ { "id": "uuid", "target_id": "uuid", "relation_type": "supports", "created_at": "..." } ],
  "incoming": [ { "id": "uuid", "source_id": "uuid", "relation_type": "elaborates", "created_at": "..." } ]
}
```

### `DELETE /memories/{memory_id}/relations/{relation_id}`
Delete a relation. Must belong to the specified memory (source or target).

**Response 200:** `{ "status": "ok", "deleted": "uuid" }`

### `GET /memories/{memory_id}/relations/graph`
BFS traversal of the relation graph from a starting memory, up to specified depth.

**Query params:**
| Param | Type | Default | Description |
|---|---|---|---|
| `depth` | int | `2` | BFS depth (1–5) |

**Response 200:**
```json
{
  "nodes": {
    "uuid1": { "id": "uuid1", "content": "First 150 chars...", "type": "factual", "importance": 0.5 },
    "uuid2": { ... }
  },
  "edges": [
    { "id": "uuid", "source": "uuid1", "target": "uuid2", "type": "supports" }
  ]
}
```

---

## 8. Profiles

### `GET /profiles`
List all user profiles.

**Response 200:** `{ "profiles": [ { "user_id": "default", "profile_data": { ... }, "updated_at": "..." } ], "count": 1 }`

### `GET /profiles/{user_id}`
Get a specific user profile.

**Response 200:** `{ "profile": { "user_id": "default", "profile_data": { "preferences": { "language": "polish" } }, "updated_at": "..." } }`

### `POST /profiles/{user_id}`
Update (merge) a user profile. Uses JSONB `||` merge — existing keys are preserved, new keys are added.

**Request body:** Any JSON object (the full profile_data payload).

**Response 200:** `{ "status": "updated", "profile": { "user_id": "default", "profile_data": { ... }, "updated_at": "..." } }`

---

## 9. Admin

### `GET /stats`
Basic stats — count of active memories.

**Response 200:** `{ "overview": { "active": 1100 } }`

### `GET /memories/stats`
Enhanced stats with breakdowns by agent, project, and category.

**Response 200:**
```json
{
  "overview": { "total": 1200, "active": 1100, "archived": 100, "avg_trust": 0.62, "avg_importance": 0.45, "agents": 3, "projects": 5 },
  "by_agent": { "pi-agent": 600, "budy": 500 },
  "by_project": { "ubekv2": 400, "shark-barbershop": 100 },
  "by_category": { "technical": 300, "lesson": 200, "general": 600 }
}
```

### `POST /extract-facts`
Use LLM (OpenRouter) to extract key facts from text and save them to memory. Requires `OPENROUTER_API_KEY`.

**Request body:**
| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `text` | string | **yes** | — | Source text (max ~8000 chars) |
| `agent_id` | string | no | `"unknown"` | Agent identifier |
| `project_id` | string | no | `""` | Project identifier |
| `category` | string | no | `"general"` | Category for extracted facts |

**Response 200:**
```json
{
  "extracted": 5,
  "saved": 3,
  "duplicates": 2,
  "facts": [
    { "content": "...", "category": "technical", "importance": 0.8, "tags": ["tag1"] }
  ]
}
```

### `POST /dream/run`
Run the full dream consolidation cycle (LINK → CONSOLIDATE → REFLECT → DECAY → BUDGET → EXPIRE).

**Response 200:**
```json
{
  "status": "completed",
  "stats": { "link": 5, "consolidated": 2, "reflections": 1, "decayed": 10, "budget_archived": 0, "expired": 3 }
}
```

### `GET /dream/status`
Get current dream cycle state and last run stats.

**Response 200:**
```json
{
  "running": false,
  "stage": "idle",
  "progress": 0,
  "started_at": null,
  "last_completed": "2026-05-25T02:00:00Z",
  "stats": { ... }
}
```

### `POST /memories/expire`
Archive all memories past their `expires_at`. Idempotent.

**Response 200:** `{ "status": "ok", "expired": 12 }`

---

## 10. Hygiene

### `POST /memories/{memory_id}/feedback`
Record feedback on a memory and recalculate its trust_score from the last 10 entries.

**Scoring deltas:** helpful → 1.0, not_helpful → 0.5, incorrect → 0.0, outdated → 0.3

**Request body:**
| Field | Type | Required | Description |
|---|---|---|---|
| `agent_id` | string | **yes** | Agent providing feedback |
| `feedback_type` | string | **yes** | `helpful`, `not_helpful`, `incorrect`, or `outdated` |
| `comment` | string | no | Optional comment |

**Response 200:**
```json
{ "status": "feedback_recorded", "memory_id": "uuid", "trust_score": 0.85 }
```

### `POST /memories/decay`
Apply temporal decay to trust scores of non-archived memories with trust_score < 0.8. Formula: `trust_score *= 0.5^(age_in_years)`. Uses 1-year half-life.

**Response 200:** `{ "status": "ok", "updated_count": 45 }`

### `POST /hygiene/run`
Run contradiction detection across all memories. Finds pairs with shared entities + vector distance < 0.3, then flags both sides.

**Response 200:**
```json
{
  "status": "ok",
  "contradictions_found": 3,
  "pairs": [ { "memory_a": "uuid", "memory_b": "uuid", "trust_a": 0.6, "trust_b": 0.7, "vector_distance": 0.85 } ]
}
```

### `POST /entities/resolve`
Merge duplicate entities with similar names (exact case-folded match or pg_trgm similarity > 0.8). Migrates links, removes duplicates.

**Response 200:**
```json
{ "status": "ok", "resolved": 5, "merged": 3, "note": "Safe entity resolution completed" }
```

### `DELETE /memories/purge`
Two-phase cleanup: (1) Archive low-trust, old, or zero-access memories. (2) Permanently delete old archived records.

**Query params:**
| Param | Type | Default | Description |
|---|---|---|---|
| `max_trust` | float | `0.2` | Archive memories below this trust score |
| `min_age_days` | int | `90` | Only archive memories older than N days |
| `archived_before_days` | int | `30` | Permanently delete memories archived before N days ago |

**Response 200:** `{ "status": "ok", "archived": 15, "deleted": 3 }`

---

## 11. Aliases (Pi Agent Compat)

### `POST /pi-remember`
Legacy Pi Agent endpoint — save a fact with pi_memory_key and auto-dedup.

**Request body (JSON):**
| Field | Type | Required | Description |
|---|---|---|---|
| `content` | string | **yes** | Fact content |
| `key` | string | no | pi_memory_key (alias: `pi_memory_key`) |
| `agent_id` | string | no | Default: `"pi-agent"` |
| `project_id` | string | no | Default: `""` |
| `category` | string | no | Default: `"general"` |
| `importance` | float | no | Default: `0.5` |
| `tags` | string | no | Comma-separated tags |

### `POST /pi-search`
Legacy Pi Agent search endpoint with optional pi_memory_key filter (supports wildcard `lesson.*` → LIKE `lesson.%`). Uses semantic + keyword hybrid.

**Request body (JSON):**
| Field | Type | Required | Description |
|---|---|---|---|
| `query` | string | **yes** | Search query |
| `pi_memory_key` | string | no | Filter by key (supports `.*` wildcard) |
| `limit` | int | no | Default: `5` |

### `POST /save_fact`
Legacy save_fact endpoint — redirects to pi-remember logic.

### `GET /notebooklm-ask` / `POST /notebooklm-ask`
Legacy endpoints — return stub: `{ "status": "not_implemented", "message": "Use Memory API search instead" }`.

---

## 12. Setup

### `POST /setup/fts`
Initialize Polish Full-Text Search configuration. Creates:
- `unaccent` extension
- `pg_trgm` extension for fuzzy matching
- `polish_unaccent` text search configuration
- `update_memories_tsv()` trigger function (English + Polish unaccent)
- `tsv` column on `memories` table with GIN index
- Backfills existing memories

**Response 200:**
```json
{ "status": "ok", "fts_configured": true, "rows_backfilled": 1193 }
```

---

## 13. MCP SSE (Model Context Protocol)

### `GET /mcp/sse`
SSE (Server-Sent Events) endpoint for MCP protocol. Streams JSON-RPC responses to the client.
- First event: `endpoint` with the message POST URL (`/mcp/messages/{session_id}`)
- Subsequent events: `message` with JSON-RPC responses
- Keepalive: `ping` events every 30s

### `POST /mcp/messages/{session_id}`
Handle JSON-RPC messages for the MCP session. Supports:
- `initialize` — protocol handshake
- `notifications/initialized` — ack
- `tools/list` — returns tool list
- `tools/call` — executes a tool (add_memory, search_memories, list_memories, forget_memory)

**MCP Tools Available:**
| Tool | Description |
|---|---|
| `add_memory` | Store a new memory for an agent |
| `search_memories` | Search memories by semantic similarity |
| `list_memories` | List recent memories for an agent |
| `forget_memory` | Archive (soft-delete) a memory by ID |

---

## Database Schema (memories table)

| Column | Type | Description |
|---|---|---|
| `id` | UUID PK | Primary key |
| `content` | TEXT | Memory content |
| `embedding` | vector(1024) | Vector embedding (1024d) |
| `agent_id` | VARCHAR(64) | Agent identifier |
| `project_id` | VARCHAR(128) | Project identifier |
| `user_id` | VARCHAR(128) | User identifier |
| `category` | VARCHAR(32) | Category tag |
| `tags` | TEXT[] | Tags array |
| `source_file` | VARCHAR(512) | Source filename |
| `importance` | REAL | 0.0–1.0 |
| `trust_score` | REAL | 0.0–1.0 |
| `memory_type` | VARCHAR(32) | factual, preference, lesson, etc. |
| `pi_memory_key` | VARCHAR(255) | Structured key for Pi Agent |
| `session_id` | UUID | Associated session |
| `entities` | JSONB | Extracted entities |
| `contradictions` | JSONB | Flagged contradictions |
| `expires_at` | TIMESTAMPTZ | TTL expiration |
| `access_count` | INTEGER | Retrieval count |
| `tsv` | tsvector | Full-text search vector |
| `created_at` | TIMESTAMPTZ | Creation timestamp |
| `updated_at` | TIMESTAMPTZ | Last update timestamp |
| `archived_at` | TIMESTAMPTZ | Soft-delete timestamp |