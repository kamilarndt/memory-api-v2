# Memory API v2 — Instrukcja dla Pi Agenta

**Data:** 2026-05-12  
**Autor:** Budy

---

## Co się zmieniło

### Stary → Nowy

| | Stary (v1) | Nowy (v2) |
|---|---|---|
| Plik | `Tools/memory-api/server.py` | `Tools/memory-api-v2/main.py` |
| Port | 8765 | **8765** (ten sam) |
| Framework | FastAPI + psycopg2 (sync) | FastAPI + asyncpg (async) |
| Baza | `pgmemory` (ta sama) | `pgmemory` (ta sama) |

### Nie musisz zmieniać:
- **URL bazy** — dalej ta sama `pgmemory`, wszystkie rekordy (1191+) są
- **Embedding dimension** — dalej 1024d (teraz z Gemini zamiast Ollama)
- **API endpointy** — te same nazwy, ten sam port 8765

---

## Co jest nowego

### 1. Nowe endpointy (dodane w v2)
| Endpoint | Opis |
|---|---|
| `POST /memories` | Dodaj pamięć z auto-embedding + dedup |
| `PUT /memories/{id}` | Update pamięci (agent może zmieniać tylko swoje) |
| `DELETE /memories/{id}` | Soft delete (archive) |
| `POST /search` | Hybrid search: semantic (vector) + keyword (FTS) + RRF |
| `POST /search/compositional` | Szukaj pamięci z WSZYSTKIMI entity (`?&`) |
| `GET /memories/{id}/related` | Powiązane pamięci (entity overlap + similarity) |
| `POST /memories/{id}/feedback` | Feedback loop → trust_score update |
| `POST /memories/decay` | Temporal decay recall |
| `POST /hygiene/run` | Wykryj sprzeczne fakty |
| `POST /entities/resolve` | Merge duplicate entities |
| `POST /extract-facts` | LLM auto-extract facts z tekstu |
| `DELETE /memories/purge` | Archive low-trust + delete old archived |
| `GET /profiles` | User profiles CRUD |
| `GET /memories/stats` | Enhanced stats (by agent/project/category) |

### 2. Gemini Embedding (primary)
- Primary: `gemini-embedding-001` (Google) → 1024d (`output_dimensionality=1024`)
- Fallback: OpenRouter (`baai/bge-m3`)
- Ostatni fallback: Ollama (`nomic-embed-text`)
- Circuit breaker: 5 faili → 120s cooldown → auto-recover

### 3. Auth
- `x-api-token` header — optional w dev mode
- Dev mode: token = `dev-token-change-me` → skip check
- W production trzeba podać poprawny token

### 4. Multi-Agent
- `agent_id` w każdym rekordzie (`budy`, `pi-agent`, `claude`)
- Search domyślnie: cross-agent (wszyscy)
- `agent_id=X` filtruje do конкретного agenta
- PUT/DELETE: agent NIE może zmieniać pamięci innego agenta

### 5. Nocne operacje (cron)
| Czas | Operacja |
|---|---|
| 02:00 | Temporal decay (trust < 0.8) |
| 03:00 | Hygiene run (contradiction detection) |
| 05:30 | Stats snapshot |

---

## Jak używać (dla Pi Agenta)

### Dodaj pamięć
```bash
curl -s -X POST http://localhost:8765/memories \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Kamil preferuje asyncpg zamiast psycopg2",
    "agent_id": "pi-agent",
    "project_id": "ubekv2",
    "category": "technical",
    "importance": 0.8
  }'
```

### Szukaj pamięci
```bash
curl -s -X POST http://localhost:8765/search \
  -H "Content-Type: application/json" \
  -d '{"query": "asyncpg psycopg2", "limit": 5, "cross_agent": true}'
```

### Dodaj feedback
```bash
curl -s -X POST http://localhost:8765/memories/{memory_id}/feedback \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "pi-agent", "feedback_type": "helpful"}'
```

### Wyświetl statystyki
```bash
curl -s http://localhost:8765/memories/stats
```

---

## Schema bazy (memories)

```
id UUID PK
content TEXT NOT NULL
embedding vector(1024)
agent_id VARCHAR(64)      -- 'pi-agent', 'budy', 'claude'
project_id VARCHAR(128)   -- 'ubekv2', '' = general
user_id VARCHAR(128)      -- 'kamil', 'rafal', ...
category VARCHAR(32)      -- 'technical', 'business', 'lesson', etc.
tags TEXT[]               -- array
metadata JSONB
source_file VARCHAR(512)
created_at TIMESTAMPTZ
updated_at TIMESTAMPTZ
trust_score REAL          -- 0.0-1.0 (auto z feedback)
importance REAL           -- 0.0-1.0
archived_at TIMESTAMPTZ   -- NULL = active
entities JSONB            -- ['entity1', 'entity2']
linked_ids TEXT           -- ['uuid1', 'uuid2']
access_count INTEGER      -- ile razy retrieved
memory_type VARCHAR(32)   -- 'factual', 'preference', 'lesson', etc.
contradictions JSONB      -- flagged contradictions
```

---

## Stary server (v1)
- `Tools/memory-api/server.py` — **wyłączony**
- Był na porcie 8765 — v2 teraz tam jest
- Nie uruchamiaj — v2 jest zamiennikiem

---

## Status
- ✅ Server: PID 30001, port 8765
- ✅ Baza: `pgmemory` (1191+ rekordów)
- ✅ Embedding: Gemini (1024d)
- ✅ Cron: decay, hygiene, stats snapshot
- ✅ Auth: dev mode (no token required)

---

*Ostatnia aktualizacja: 2026-05-12*
