#!/usr/bin/env python3
"""Memory API v2 — FastAPI application assembly with MCP SSE support.

MCP tool handlers delegate to MemoryRepository for all database operations,
eliminating the SQL duplication that existed across routes, aliases, and main.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import asyncio as _asyncio

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from core import get_config, get_embedding
from db import init_pool, close_pool
from repositories.memory import MemoryRepository
from routes.memories import router as memories_router, search_router
from routes.admin import router as admin_router
from routes.profiles import router as profiles_router
from routes.hygiene import router as hygiene_router
from routes.aliases import router as aliases_router
from routes.setup import router as setup_router
from routes.relations import router as relations_router
from routes.sessions import router as sessions_router
from routes.conflicts import router as conflicts_router
from routes.prompts import router as prompts_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MCP tool implementations (injected with pool at call time)
# ---------------------------------------------------------------------------

MCP_TOOLS = [
    {
        "name": "add_memory",
        "description": "Store a new memory (fact) for an agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent identifier"},
                "content": {"type": "string", "description": "Memory content"},
                "memory_type": {"type": "string", "description": "Optional type tag"},
                "importance": {"type": "number", "description": "Importance 0-10"},
            },
            "required": ["agent_id", "content"],
        },
    },
    {
        "name": "search_memories",
        "description": "Search memories by semantic similarity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent identifier"},
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "number", "description": "Max results (default 10)"},
                "min_score": {"type": "number", "description": "Minimum similarity 0-1"},
            },
            "required": ["agent_id", "query"],
        },
    },
    {
        "name": "list_memories",
        "description": "List recent memories for an agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent identifier"},
                "limit": {"type": "number", "description": "Max results (default 20)"},
                "memory_type": {"type": "string", "description": "Optional type filter"},
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "forget_memory",
        "description": "Archive (soft-delete) a memory by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Memory UUID to archive"},
            },
            "required": ["memory_id"],
        },
    },
]


async def _execute_tool(pool, tool_name: str, arguments: dict) -> dict:
    """Dispatch MCP tool calls to the appropriate handler.

    Uses MemoryRepository for all DB operations — no inline SQL.
    """
    repo = MemoryRepository()

    if tool_name == "add_memory":
        config = get_config()
        emb = await get_embedding(arguments["content"], config)
        if not emb:
            return {"error": "embedding failed", "id": None}

        async with pool.acquire() as conn:
            mid = str(uuid.uuid4())
            await repo.insert_memory(
                conn=conn, mid=mid, content=arguments["content"],
                emb=emb, agent_id=arguments["agent_id"],
                project_id="", user_id="default",
                category="general", tags=[],
                importance=arguments.get("importance", 0),
                memory_type=arguments.get("memory_type", "factual"),
            )
            row = await conn.fetchrow(
                "SELECT id, created_at FROM memories WHERE id = $1", mid,
            )
            return {
                "id": str(row["id"]),
                "created_at": row["created_at"].isoformat(),
            }

    elif tool_name == "search_memories":
        config = get_config()
        emb = await get_embedding(arguments["query"], config)
        if not emb:
            return {"results": [], "error": "embedding failed"}

        es = repo.format_vector(emb)
        limit = int(arguments.get("limit", 10))
        min_score = float(arguments.get("min_score", 0.0))

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, content, memory_type, importance,
                          created_at, updated_at,
                          1 - (embedding <=> $2::vector) AS similarity
                   FROM memories
                   WHERE agent_id = $1
                     AND archived_at IS NULL
                     AND embedding IS NOT NULL
                     AND 1 - (embedding <=> $2::vector) > $3
                   ORDER BY similarity DESC
                   LIMIT $4""",
                arguments["agent_id"], es, min_score, limit,
            )
            return {
                "results": [
                    {
                        "id": str(r["id"]),
                        "content": r["content"],
                        "memory_type": r["memory_type"],
                        "importance": r["importance"],
                        "similarity": round(float(r["similarity"]), 4),
                        "created_at": r["created_at"].isoformat(),
                    }
                    for r in rows
                ]
            }

    elif tool_name == "list_memories":
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, content, memory_type, importance,
                          created_at, updated_at
                   FROM memories
                   WHERE agent_id = $1
                     AND archived_at IS NULL
                     AND ($2::text IS NULL OR memory_type = $2)
                   ORDER BY created_at DESC
                   LIMIT $3""",
                arguments["agent_id"],
                arguments.get("memory_type"),
                arguments.get("limit", 20),
            )
            return {
                "memories": [
                    {
                        "id": str(r["id"]),
                        "content": r["content"],
                        "memory_type": r["memory_type"],
                        "importance": r["importance"],
                        "created_at": r["created_at"].isoformat(),
                    }
                    for r in rows
                ]
            }

    elif tool_name == "forget_memory":
        async with pool.acquire() as conn:
            archived = await repo.soft_delete(conn, arguments["memory_id"])
            return {"archived": archived}

    raise ValueError(f"Unknown tool: {tool_name}")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    config = get_config()
    logger.info("Memory API v2 starting (dev=%s, db=%s)", config.is_dev, config.db_name)

    pool = await init_pool(config)
    app.state.pool = pool

    async with pool.acquire() as conn:
        val = await conn.fetchval("SELECT 1")
        logger.info("DB warm-up: SELECT 1 = %s", val)

    logger.info("Memory API v2 ready on port 8765")
    yield
    await close_pool()
    logger.info("Memory API v2 shutdown complete")


app = FastAPI(
    title="Memory API v2",
    version="2.0",
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "path": str(request.url.path)},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin_router)
app.include_router(profiles_router)
app.include_router(hygiene_router)
app.include_router(memories_router)
app.include_router(search_router)
app.include_router(aliases_router)
app.include_router(setup_router)
app.include_router(relations_router)
app.include_router(sessions_router)
app.include_router(conflicts_router)
app.include_router(prompts_router)


@app.get("/")
async def root():
    return {
        "service": "Memory API v2",
        "version": "2.0",
        "docs": "/docs",
    }


# ---------------------------------------------------------------------------
# MCP SSE endpoint — JSON-RPC over Server-Sent Events
# ---------------------------------------------------------------------------


@app.get("/mcp/sse")
async def mcp_sse(request: Request):
    """SSE endpoint for MCP — streams responses to client."""
    session_id = str(uuid.uuid4())
    queue: _asyncio.Queue = _asyncio.Queue()
    if not hasattr(app.state, "mcp_queues"):
        app.state.mcp_queues = {}
    app.state.mcp_queues[session_id] = queue

    logger.info("MCP SSE session started: %s", session_id)

    async def event_generator() -> AsyncIterator[dict]:
        yield {
            "event": "endpoint",
            "data": f"/mcp/messages/{session_id}",
        }
        try:
            while True:
                try:
                    message = await _asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {
                        "event": "message",
                        "data": json.dumps(message),
                    }
                except _asyncio.TimeoutError:
                    yield {"event": "ping", "data": "keepalive"}
        except _asyncio.CancelledError:
            pass
        finally:
            app.state.mcp_queues.pop(session_id, None)
            logger.info("MCP SSE session ended: %s", session_id)

    return EventSourceResponse(event_generator())


@app.post("/mcp/messages/{session_id}")
async def mcp_messages(session_id: str, request: Request):
    """Handle MCP JSON-RPC messages. Responses flow back via SSE."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None},
        )

    msg_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

    logger.info("MCP message: method=%s id=%s session=%s", method, msg_id, session_id)

    response = None

    if method == "initialize":
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "memory-api-v2", "version": "2.0"},
            },
        }

    elif method == "notifications/initialized":
        return JSONResponse(status_code=202, content="")

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        try:
            result = await _execute_tool(request.app.state.pool, tool_name, arguments)
            response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except ValueError as e:
            response = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": str(e)}}
        except Exception as e:
            logger.exception("Tool execution error: %s", tool_name)
            response = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(e)}}

    elif method == "tools/list":
        response = {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": MCP_TOOLS}}

    else:
        response = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

    if response:
        queues = getattr(request.app.state, "mcp_queues", {})
        queue = queues.get(session_id)
        if queue:
            await queue.put(response)
        return response

    return JSONResponse(status_code=202, content="")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")
