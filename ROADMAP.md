# Memory API v2 — Modernizacja (Python, port 8765)

**Baza:** `/home/ArndtOs/Tools/memory-api-v2/` (Python + FastAPI + asyncpg)
**Baza danych:** `pgmemory` (PostgreSQL + pgvector 1536d)
**Obecny stan:** 17 tabel, hybrid search (RRF), embeddings (Router→Gemini→OpenRouter→Ollama),
                entity resolution, feedback, temporal decay, contradiction detection,
                DreamService (6 stages), Polish FTS, memory relations graph, per-fact TTL

---

## Co jest już zaimplementowane ✅

| Komponent | Status | Lokalizacja |
|-----------|--------|-------------|
| pgvector (1536d) | ✅ | `memories.embedding_1536` |
| Hybrid search (vector + FTS + RRF) | ✅ | `routes/memories.py` — `POST /search` |
| Embedding chain (Router→Gemini→OpenRouter→Ollama) | ✅ | `core.py` — `get_embedding()` |
| Circuit breaker | ✅ | `core.py` — `CircuitBreaker` |
| CRUD + soft-delete (archive) | ✅ | `routes/memories.py` |
| LLM fact extraction | ✅ | `routes/admin.py` — `POST /extract-facts` |
| Entity extraction + compositional search | ✅ | `routes/memories.py` |
| Related memories | ✅ | `routes/memories.py` — `GET /memories/{id}/related` |
| User profiles | ✅ | `routes/profiles.py` |
| Temporal decay (1-year half-life) | ✅ | `routes/hygiene.py` — `POST /memories/decay` |
| Contradiction detection | ✅ | `routes/hygiene.py` — `POST /hygiene/run` |
| Entity resolution (merge duplicates) | ✅ | `routes/hygiene.py` — `POST /entities/resolve` |
| Feedback + trust_score | ✅ | `routes/hygiene.py` — `POST /memories/{id}/feedback` |
| Purge (archive + permanent delete) | ✅ | `routes/hygiene.py` — `DELETE /memories/purge` |
| pi_memory_key (structured keys) | ✅ | `routes/aliases.py` — `POST /pi-remember` |
| Vault file indexing | ✅ | `routes/vault.py` (jeśli istnieje) |
| NotebookLM bridge | ✅ | `server.py` — `POST /notebooklm-ask` |
| Auth (x-api-token) | ✅ | `core.py` — `verify_token()` |
| RouterEmbeddingProvider (primary 1536d) | ✅ | `core.py` — `get_embedding()` |
| Polish FTS (unaccent + trigram + trigger) | ✅ | `routes/setup.py` + `db.py` |
| Embedding migration 1024d→1536d | ✅ | `scripts/migrate-embed-dim.py` |
| DreamService orchestrator (6 stages) | ✅ | `services/dream.py` |
| Dream LINK (cosine > 0.92) | ✅ | `services/dream.py` |
| Dream CONSOLIDATE (LLM merge) | ✅ | `services/dream.py` |
| Dream REFLECT (LLM insights) | ✅ | `services/dream.py` |
| Dream DECAY (boost/decay trust) | ✅ | `services/dream.py` + `routes/hygiene.py` |
| Dream BUDGET (prune > 500/agent) | ✅ | `services/dream.py` |
| Dream EXPIRE (auto-archive expired) | ✅ | `services/dream.py` + `routes/admin.py` |
| Smart inline dedup (merge on duplicate) | ✅ | `routes/memories.py` |
| Per-fact TTL (`expires_in_hours`) | ✅ | `routes/memories.py` + `db.py` |
| Auto-extract entities via LLM | ✅ | `routes/memories.py` |
| Memory relations graph (table + CRUD + BFS) | ✅ | `routes/relations.py` + `db.py` |
| `POST /memories/expire` endpoint | ✅ | `routes/admin.py` |

---

## Czego brakuje i co trzeba dodać

### Faza M1: Router embedding + Polish FTS (~6h) ✅ (done: 2026-05-22)

#### M1.1: RouterEmbeddingProvider ✅
- **Plik:** `core.py` — `get_embedding()`
- Router (http://127.0.0.1:18881/v1/embeddings) jako primary provider, model `openai/text-embedding-3-small` (1536d)
- Fallback chain: Gemini (1536d) → OpenRouter bge-m3 → Ollama nomic-embed-text
- Config: `router_key: str`, `ROUTER_API_KEY` env var
- **Czas:** 2h → zrobione

#### M1.2: Polish FTS configuration ✅
- **Plik:** `routes/setup.py` + `db.py`
- `CREATE EXTENSION IF NOT EXISTS unaccent` + `pg_trgm`
- `polish_unaccent` text search configuration
- Trigger `update_memories_tsv()`: `to_tsvector('english', content) || to_tsvector('polish_unaccent', content)`
- Endpoint: `POST /api/setup/fts` — inicjalizacja FTS
- **Czas:** 2h → zrobione

#### M1.3: Zmiana embedding dimension na 1536 ✅
- **Plik:** `core.py` (embed_dim=1536) + `scripts/migrate-embed-dim.py`
- `EMBED_DIM=1536` w Config
- Gemini `output_dimensionality=1024` → `1536`
- Skrypt migracyjny: `ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_1536 vector(1536)`
- Nowe embeddingi idą do `embedding_1536`, stare `embedding(1024)` pozostaje dla kompatybilności
- **Czas:** 2h → zrobione

---

### Faza M2: Dream Engine (~17h) ✅ (done: 2026-05-22)

Pełny 6-stage Dream cycle w `services/dream.py`:

```
LINK → CONSOLIDATE → REFLECT → DECAY → BUDGET → EXPIRE
```

#### M2.1: DreamService orchestrator ✅
- **Plik:** `services/dream.py`
- Klasa `DreamService` z background asyncio loop (interval: 3600s)
- Endpointy:
  - `POST /dream/run` — triggeruje full cycle
  - `GET /dream/status` — stan cyklu + statystyki
- Działa per agent (iteruje po wszystkich aktywnych agent_id)
- **Czas:** 3h → zrobione

#### M2.2: Stage LINK ✅
- **Plik:** `services/dream.py`
- Cosine similarity > 0.92 między f aktami tego samego agenta (`embedding_1536 <=>`)
- Tworzy dwukierunkowe `linked_ids` (JSONB array)
- Limit: max 100 par na cykl
- **Czas:** 2h → zrobione

#### M2.3: Stage CONSOLIDATE ✅
- **Plik:** `services/dream.py`
- LLM (GPT-4o-mini przez Router, temp=0.1, max_tokens=500) scala pary podobnych faktów
- Prompt: "Czy to ten sam fakt? Jeśli tak — zwróć scaloną wersję"
- JSON response: `{canMerge, mergedContent, mergedType, reason}`
- Nowy fakt dostaje `trust_score=0.85`, oryginały archiwizowane
- **Czas:** 4h → zrobione

#### M2.4: Stage REFLECT ✅
- **Plik:** `services/dream.py`
- LLM analizuje grupy 5 powiązanych faktów (linked_ids)
- Jeśli ma insight → generuje refleksję (`memory_type='reflection'`)
- Tylko dla agentów z ≥3 linked faktami
- **Czas:** 3h → zrobione

#### M2.5: Stage DECAY ✅
- **Plik:** `services/dream.py` + `routes/hygiene.py`
- Hot facts (access_count > 0, updated < 7d): `trust_score += 0.05`
- Cold facts (no access > 90d): `trust_score -= 0.1`
- Positive feedback (≥3 helpful): `trust_score += 0.02`
- **Czas:** 1h → zrobione

#### M2.6: Stage BUDGET ✅
- **Plik:** `services/dream.py`
- Gdy agent > 500 faktów → archiwizuj nadmiar (od najniższego trust_score)
- Obsługa `dry_run` do sprawdzenia bez zmian
- **Czas:** 1h → zrobione

#### M2.7: Stage EXPIRE ✅ (added 2026-05-22)
- **Plik:** `services/dream.py` + `routes/admin.py`
- Archiwizuje fakty z `expires_at < NOW()`
- Endpoint: `POST /memories/expire` — standalone trigger
- Zintegrowany jako stage 6 w dream cycle
- **Czas:** 0.5h

---

### Faza M3: Integracja z Sejf + Feedback w search (~6h)

- **Pending** — nierozpoczęte

### Faza M4: Memory graph + Context optimizer (~8h)

- **Pending** — nierozpoczęte

---

## Podsumowanie — wszystkie zadania

| # | Zadanie | Faza | Czas | Status |
|---|---------|------|------|--------|
| M1.1 | Router embedding (core.py) | M1 | 2h | ✅ |
| M1.2 | Polish FTS (schema + trigger) | M1 | 2h | ✅ |
| M1.3 | Embedding dim 1536 migration | M1 | 2h | ✅ |
| M2.1 | DreamService orchestrator (dream.py) | M2 | 3h | ✅ |
| M2.2 | Stage LINK (dream.py) | M2 | 2h | ✅ |
| M2.3 | Stage CONSOLIDATE (dream.py) | M2 | 4h | ✅ |
| M2.4 | Stage REFLECT (dream.py) | M2 | 3h | ✅ |
| M2.5 | Stage DECAY boost (hygiene.py) | M2 | 1h | ✅ |
| M2.6 | Stage BUDGET (hygiene.py) | M2 | 1h | ✅ |
| M2.7 | Stage EXPIRE (dream.py + admin.py) | M2 | 0.5h | ✅ |
| M3.1 | Feedback reranking w search | M3 | 1h | ❌ |
| M3.2 | Sejf auto-indexing | M3 | 3h | ❌ |
| M3.3 | Dream health endpoint | M3 | 1h | ❌ |
| M4.1 | Tabela memory_relations | M4 | 1h | ✅ |
| M4.2 | Graph walk w search | M4 | 3h | ❌ |
| M4.3 | Context optimizer | M4 | 4h | ❌ |
| | Pamięć osobista — Mem0-inspired | X | 2h | ✅ |
| | **Łącznie** | | **~35h** | **12/17 ✅** |

### Kolejność wdrożenia (completed)

```
M1 (6h)  → ✅ Router + Polish FTS + 1536d
   │
   ▼
M2 (17h) → ✅ Dream engine (6 stages)
   │
   ▼
M4.1 (1h) → ✅ Memory relations graph
   │
   ▼
Mem0 (2h) → ✅ Expires/Auto-extract/Dedup
   │
   ▼
M3 + M4.2 + M4.3 → ❌ Pending
```

### Mem0-Inspired Enhancements (done: 2026-05-22, ~2h)

| # | Feature | Priority | Plik |
|---|---------|----------|------|
| 1 | Smart inline dedup (merge on duplicate) | High | `routes/memories.py` |
| 2 | `expires_at` + per-fact TTL (`expires_in_hours`) | Medium | `routes/memories.py` + `db.py` |
| 3 | Auto-extract entities via LLM (`extract: true` flag) | Low | `routes/memories.py` |
| 4 | Memory relations graph (table + CRUD + BFS) | Optional | `routes/relations.py` + `db.py` |

### Pliki zmodyfikowane/stworzone

| Plik | Operacja | Status |
|------|----------|--------|
| `core.py` | Router embedding + Config + 1536d | ✅ |
| `db.py` | Polish FTS init + expires_at + memory_relations | ✅ |
| `routes/setup.py` | **NOWY** — Schema migracje, FTS init | ✅ |
| `routes/hygiene.py` | Decay boost + Budget | ✅ |
| `routes/memories.py` | Smart dedup + expires + auto-extract | ✅ |
| `routes/admin.py` | EXPIRE endpoint + Dream status | ✅ |
| `routes/relations.py` | **NOWY** — memory_relations CRUD + BFS | ✅ |
| `services/dream.py` | **NOWY** — 6-stage Dream cycle | ✅ |
| `scripts/migrate-embed-dim.py` | **NOWY** — migracja 1024→1536d | ✅ |

### Wzorce z researchu

| Wzorzec | Repozytorium | Jak wykorzystano |
|---------|-------------|------------------|
| Dream orchestrator | cersei `auto_dream.rs` | Background asyncio loop, 6-stage pipeline |
| Consolidation pipeline | ai-kit `MemoryConsolidationService` | LLM prompt + store + archive |
| Graph relations | AgentDock `memory_connections` | Typed relations (supports/contradicts/...) |
| Inline dedup | Mem0 | Merge on duplicate zamiast reject |
| Temporal TTL | Mem0 | `expires_in_hours`, auto-archive w Dream |
| Auto-extract | Mem0 | LLM entity extraction przy add_memory |
