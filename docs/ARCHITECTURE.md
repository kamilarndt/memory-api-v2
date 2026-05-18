# Memory API v2 — Dokumentacja Projektu

## 1. Cel

Centralny system pamięci dla wszystkich agentów AI na WSL2:
- **Budy/Hermes**, **Pi Agent**, **Claude Code**
- Współdzielona, semantyczna pamięć (PostgreSQL + pgvector)
- Izolacja per-agent (`agent_id`) z możliwością cross-agent search
- Automatyczna higiena pamięci w nocy (cron via Hermes)

**Stack:** FastAPI + asyncpg + PostgreSQL (pgvector) + SQLite write queue
**Port:** 8766 (v2), docelowo 8765 (po migracji)
**Baza:** `pgmemory` (tena sama — zero utraty danych)

---

## 2. Architektura

```
┌──────────────────────────────────────────────────────────────┐
│                     Memory API v2                             │
├──────────────────────────────────────────────────────────────┤
│  FastAPI (async)                                              │
├─────────────────────┬────────────────────┬────────────────────┤
│  routes/            │  core.py           │  workers/          │
│  ├── memories.py    │  config/dataclass  │  ├── queue.py      │
│  ├── profiles.py    │  auth middleware   │  └── extract.py    │
│  ├── admin.py       │  circuit breaker   └────────────────────┘
│  └── hygiene.py     │  embedding cache                        │
│                     │  db.py (asyncpg pool)                   │
│                     │  entities.py (resolution)               │
├─────────────────────┼────────────────────┼────────────────────┤
│  PostgreSQL (pgmemory) — główna baza                          │
│  ├── memories          — 1191+ records, vector(1024)        │
│  ├── entities          — entity registry                    │
│  ├── entity_aliases    — entity resolution                  │
│  ├── fact_entity_links — memory ↔ entity (M2M)             │
│  ├── memory_feedback   — trust scoring                      │
│  ├── user_profiles     — cross-session user models          │
│  └── daily_stats       — snapshots statystyk              │
├──────────────────────────────────────────────────────────────┤
│  SQLite (write queue) — crash-proof buffer                  │
│  └── pending_writes     — replay po restarcie                │
└──────────────────────────────────────────────────────────────┘

┌──────────┐   ┌──────────┐   ┌──────────────┐
│ Budy     │   │ Pi Agent │   │ Claude Code  │
│ (Hermes) │   │ (Z.AI)   │   │ (Win/Sonnet) │
└────┬─────┘   └────┬─────┘   └──────┬───────┘
     │              │                │
     └──────────────┼────────────────┘
                    │ HTTP
                    ▼
              Memory API :8766
```

---

## 3. Endpointy API

### Memory CRUD
| Metoda | Endpoint | Opis | Auth |
|--------|----------|------|------|
| POST | `/memories` | Dodaj pamięć (z embedding + dedup) | optional |
| GET | `/memories` | Lista z filtrami | optional |
| GET | `/memories/{id}` | Pojedyncza pamięć | optional |
| PUT | `/memories/{id}` | Aktualizuj pamięć | agent check |
| DELETE | `/memories/{id}` | Soft delete → `archived_at = NOW()` | agent check |

### Search
| Metoda | Endpoint | Opis | Auth |
|--------|----------|------|------|
| POST | `/search` | Hybrid: semantic (vector) + keyword (FTS) + RRF | optional |
| POST | `/search/compositional` | Multi-entity overlap (`?&` operator) | optional |
| GET | `/memories/{id}/related` | Powiązane (entity overlap + similarity) | optional |

### Profiles
| Metoda | Endpoint | Opis | Auth |
|--------|----------|------|------|
| GET | `/profiles` | Lista profili użytkowników | optional |
| GET | `/profiles/{user_id}` | Pojedynczy profil | optional |
| POST | `/profiles/{user_id}` | Create/update (JSONB merge) | optional |

### Admin & Hygiene
| Metoda | Endpoint | Opis | Auth |
|--------|----------|------|------|
| GET | `/health` | Health check (DB latency, embedding status) | ❌ |
| GET | `/stats` | Basic stats (active count) | optional |
| GET | `/memories/stats` | Enhanced stats (by agent/project/category) | optional |
| POST | `/memories/decay` | Temporal decay: `trust * 0.5^(age/365)` | optional |
| POST | `/extract-facts` | LLM auto-extract facts z tekstu | optional |
| POST | `/memories/{id}/feedback` | Feedback → trust_score update | optional |
| POST | `/hygiene/run` | Full hygiene cycle (contradictions) | optional |
| POST | `/entities/resolve` | Entity resolution pass | optional |
| DELETE | `/memories/purge` | Delete archived > N dni | optional |

### Write Queue
| Metoda | Endpoint | Opis | Auth |
|--------|----------|------|------|
| GET | `/queue/status` | Status write queue | optional |
| POST | `/queue/flush` | Manual flush pending writes | optional |

---

## 4. Model Danych

### memories
```sql
id UUID PK, content TEXT NOT NULL, embedding vector(1024),
agent_id VARCHAR(64), project_id VARCHAR(128), user_id VARCHAR(128),
category VARCHAR(32), tags TEXT[], metadata JSONB, source_file VARCHAR(512),
created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
trust_score REAL, importance REAL, archived_at TIMESTAMPTZ,
entities JSONB, linked_ids TEXT, access_count INTEGER,
memory_type VARCHAR(32), contradictions JSONB
```

### entities
```sql
entity_id SERIAL PK, name VARCHAR(256) UNIQUE,
entity_type VARCHAR(64), description TEXT, created_at TIMESTAMPTZ
```

### entity_aliases
```sql
alias_id SERIAL PK, entity_id INT REFERENCES entities(entity_id),
alias VARCHAR(256) UNIQUE
```

### fact_entity_links
```sql
link_id SERIAL PK, memory_id UUID REFERENCES memories(id) ON DELETE CASCADE,
entity_id INT REFERENCES entities(entity_id) ON DELETE CASCADE
```

### memory_feedback
```sql
feedback_id SERIAL PK, memory_id UUID REFERENCES memories(id) ON DELETE CASCADE,
agent_id VARCHAR(64), feedback_type VARCHAR(32),
comment TEXT, created_at TIMESTAMPTZ
```

### user_profiles
```sql
user_id VARCHAR(128) PK, profile_data JSONB, updated_at TIMESTAMPTZ
```

### daily_stats
```sql
id SERIAL PK, snapshot_date DATE, agent_id VARCHAR(64),
total_memories INT, active_memories INT, avg_trust_score REAL,
avg_importance REAL, new_today INT, archived_today INT, created_at TIMESTAMPTZ
```

---

## 5. Multi-Agent Izolacja

### Namespace by `agent_id`
- `budy`, `pi-agent`, `claude` — każdy ma swoją przestrzeń
- Search domyślnie: **cross-agent** (wszyscy agenci)
- `agent_id=X` filtruje do конкретного agenta
- PUT/DELETE: agent może modyfikować **tylko swoje** pamięci

### Rate Limiting (per-agent)
- 100 req/min (domyślnie)
- 10 concurrent embedding requests
- Max 500 pending writes (overflow → 429)

### Quoty
- Max memories/agent: 10 000 (warning at 80%)
- Max feedback/memory: 100
- Max pending writes/agent: 500

---

## 6. Nocne Operacje (Night Ops)

Hermes cronjob wywołuje endpointy w nocy gdy nikt nie pracuje na WSL2.

| Czas | Operacja | Endpoint | Opis |
|------|----------|----------|------|
| 02:00 | Decay | `POST /memories/decay` | `trust_score *= 0.5^(age/365)` dla < 0.8 |
| 02:30 | Contradiction | `POST /hygiene/run` | Znajdź sprzeczne fakty (shared entities + low similarity) |
| 03:00 | Entity Resolution | `POST /entities/resolve` | Merge duplicates z aliases |
| 03:30 | Trust Recalculation | `POST /hygiene/trust` | Recalc trust na podstawie feedback × recency |
| 04:00 | Archive Low Trust | `DELETE /memories/purge` | Archive: trust < 0.2 AND age > 90 dni |
| 04:30 | Purge Old Archive | `DELETE /memories/purge` | Delete archived > 30 dni (niedziele) |
| 05:00 | Cache Rebuild | `POST /cache/rebuild` | Pre-compute embeddingi common queries |
| 05:30 | Stats Snapshot | `POST /stats/snapshot` | Daily stats → `daily_stats` table |

**Monitoring:**
- Każdy ops → log do `/tmp/night-ops-YYYY-MM-DD.log`
- Fail → alert na Telegram
- Rollback: każdy ops jest transactional

---

## 7. Bezpieczeństwo

- `x-api-token` header (optional w dev, required w prod)
- Dev mode: token = `dev-token-change-me` → skip check
- Write operations: `agent_id` must match requester
- Backup: PostgreSQL dump co 6h → `/home/ArndtOs/backups/memory/`

---

## 8. Migracja v1 → v2

1. v1 (8765) i v2 (8766) równolegle
2. Testy na v2 z aktualną bazą
3. Switch port 8765 → v2
4. v1 fallback przez 7 dni

**Co zostaje z v1:** Wszystkie dane w `pgmemory` — nietknięte.

---

## 9. Struktura Kodu

```
memory-api-v2/
├── README.md              # Ten plik
├── docs/                  # Dodatkowa dokumentacja
│   ├── API.md             # Pełna specyfikacja endpointów
│   ├── NIGHT_OPS.md       # Harmonogram nocnych operacji
│   ├── MIGRATION.md       # Plan migracji z v1
│   └── ARCHITECTURE.md    # Diagramy i decyzje
├── core.py                # Config, auth, circuit breaker, embedding
├── db.py                  # asyncpg pool, init schema
├── embeddings.py          # Embedding cache (content_hash → vector)
├── entities.py            # Entity resolution + aliases
├── routes/
│   ├── memories.py        # CRUD + search endpoints
│   ├── profiles.py        # User profiles
│   ├── admin.py           # Health, stats, extract-facts
│   └── hygiene.py         # Contradictions, trust, decay, purge
├── workers/
│   ├── queue.py           # SQLite write queue → PG flush
│   └── extract.py         # Async fact extraction
├── main.py                # FastAPI app + lifespan
├── requirements.txt       # asyncpg, fastapi, httpx, python-dotenv, pydantic
├── .env                   # Konfiguracja (gitignored)
└── scripts/
    ├── setup.sh           # Install deps, setup DB
    ├── restart.sh         # Kill + start with proper env
    └── night-ops.sh       # Cronjob dispatcher
```

---

*Autor: Budy*
*Data: 2026-05-12*
*Status: Design — w trakcie implementacji*