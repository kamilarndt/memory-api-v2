# Memory API v2 vs Mem0 — Porównanie i Rekomendacje

## Wynik analizy swarm (2 agentów: Scout + Researcher)

### Obecny stan memory-api-v2

| Aspekt | memory-api-v2 | Mem0 | Wniosek |
|--------|---------------|------|---------|
| Storage | pgvector (PostgreSQL 16) | SQLite / pgvector / Qdrant / Chroma | ✅ Nasze lepsze — skalowalność |
| Embedding | Router → Gemini → OpenRouter → Ollama | Multi-LLM (OpenAI, Gemini, Ollama…) | ✅ Porównywalne |
| Dedup | DreamService LINK (cosine > 0.92) + CONSOLIDATE (LLM merge) | ADD-only, 1 LLM call, nic nie nadpisuje | ⚠️ Różne filozofie |
| Search | Hybrid vector + FTS (English/Polish) + trigram + RRF | Semantic + BM25 + entity (additive) | ⚠️ RRF vs additive scoring |
| Temporal | Batch decay (Dream DECAY), 1-year half-life | Time-aware ranking, `expires_at` | 🔑 **Luka** |
| Relacje | `contradictions` JSONB, `entities` JSONB | Graph relations (dedykowany store) | 🔑 **Luka** |
| Auto-extract | `POST /extract-facts` (jawnie) | SDK auto-extract z konwersacji | 🔑 **Luka** |
| Entity boost | Dream LINK (batch) | Entity linking w runtime + boost retrieval | 🔑 **Luka** |

### 4 rekomendowane ulepszenia (priorytetowo)

| # | Ulepszenie | Koszt | Zysk | Pliki |
|---|-----------|-------|------|-------|
| 1 | **Smart inline dedup** — zamiast odrzucać duplikat, merguj treść + update `updated_at` | 🔴 15 min | Zero duplikatów, bogatsze treści | `routes/memories.py` |
| 2 | **`expires_at` + expiry cleanup** — per-fact TTL, auto-archiwizacja | 🟡 30 min | Automatyczne czyszczenie nieaktualnych faktów | `db.py` (migration), `routes/memories.py`, `services/dream.py` |
| 3 | **Auto-extract jako opcja** — flag `extract: true` przy add_memory → LLM wyciąga encje | 🟢 20 min | Wzbogacanie faktów przy zapisie | `routes/memories.py` |
| 4 | **Tabela memory_relations** — source_id → target_id → relation_type | ⚪ 45 min | Grafowe query: "jakie decyzje podjąłem w kontekście projektu X?" | Nowa migracja + `routes/relations.py` |

### Czego NIE robić (z Mem0)
- User/Session/Agent scope hierarchy — overkill, mamy agent_id + user_id + project_id płasko
- Memory diff history — overkill jak na pamięć agenta
- Graph memory paywalled ($249/mc) — nie potrzebny
- SQLite backend — nasz pgvector jest lepszy
- ADD-only (no updates) — nasz CRUD + soft-delete jest bardziej elastyczny
- spaCy entity extraction — DreamService LLM jest dokładniejszy

### Największy zysk za najmniejszy koszt

**#1 Smart inline dedup** (15 minut):
- W `_check_duplicate()` zamiast `return {"status": "duplicate"}` → UPDATE existing memory, append różnic, update `updated_at`, inkrement `access_count`
- Eliminuje duplikaty na wejściu, a nie dopiero w Dream batchu
