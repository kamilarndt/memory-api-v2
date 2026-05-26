"""Memory API v2 — Core: Config, auth, circuit breaker, embedding."""

from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx
from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass
class Config:
    """All configuration loaded once at startup from environment variables."""
    db_host: str = field(default_factory=lambda: os.environ.get("PGMEMORY_DB_HOST", "localhost"))
    db_port: int = field(default_factory=lambda: int(os.environ.get("PGMEMORY_DB_PORT", "5432")))
    db_name: str = field(default_factory=lambda: os.environ.get("PGMEMORY_DB_NAME", "pgmemory"))
    db_user: str = field(default_factory=lambda: os.environ.get("PGMEMORY_DB_USER", "postgres"))
    db_password: str = field(default_factory=lambda: os.environ.get("PGMEMORY_DB_PASSWORD", ""))
    openrouter_key: str = field(default_factory=lambda: os.environ.get("OPENROUTER_API_KEY", ""))
    api_token: str = field(default_factory=lambda: os.environ.get("MEMORY_API_TOKEN", "dev-token-change-me"))
    vault_path: str = field(default_factory=lambda: os.environ.get("VAULT_PATH", "/home/ArndtOs/vault"))
    gemini_key: str = field(default_factory=lambda: os.environ.get("GEMINI_EMBED_KEY", ""))
    ollama_url: str = field(default_factory=lambda: os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    embed_model: str = field(default_factory=lambda: os.environ.get("EMBED_MODEL", "nomic-embed-text"))
    embed_dim: int = field(default_factory=lambda: int(os.environ.get("EMBED_DIM", "1536")))
    router_key: str = field(default_factory=lambda: os.environ.get("ROUTER_API_KEY", "sk-rtr-78770c19-d6c7-4fa8-b083-49346a367ec1"))
    router_url: str = field(default_factory=lambda: os.environ.get("ROUTER_URL", "http://127.0.0.1:18881/v1"))
    router_model: str = field(default_factory=lambda: os.environ.get("ROUTER_EMBED_MODEL", "openai/text-embedding-3-small"))

    def __post_init__(self):
        if not isinstance(self.db_port, int):
            self.db_port = int(self.db_port)

    @property
    def dsn(self) -> str:
        pw = self.db_password
        return f"postgresql://{self.db_user}:{pw}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def dsn_display(self) -> str:
        """For logging — mask the password."""
        pw = "***" if self.db_password else ""
        return f"postgresql://{self.db_user}:{pw}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def is_dev(self) -> bool:
        return self.api_token in ("", "dev-token-change-me")


# Singleton — loaded once
_config: Optional[Config] = None


def get_config() -> Config:
    global _config
    if _config is None:
        from dotenv import load_dotenv
        load_dotenv()
        _config = Config()
    return _config


# ── Auth ─────────────────────────────────────────────────────────────────────

def verify_token(
    x_api_token: str = Header(default=None, alias="x-api-token"),
) -> None:
    """Require valid API token. No-op in dev mode."""
    config = get_config()
    if config.is_dev:
        return
    if x_api_token != config.api_token:
        raise HTTPException(401, "Unauthorized: invalid or missing x-api-token")


async def verify_token_optional(
    x_api_token: str = Header(default=None, alias="x-api-token"),
) -> Optional[str]:
    """Allow access without token, but validate if one is provided."""
    config = get_config()
    if config.is_dev:
        return None
    if x_api_token and x_api_token == config.api_token:
        return "authenticated"
    if not x_api_token:
        return None
    raise HTTPException(401, "Unauthorized: invalid x-api-token")


# ── Circuit Breaker ──────────────────────────────────────────────────────────

class CircuitBreaker:
    """Simple circuit breaker for external API calls.

    closed → open after failure_threshold failures.
    open → half-open after cooldown seconds.
    half-open → closed on next success, open on next failure.
    """

    def __init__(self, failure_threshold: int = 5, cooldown: float = 120.0):
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self.failures = 0
        self.last_failure_time = 0.0
        self.state = "closed"

    def can_execute(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.time() - self.last_failure_time > self.cooldown:
                self.state = "half-open"
                return True
            return False
        return True  # half-open

    def record_success(self) -> None:
        self.failures = 0
        self.state = "closed"

    def record_failure(self) -> None:
        self.failures += 1
        self.last_failure_time = time.time()
        if self.failures >= self.failure_threshold:
            self.state = "open"
            logger.warning(
                "Circuit breaker OPEN: %d failures, cooldown %.0fs",
                self.failures, self.cooldown,
            )

    def status(self) -> dict:
        remaining = 0.0
        if self.state == "open":
            remaining = max(0, self.cooldown - (time.time() - self.last_failure_time))
        return {
            "state": self.state,
            "failures": self.failures,
            "cooldown_remaining": remaining,
        }


# Global breaker for embedding API calls
embedding_breaker = CircuitBreaker()


# ── Embedding ────────────────────────────────────────────────────────────────

async def get_embedding(text: str, config: Optional[Config] = None) -> Optional[list[float]]:
    """Get embedding vector from Router (primary, 1536d) with Gemini fallback.

    Chain: Router → Gemini → OpenRouter → Ollama
    Router provides 1536d embeddings via OpenAI-compatible endpoint.
    """
    config = config or get_config()

    if not embedding_breaker.can_execute():
        logger.debug("Embedding circuit breaker is open, skipping")
        return None

    # Strategy 1: Router (OpenAI-compatible, 1536d)
    if config.router_key:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{config.router_url}/embeddings",
                    headers={"Authorization": f"Bearer {config.router_key}"},
                    json={"model": config.router_model, "input": text, "dimensions": 1024},
                )
                resp.raise_for_status()
                data = resp.json()
                emb = data.get("data", [{}])[0].get("embedding")
                if emb:
                    embedding_breaker.record_success()
                    # Truncate to 1024d to match DB schema
                    if len(emb) > 1024:
                        emb = emb[:1024]
                    return emb
        except Exception as e:
            logger.warning("Router embedding failed: %s", e)
            embedding_breaker.record_failure()

    # Strategy 2: Gemini (3072d raw → output_dimensionality=1536 to match DB)
    if config.gemini_key:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={config.gemini_key}",
                    json={
                        "model": "models/gemini-embedding-001",
                        "content": {"parts": [{"text": text}]},
                        "output_dimensionality": 1536,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                emb = data.get("embedding", {}).get("values")
                if emb:
                    embedding_breaker.record_success()
                    return emb
        except Exception as e:
            logger.warning("Gemini embedding failed: %s", e)
            embedding_breaker.record_failure()

    # Strategy 3: OpenRouter (baai/bge-m3, 1024d)
    if config.openrouter_key:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/embeddings",
                    headers={"Authorization": f"Bearer {config.openrouter_key}"},
                    json={"model": "baai/bge-m3", "input": text},
                )
                resp.raise_for_status()
                data = resp.json()
                emb = data.get("data", [{}])[0].get("embedding")
                if emb:
                    embedding_breaker.record_success()
                    # Truncate to 1024d to match DB schema
                    if len(emb) > 1024:
                        emb = emb[:1024]
                    return emb
        except Exception as e:
            logger.warning("OpenRouter embedding failed: %s", e)

    # Strategy 4: Ollama fallback
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{config.ollama_url}/api/embeddings",
                json={"model": config.embed_model, "prompt": text},
            )
            resp.raise_for_status()
            emb = resp.json().get("embedding")
            if emb:
                embedding_breaker.record_success()
                return emb
    except Exception as e:
        logger.warning("Ollama embedding failed: %s", e)

    # All failed
    embedding_breaker.record_failure()
    return None
