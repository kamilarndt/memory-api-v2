# Memory API v2 — Hermes Integration

Dedykowana integracja Memory API v2 z Hermes Agent.

## Cel

Umożliwić Hermesowi naturalne, wygodne i wydajne korzystanie z Memory API v2 jako jednej z głównych warstw pamięci długoterminowej (obok pgvector vault i MemPalace).

## Struktura

- `client.py` — asynchroniczny klient z wygodnymi metodami
- `tool.py` — definicja narzędzi gotowa do rejestracji w Hermesie
- `SKILL.md` — instrukcje i wytyczne dla Hermesa

## Dostępne narzędzia

### Podstawowe

| Narzędzie              | Opis                                      |
|------------------------|-------------------------------------------|
| `memory_remember`      | Zapisuje nową pamięć                      |
| `memory_recall`        | Wyszukiwanie semantyczne                  |
| `memory_search`        | Zaawansowane wyszukiwanie                 |
| `memory_feedback`      | Feedback na konkretną pamięć              |

### Zaawansowane

| Narzędzie                     | Opis                                              |
|-------------------------------|---------------------------------------------------|
| `memory_extract_facts`        | Automatyczne wyciąganie faktów z tekstu           |
| `memory_compositional_search` | Wyszukiwanie z wieloma encjami (logika AND)       |
| `memory_hygiene_run`          | Detekcja sprzeczności i czyszczenie pamięci       |
| `memory_resolve_entities`     | Scalanie duplikatów encji                         |
| `memory_get_stats`            | Statystyki pamięci                                |

## Rekomendowane użycie

Hermes powinien traktować Memory API v2 jako **drugą warstwę pamięci** po lokalnym pgvectorze vaultu. Najlepiej sprawdza się przy:

- Wiedzy projektowej
- Preferencjach użytkownika
- Lekcjach i wnioskach z poprzednich sesji
- Informacjach, które mają żyć dłużej niż jedna sesja

## Status

Integracja w pełni rozbudowana (2026-05-19).