# Rejestracja narzędzi Memory API v2 w Hermesie

## Sposób 1: Bezpośrednia rejestracja (najprostsza)

W pliku konfiguracyjnym Hermesa lub przy starcie dodaj:

```python
from memory_api_v2.hermes.tool import (
    memory_remember,
    memory_recall,
    memory_search,
    memory_feedback,
)

# Rejestracja w Hermes tool registry
registry.register_tool("memory_remember", memory_remember)
registry.register_tool("memory_recall", memory_recall)
registry.register_tool("memory_search", memory_search)
registry.register_tool("memory_feedback", memory_feedback)
```

## Sposób 2: Przez Skill

1. Skopiuj folder `hermes/` do `~/.hermes/skills/memory-api-v2/`
2. Załaduj skill: `/skill memory-api-v2`
3. Hermes powinien automatycznie zobaczyć narzędzia

## Zmienne środowiskowe

Możesz nadpisać URL w `.env`:

```bash
MEMORY_API_V2_URL=http://localhost:8765
```

## Status

Narzędzia gotowe do rejestracji. Klient jest w pełni asynchroniczny.