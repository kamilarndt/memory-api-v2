---
name: memory
description: "Memory API v2 — PostgreSQL-backed persistent memory with semantic search (pgvector + Gemini). Use memory_add/memory_search/memory_list to store and retrieve cross-session facts, preferences, lessons, and project patterns."
category: memory
---

# Memory API v2 — Współdzielona Pamięć (pgvector + Gemini)

## Użyj gdy

Szukasz informacji z przeszłych sesji, chcesz zapamiętać fakt, potrzebujesz kontekstu projektu, lub chcesz zarządzać pamięcią.

## Narzędzia

| Tool | Opis |
|------|------|
| `memory_add` | Zapisz fakt, preferencję, lekcję (z kluczem `pref.*`, `lesson.*`, `project.*`) |
| `memory_search` | Szukaj semantycznie (hybrid: vector + FTS + RRF) |
| `memory_list` | Przeglądaj fakty z filtrami i paginacją |
| `memory_stats` | Statystyki pamięci (wg agenta, projektu, kategorii) |
| `memory_forget` | Zarchiwizuj fakt po ID (soft delete) |
| `memory_related` | Znajdź powiązane fakty (entity overlap + similarity) |
| `memory_feedback` | Oceń fakt (helpful/not_helpful/incorrect/outdated) → trust_score |
| `memory_extract` | Auto-wyodrębnij fakty z tekstu przez LLM |
| `memory_profile` | Zarządzaj profilami użytkowników |

## Komendy

| Komenda | Opis |
|---------|------|
| `/memory-health` | Sprawdź stan Memory API |
| `/memory-sync` | Wymuś sync oczekujących faktów |

## Przykłady

```
memory_add("Kamil preferuje asyncpg", memory_type="pref", key="database", importance=7)
memory_search("jakie preferencje ma Kamil", min_score=0.5)
memory_related(id="uuid-znaleziony-w-search")
memory_feedback(id="uuid", feedback_type="helpful", comment="dobrze zapamiętane")
memory_extract("Długa rozmowa o architekturze...")
memory_profile(action="update", user_id="kamil", data='{"preferred_db": "asyncpg"}')
```
