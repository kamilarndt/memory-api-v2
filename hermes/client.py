"""
Memory API v2 - Hermes Client
Lekki, asynchroniczny klient zoptymalizowany pod kątem Hermesa.
"""

import os
import httpx
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("MEMORY_API_V2_URL", "http://localhost:8765")
DEFAULT_AGENT_ID = "hermes"


class MemoryV2Client:
    def __init__(self, base_url: str = BASE_URL, agent_id: str = DEFAULT_AGENT_ID):
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.client = httpx.AsyncClient(timeout=30.0)

    async def remember(
        self,
        content: str,
        project_id: Optional[str] = None,
        category: Optional[str] = None,
        importance: float = 0.7,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Zapisuje nową pamięć."""
        payload = {
            "content": content,
            "agent_id": self.agent_id,
            "project_id": project_id or "",
            "category": category or "general",
            "importance": importance,
            "tags": tags or [],
            "metadata": metadata or {},
        }
        resp = await self.client.post(f"{self.base_url}/memories", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def recall(
        self,
        query: str,
        project_id: Optional[str] = None,
        limit: int = 8,
        cross_agent: bool = True,
        min_confidence: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Hybrydowe wyszukiwanie (semantic + keyword RRF). Domyślnie cross-agent."""
        payload = {
            "query": query,
            "limit": limit,
            "cross_agent": cross_agent,
            "min_confidence": min_confidence,
        }
        if project_id:
            payload["project_id"] = project_id

        resp = await self.client.post(f"{self.base_url}/search", json=payload)
        resp.raise_for_status()
        return resp.json().get("results", [])

    async def search(
        self,
        query: str,
        project_id: Optional[str] = None,
        limit: int = 10,
        **kwargs
    ) -> List[Dict[str, Any]]:
        """Zaawansowane wyszukiwanie."""
        payload = {
            "query": query,
            "limit": limit,
            **kwargs
        }
        if project_id:
            payload["project_id"] = project_id

        resp = await self.client.post(f"{self.base_url}/search", json=payload)
        resp.raise_for_status()
        return resp.json().get("results", [])

    async def feedback(
        self,
        memory_id: str,
        feedback_type: str = "helpful",
    ) -> Dict[str, Any]:
        """Daje feedback na konkretną pamięć."""
        payload = {
            "agent_id": self.agent_id,
            "feedback_type": feedback_type
        }
        resp = await self.client.post(
            f"{self.base_url}/memories/{memory_id}/feedback",
            json=payload
        )
        resp.raise_for_status()
        return resp.json()

    async def get_related(self, memory_id: str, limit: int = 5):
        resp = await self.client.get(
            f"{self.base_url}/memories/{memory_id}/related?limit={limit}"
        )
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        await self.client.aclose()

    # =====================
    # Zaawansowane funkcje
    # =====================

    async def extract_facts(
        self,
        text: str,
        project_id: Optional[str] = None,
        auto_save: bool = True,
    ) -> Dict[str, Any]:
        """Wyodrębnia fakty z tekstu przy użyciu LLM i opcjonalnie zapisuje je jako wspomnienia."""
        payload = {
            "text": text,
            "agent_id": self.agent_id,
            "project_id": project_id or "",
            "auto_save": auto_save,
        }
        resp = await self.client.post(f"{self.base_url}/extract-facts", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def compositional_search(
        self,
        entities: List[str],
        project_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Wyszukiwanie z wieloma encjami (logika AND).
        Przykład: entities=["Rust", "async", "tokio"]
        """
        payload = {
            "entities": entities,
            "limit": limit,
        }
        if project_id:
            payload["project_id"] = project_id

        resp = await self.client.post(f"{self.base_url}/search/compositional", json=payload)
        resp.raise_for_status()
        return resp.json().get("results", [])

    async def hygiene_run(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """Uruchamia detekcję sprzeczności i niskiej jakości wspomnień."""
        payload = {}
        if project_id:
            payload["project_id"] = project_id

        resp = await self.client.post(f"{self.base_url}/hygiene/run", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def resolve_entities(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """Scala duplikaty encji."""
        payload = {}
        if project_id:
            payload["project_id"] = project_id

        resp = await self.client.post(f"{self.base_url}/entities/resolve", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def get_stats(self) -> Dict[str, Any]:
        """Zwraca statystyki pamięci."""
        resp = await self.client.get(f"{self.base_url}/memories/stats")
        resp.raise_for_status()
        return resp.json()
