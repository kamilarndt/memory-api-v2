"""
Memory API v2 Tool dla Hermesa
Gotowa definicja narzędzi do rejestracji.
"""

from typing import Any, Dict, List, Optional
from .client import MemoryV2Client

# Globalny klient (można też tworzyć per-sesja)
_client: Optional[MemoryV2Client] = None


def get_client() -> MemoryV2Client:
    global _client
    if _client is None:
        _client = MemoryV2Client()
    return _client


# === Podstawowe narzędzia ===

async def memory_remember(
    content: str,
    project_id: Optional[str] = None,
    category: Optional[str] = None,
    importance: float = 0.7,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Zapisuje nową informację do długoterminowej pamięci."""
    client = get_client()
    return await client.remember(
        content=content,
        project_id=project_id,
        category=category,
        importance=importance,
        tags=tags,
    )


async def memory_recall(
    query: str,
    project_id: Optional[str] = None,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """Wyszukuje relevantne wspomnienia z pamięci długoterminowej."""
    client = get_client()
    return await client.recall(query=query, project_id=project_id, limit=limit)


async def memory_search(
    query: str,
    project_id: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Zaawansowane wyszukiwanie w pamięci."""
    client = get_client()
    return await client.search(query=query, project_id=project_id, limit=limit)


async def memory_feedback(
    memory_id: str,
    feedback_type: str = "helpful",
) -> Dict[str, Any]:
    """Daje feedback na konkretną pamięć (wpływa na trust_score)."""
    client = get_client()
    return await client.feedback(memory_id=memory_id, feedback_type=feedback_type)


# === Zaawansowane narzędzia ===

async def memory_extract_facts(
    text: str,
    project_id: Optional[str] = None,
    auto_save: bool = True,
) -> Dict[str, Any]:
    """Automatycznie wyciąga fakty z tekstu i opcjonalnie zapisuje je jako wspomnienia."""
    client = get_client()
    return await client.extract_facts(text=text, project_id=project_id, auto_save=auto_save)


async def memory_compositional_search(
    entities: List[str],
    project_id: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Wyszukiwanie z wieloma encjami (logika AND)."""
    client = get_client()
    return await client.compositional_search(entities=entities, project_id=project_id, limit=limit)


async def memory_hygiene_run(
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Uruchamia proces czyszczenia i wykrywania sprzeczności w pamięci."""
    client = get_client()
    return await client.hygiene_run(project_id=project_id)


async def memory_resolve_entities(
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Scala duplikaty encji w pamięci."""
    client = get_client()
    return await client.resolve_entities(project_id=project_id)


async def memory_get_stats() -> Dict[str, Any]:
    """Zwraca statystyki pamięci."""
    client = get_client()
    return await client.get_stats()
