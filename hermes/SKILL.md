---
name: memory-api-v2
description: "Integracja z Memory API v2 — długoterminowa pamięć semantyczna dla Hermesa + automatyczne zachowania"
version: 0.3.0
author: ArndtOs
tags: [memory, hermes, long-term, semantic, lazy-loading, notebooklm]
updated: 2026-05-19
---

# Memory API v2 Skill (Hermes) — v0.3.0

**Główna warstwa długoterminowej pamięci semantycznej** w ArndtOs. Integracja jest naturalna, automatyczna i token-efficient.

## Zmiany w wersji 0.3.0
- Dodano **lazy loading** (tool ładowany tylko gdy potrzebny)
- Wprowadzono **automatyczne zachowania** (`memory_extract_facts` + `memory_feedback`)
- Lepsza integracja komplementarna z NotebookLM
- Udoskonalone kategorie i wytyczne użycia

## Dostępne narzędzia (hermes/tool.py)

### Podstawowe
- `memory_remember(content, project_id?, category?, importance=0.7, tags?)`
- `memory_recall(query, project_id?, limit=8)`
- `memory_search(query, project_id?, limit=10)`
- `memory_feedback(memory_id, feedback_type="helpful")`

### Zaawansowane / Automatyczne
- `memory_extract_facts(text, project_id?, auto_save=True)` ← **używane automatycznie**
- `memory_compositional_search(entities[], project_id?, limit=10)`
- `memory_hygiene_run(project_id?)`
- `memory_resolve_entities(project_id?)`
- `memory_get_stats()`

## AUTOMATYCZNE ZACHOWANIA (nowość)

**Hermes ma teraz następujące automatyczne reguły:**

1. **Po większych blokach researchu, analizy lub ważnej konwersacji** — automatycznie wywołuj `memory_extract_facts(text, project_id="ArndtOs", auto_save=True)`
2. **Po każdym użyciu `memory_recall` lub `memory_search`** — jeśli wynik był przydatny, automatycznie wyślij `memory_feedback(memory_id, "helpful")`
3. **Po zakończeniu złożonego zadania** (5+ tool calls) — rozważ zapisanie kluczowych wniosków przez `memory_remember` lub `memory_extract_facts`
4. **Raz na kilka sesji** — uruchamiaj `memory_hygiene_run()` i `memory_resolve_entities()`

**Lazy Loading**: Narzędzia pamięci nie są ładowane na starcie sesji. Ładuj je dopiero gdy są potrzebne (`skill_view("hermes-memory-integration")` + `skill_view("memory-api-v2")`).

## Integracja z NotebookLM

Memory API v2 i NotebookLM są **komplementarne**:
- **Memory API v2** → szybkie, semantyczne, cross-agent, z feedback loop i hygiene
- **NotebookLM** → bogate, narracyjne, per-project/per-topic knowledge base (dokumentacja agentów, głębokie konteksty)

**Zalecany wzorzec:**
- Najważniejsze fakty/decyzje/preferencje → Memory API v2
- Pełna dokumentacja, historie projektowe, instrukcje agentów → NotebookLM
- Hermes powinien naturalnie korzystać z obu warstw

## Zalecane kategorie (aktualne)

- `user-preference`
- `project-decision`
- `architecture`
- `lesson-learned`
- `technical`
- `sop`
- `agent-behavior`
- `research-insight`
- `test`
- `integration`

## Wzorce użycia

**Automatyczne (preferowane):**
- Po researchu → `memory_extract_facts(...)`
- Po użyciu recall → `memory_feedback(...)` jeśli przydatne

**Ręczne:**
- `memory_remember()` przy kluczowych decyzjach użytkownika
- `memory_compositional_search()` przy złożonych zapytaniach
- `memory_get_stats()` do monitorowania

## Uwagi ważne

- Pamięć jest współdzielona między Hermes, Claude Code, Pi-Agent, Budy itd.
- System automatycznie zarządza `trust_score` na podstawie feedbacku
- **Nie zapisuj wszystkiego** — tylko wartościowe, trwałe informacje
- Używaj `project_id="ArndtOs"` lub konkretnego projektu gdy to możliwe

---
**Ostatnia aktualizacja:** 19 maja 2026
Cel: naturalna, automatyczna, token-efficient pamięć długoterminowa w ekosystemie ArndtOs.
