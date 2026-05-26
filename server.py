"""
Memory API v2 — Lightweight FastAPI server with SQLite backend.

Provides memory recall/store endpoints for Hermes plugin hooks,
a NotebookLM bridge endpoint, and standard health/openapi routes.

Runs on port 8765 with CORS enabled for local plugin access.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger = logging.getLogger("memory-api-v2")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).resolve().parent / "memory.db"

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class RecallRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Search query for memory recall")
    session_id: str | None = Field(
        None, description="Optional session ID to scope the recall"
    )


class RecallResult(BaseModel):
    id: str
    session_id: str
    user_message: str
    assistant_response: str
    created_at: str
    score: float = 0.0


class RecallResponse(BaseModel):
    results: list[RecallResult]


class StoreRequest(BaseModel):
    session_id: str = Field(..., min_length=1, description="Session identifier")
    user: str = Field(..., min_length=1, description="User message content")
    assistant: str = Field(..., min_length=1, description="Assistant response content")


class StoreResponse(BaseModel):
    status: str


class NotebookLMRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Question to ask NotebookLM")


class NotebookLMResponse(BaseModel):
    answer: str
    sources: list[dict[str, Any]]


class HealthResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

SQL_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    user_message TEXT NOT NULL,
    assistant_response TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

SQL_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_memories_session_id ON memories(session_id);
"""

SQL_INSERT_MEMORY = """
INSERT INTO memories (id, session_id, user_message, assistant_response, created_at)
VALUES (?, ?, ?, ?, ?);
"""

SQL_SEARCH_MEMORIES = """
SELECT id, session_id, user_message, assistant_response, created_at
FROM memories
WHERE (user_message LIKE ? OR assistant_response LIKE ?)
  AND (? IS NULL OR session_id = ?)
ORDER BY created_at DESC
LIMIT ?;
"""

SQL_SEARCH_ALL = """
SELECT id, session_id, user_message, assistant_response, created_at
FROM memories
ORDER BY created_at DESC
LIMIT ?;
"""


async def init_db() -> aiosqlite.Connection:
    """Create and return a new database connection with schema initialized."""
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute(SQL_CREATE_TABLE)
    await db.execute(SQL_CREATE_INDEX)
    await db.commit()
    logger.info("Database initialized at %s", DB_PATH)
    return db


async def store_memory(
    db: aiosqlite.Connection,
    session_id: str,
    user_message: str,
    assistant_response: str,
) -> str:
    """Insert a new memory and return its ID."""
    memory_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    await db.execute(
        SQL_INSERT_MEMORY,
        (memory_id, session_id, user_message, assistant_response, created_at),
    )
    await db.commit()
    logger.info("Stored memory %s for session %s", memory_id, session_id)
    return memory_id


async def recall_memories(
    db: aiosqlite.Connection,
    query: str,
    session_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search memories by keyword matching on user_message and assistant_response.

    Results are ordered by most recent first. A simple scoring heuristic
    prioritises exact matches and recency.
    """
    like_pattern = f"%{query}%"

    if session_id:
        cursor = await db.execute(
            SQL_SEARCH_MEMORIES,
            (like_pattern, like_pattern, session_id, session_id, limit),
        )
    else:
        cursor = await db.execute(
            SQL_SEARCH_ALL,
            (limit,),
        )

    rows = await cursor.fetchall()
    results = []
    for row in rows:
        row_dict = dict(row)
        # Simple relevance score: 1.0 for exact substring match, decaying
        content = f"{row_dict['user_message']} {row_dict['assistant_response']}"
        score = _compute_relevance_score(query, content)
        row_dict["score"] = round(score, 4)
        results.append(row_dict)

    # Re-sort by score descending when no session filter (all results returned)
    if not session_id:
        results.sort(key=lambda r: r["score"], reverse=True)

    return results


def _compute_relevance_score(query: str, content: str) -> float:
    """Compute a simple relevance score (0-1) for a memory vs query.

    Uses a basic TF-like heuristic: counts query term occurrences,
    penalises long content, and rewards exact phrase presence.
    """
    query_lower = query.lower().strip()
    content_lower = content.lower()

    if not query_lower or not content_lower:
        return 0.0

    # Exact phrase match gets a big boost
    if query_lower in content_lower:
        base_score = 0.8
    else:
        base_score = 0.0

    # Token overlap
    query_tokens = set(query_lower.split())
    content_tokens = content_lower.split()
    content_token_set = set(content_tokens)

    if query_tokens:
        overlap = query_tokens & content_token_set
        token_score = len(overlap) / len(query_tokens) * 0.4
    else:
        token_score = 0.0

    # Frequency bonus for repeated important terms
    freq_bonus = 0.0
    for token in query_tokens:
        if len(token) > 3:
            count = content_tokens.count(token)
            if count > 1:
                freq_bonus = min(0.1, freq_bonus + 0.02 * (count - 1))

    return min(1.0, base_score + token_score + freq_bonus)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: initialise database. Shutdown: close connection."""
    logger.info("Memory API v2 starting up ...")
    db = await init_db()
    app.state.db = db
    yield
    await db.close()
    logger.info("Memory API v2 shut down.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Memory API v2",
    version="2.0.0",
    description=(
        "Lightweight memory recall and storage service for Hermes plugin hooks. "
        "Provides keyword-based memory retrieval, conversation history storage, "
        "and a NotebookLM bridge endpoint."
    ),
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all exception handler returning a structured JSON error."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "path": str(request.url.path)},
    )


# ---------------------------------------------------------------------------
# Request timing middleware
# ---------------------------------------------------------------------------


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """Log request duration for observability."""
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    response.headers["X-Process-Time-Ms"] = str(round(elapsed * 1000, 2))
    logger.debug(
        "%s %s -> %s (%.2fms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed * 1000,
    )
    return response


# ---------------------------------------------------------------------------
# Routes — Health & Discovery
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/openapi.json", tags=["system"])
async def openapi_json():
    """Return the OpenAPI specification."""
    return app.openapi()


# ---------------------------------------------------------------------------
# Routes — Memory v2
# ---------------------------------------------------------------------------


@app.post(
    "/v2/memory/recall",
    response_model=RecallResponse,
    tags=["memory"],
    summary="Recall memories by query",
    description=(
        "Search stored memories by keyword match against user and assistant messages. "
        "Optionally scope to a session_id. Results are scored by relevance and recency."
    ),
)
async def memory_recall(req: RecallRequest):
    """Recall memories matching the given query."""
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    db: aiosqlite.Connection = app.state.db
    try:
        results = await recall_memories(db, req.query, session_id=req.session_id)
    except Exception:
        logger.exception("Failed to recall memories for query: %s", req.query[:100])
        raise HTTPException(status_code=500, detail="Memory recall failed")

    return RecallResponse(
        results=[
            RecallResult(
                id=r["id"],
                session_id=r["session_id"],
                user_message=r["user_message"],
                assistant_response=r["assistant_response"],
                created_at=r["created_at"],
                score=r["score"],
            )
            for r in results
        ]
    )


@app.post(
    "/v2/memory/store",
    response_model=StoreResponse,
    tags=["memory"],
    summary="Store a conversation memory",
    description="Persist a user/assistant exchange into the memory store.",
)
async def memory_store(req: StoreRequest):
    """Store a new conversation memory."""
    db: aiosqlite.Connection = app.state.db
    try:
        await store_memory(db, req.session_id, req.user, req.assistant)
    except Exception:
        logger.exception("Failed to store memory for session: %s", req.session_id)
        raise HTTPException(status_code=500, detail="Failed to store memory")

    return StoreResponse(status="stored")


# ---------------------------------------------------------------------------
# Routes — NotebookLM Bridge
# ---------------------------------------------------------------------------


@app.post(
    "/notebooklm-ask",
    response_model=NotebookLMResponse,
    tags=["notebooklm"],
    summary="Ask a question via NotebookLM bridge",
    description=(
        "Query the NotebookLM bridge for an answer with sources. "
        "Falls back to stored memories if the bridge is unavailable."
    ),
)
async def notebooklm_ask(req: NotebookLMRequest):
    """Ask a question, returning an answer and supporting sources.

    This endpoint attempts to use stored memories as context, providing
    a response synthesised from recalled information. In production this
    would call an external NotebookLM API; here it queries local memory.
    """
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    db: aiosqlite.Connection = app.state.db

    try:
        # Recall relevant memories
        memories = await recall_memories(db, req.query, limit=10)

        if memories:
            # Synthesise answer from top memories
            best = memories[0]
            top_pairs = memories[:3]

            answer = (
                f"Based on stored conversation history, here is what I found:\n\n"
                f"Most relevant exchange:\n"
                f"  User: {best['user_message'][:300]}"
                f"{'...' if len(best['user_message']) > 300 else ''}\n"
                f"  Assistant: {best['assistant_response'][:300]}"
                f"{'...' if len(best['assistant_response']) > 300 else ''}\n"
            )

            if len(top_pairs) > 1:
                answer += (
                    f"\n(Plus {len(top_pairs) - 1} additional relevant exchanges.)"
                )

            sources = [
                {
                    "memory_id": m["id"],
                    "session_id": m["session_id"],
                    "excerpt": f"{m['user_message'][:150]} / {m['assistant_response'][:150]}",
                    "relevance_score": m["score"],
                    "created_at": m["created_at"],
                }
                for m in top_pairs
            ]
        else:
            answer = (
                "I don't have any stored memories matching that query. "
                "Try storing some conversations first via /v2/memory/store."
            )
            sources = []

        return NotebookLMResponse(answer=answer, sources=sources)

    except HTTPException:
        raise
    except Exception:
        logger.exception("NotebookLM ask failed for query: %s", req.query[:100])
        raise HTTPException(status_code=500, detail="NotebookLM query failed")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Memory API v2 on 0.0.0.0:8765")
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8765,
        reload=False,
        log_level="info",
    )
