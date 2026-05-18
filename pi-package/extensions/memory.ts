/**
 * pi-memory-sync.ts v4.0 — Pi Agent Memory Extension dla Memory API v2
 *
 * Kompletna integracja Pi z memory-api na http://localhost:8765.
 * v2: asyncpg, Gemini embedding, circuit breaker, hygiene.
 *
 * - Custom tools: memory_add, memory_search, memory_list, memory_stats, memory_forget,
 *                 memory_related, memory_feedback, memory_extract, memory_profile
 * - Auto-sync: decyzje, edycje, tool results → zapis do pamięci
 * - Context injection: relewantne fakty w before_agent_start
 * - Health check na starcie sesji
 * - Obsługa API Key i rate limiting
 * - Per-agent agent_id
 *
 * Zgodność z @samfp/pi-memory key format (pref.*, project.*, tool.*, user.*, lesson.*)
 */

import type {
  ExtensionAPI,
  ExtensionContext,
  AgentToolResult,
  TurnEndEvent,
} from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";

// ═══════════════════════════════════════════════════════════════════════════════
// Types
// ═══════════════════════════════════════════════════════════════════════════════

type ToolResult = AgentToolResult<unknown>;

function ok(text: string, details?: Record<string, unknown>): ToolResult {
  return { content: [{ type: "text", text }], details: details ?? {} };
}

function err(text: string): ToolResult {
  return { content: [{ type: "text", text }], details: {}, isError: true };
}

interface MemoryRecord {
  id: string;
  content?: string;
  excerpt?: string;
  category: string;
  agent_id: string;
  project_id?: string;
  user_id?: string;
  tags?: string[];
  source_file?: string;
  confidence?: number;
  importance?: number;
  trust_score?: number;
  archived?: boolean;
  archived_at?: string;
  pi_memory_key?: string;
  memory_type?: string;
  created_at: string;
  updated_at?: string;
}

interface SearchResponse {
  results: MemoryRecord[];
  count: number;
}

interface ListResponse {
  memories: MemoryRecord[];
  count: number;
  total?: number;
  has_more?: boolean;
}

interface StatsResponse {
  overview: {
    total: number;
    active: number;
    archived: number;
    avg_trust?: number;
    avg_importance?: number;
    agents: number;
    projects: number;
  };
  by_agent: Record<string, number>;
  by_project: Record<string, number>;
  by_category: Record<string, number>;
}

interface HealthResponse {
  status: "ok" | "degraded";
  version: string;
  checks: {
    db: { ok: boolean; latency_ms: number };
    embedding: { ok: boolean; provider: string; model: string; dimension: number };
    circuit_breaker: { state: string; failures: number; cooldown_remaining: number };
  };
}

interface ProfileResponse {
  profile?: Record<string, unknown>;
  user_id?: string;
  profile_data?: Record<string, unknown>;
  updated_at?: string;
}

// ═══════════════════════════════════════════════════════════════════════════════
// Configuration
// ═══════════════════════════════════════════════════════════════════════════════

const MEMORY_API_URL = process.env.MEMORY_API_URL || "http://localhost:8765";
const API_TOKEN = process.env.MEMORY_API_TOKEN || "dev-token-change-me";
const EXT_NAME = "memory-sync";
const MAX_CONTENT_LENGTH = 10000;
const AUTO_SYNC_INTERVAL_MS = 60_000;
const AUTO_SYNC_MAX_FACTS = 10;

// ═══════════════════════════════════════════════════════════════════════════════
// HTTP Client
// ═══════════════════════════════════════════════════════════════════════════════

type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string; status?: number };

async function apiRequest<T>(
  endpoint: string,
  method: "GET" | "POST" | "PATCH" | "DELETE" = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<ApiResult<T>> {
  const url = `${MEMORY_API_URL}${endpoint}`;
  const headers: Record<string, string> = { "Content-Type": "application/json" };

  if (method !== "GET") {
    headers["Authorization"] = `Bearer ${API_TOKEN}`;
  }

  try {
    const response = await fetch(url, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
      signal,
    });

    if (response.status === 429) {
      const retryAfter = response.headers.get("Retry-After") || "30";
      return {
        ok: false,
        error: `Rate limited. Retry after ${retryAfter}s.`,
        status: 429,
      };
    }

    if (!response.ok) {
      const text = await response.text().catch(() => "unknown error");
      return {
        ok: false,
        error: `API ${response.status}: ${text.slice(0, 250)}`,
        status: response.status,
      };
    }

    const data = (await response.json()) as T;
    return { ok: true, data };
  } catch (error: unknown) {
    if (error instanceof DOMException && error.name === "AbortError") {
      return { ok: false, error: "Request cancelled", status: 0 };
    }
    const msg = error instanceof Error ? error.message : String(error);
    if (msg.includes("ECONNREFUSED") || msg.includes("fetch failed")) {
      return {
        ok: false,
        error:
          "Memory API offline.\n" +
          `  Start: cd /home/ArndtOs/Tools/memory-api-v2 && python main.py\n` +
          `  Or:    cd /home/ArndtOs/Tools/memory-api && bash restart-services.sh`,
        status: 0,
      };
    }
    return { ok: false, error: msg, status: 0 };
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// Project ID
// ═══════════════════════════════════════════════════════════════════════════════

function deriveProjectId(cwd: string): string {
  let dir = resolve(cwd);
  const parts: string[] = [];

  for (let i = 0; i < 5; i++) {
    const parent = resolve(dir, "..");
    if (parent === dir) break;

    const basename = dir.split("/").pop() || dir;
    if (existsSync(join(dir, "package.json"))) {
      parts.unshift(basename);
      return parts.join("/") || basename;
    }
    if (existsSync(join(dir, ".git"))) {
      parts.unshift(basename);
      return parts.join("/") || basename;
    }
    parts.unshift(basename);
    dir = parent;
  }

  const cwdParts = cwd.split("/").filter(Boolean);
  return cwdParts.slice(-2).join("/") || "unknown";
}

// ═══════════════════════════════════════════════════════════════════════════════
// Sync State
// ═══════════════════════════════════════════════════════════════════════════════

interface PendingFact {
  content: string;
  key: string;
  memoryType: string;
  importance: number;
}

interface SyncState {
  lastSync: number;
  pendingFacts: PendingFact[];
}

// ═══════════════════════════════════════════════════════════════════════════════
// Extension
// ═══════════════════════════════════════════════════════════════════════════════

export default function (pi: ExtensionAPI) {
  let initialized = false;
  let sessionCwd = "";
  let projectId = "unknown";
  let agentId = "pi-agent"; // default, overridden by PI_AGENT_ID or _AGENTS_MD
  let embedModel = "gemini-embedding-001";
  let totalFacts = 0;
  let isHealthy = false;
  let initialContextLines: string[] = [];

  const syncState: SyncState = { lastSync: 0, pendingFacts: [] };

  // ── Resolve Agent ID ──────────────────────────────────────────────────────

  function resolveAgentId(): string {
    // 1. Environment variable
    if (process.env.PI_AGENT_ID) return process.env.PI_AGENT_ID;
    // 2. Try to read from APPEND_SYSTEM.md
    try {
      const appendPath = join(sessionCwd, "APPEND_SYSTEM.md");
      if (existsSync(appendPath)) {
        const content = require("fs").readFileSync(appendPath, "utf-8");
        const match = content.match(/agent_id:\s*(\S+)/i);
        if (match) return match[1];
      }
      // Also check ~/.pi-agents/<agent>/APPEND_SYSTEM.md pattern
      const homeAgent = join(require("os").homedir(), ".pi-agents");
      const agentName = process.env.PI_CODING_AGENT_DIR?.split("/").pop() || "";
      if (agentName) {
        const agentAppend = join(homeAgent, agentName, "APPEND_SYSTEM.md");
        if (existsSync(agentAppend)) {
          const content = require("fs").readFileSync(agentAppend, "utf-8");
          const match = content.match(/agent_id:\s*(\S+)/i);
          if (match) return match[1];
        }
      }
    } catch {}
    // 3. Default
    return "pi-agent";
  }

  // ── Health Check ──────────────────────────────────────────────────────────

  async function checkHealth(ctx: ExtensionContext): Promise<boolean> {
    const result = await apiRequest<HealthResponse>("/health");
    if (!result.ok) {
      isHealthy = false;
      ctx.ui.setStatus(EXT_NAME, "🧠 offline");
      if (result.status !== 429) {
        ctx.ui.notify(`🧠 Memory API offline: ${result.error}`, "warning");
      }
      return false;
    }

    const h = result.data;
    isHealthy = true;
    embedModel = h.checks.embedding?.model || "baai/bge-m3";

    const statsResult = await apiRequest<StatsResponse>("/memories/stats");
    if (statsResult.ok) {
      totalFacts = statsResult.data.overview?.active ?? 0;
    }

    const dim = h.checks.embedding?.dimension || 1024;
    ctx.ui.setStatus(EXT_NAME, `🧠 ${totalFacts}f | ${embedModel.split("/")[0] || embedModel} | ${dim}d`);

    return true;
  }

  // ── Flush Pending Facts ──────────────────────────────────────────────────

  async function flushPendingFacts(ctx: ExtensionContext): Promise<void> {
    if (syncState.pendingFacts.length === 0) return;
    if (!isHealthy) return;

    const now = Date.now();
    if (now - syncState.lastSync < AUTO_SYNC_INTERVAL_MS) return;

    const batch = syncState.pendingFacts.splice(0, AUTO_SYNC_MAX_FACTS);
    syncState.lastSync = now;

    let saved = 0;
    for (const fact of batch) {
      const result = await apiRequest<{ status: string; id: string; pi_memory_key?: string }>(
        "/pi-remember",
        "POST",
        {
          content: fact.content.slice(0, MAX_CONTENT_LENGTH),
          key: fact.key,
          agent_id: agentId,
          project_id: projectId,
          importance: fact.importance,
        },
      );
      if (result.ok) saved++;
    }

    if (saved > 0) {
      totalFacts += saved;
      ctx.ui.setStatus(EXT_NAME, `🧠 ${totalFacts}f`);
    }
  }

  // ── Queue Fact ───────────────────────────────────────────────────────────

  function queueFact(
    content: string,
    key: string,
    memoryType: string = "general",
    importance: number = 0,
  ): void {
    if (!content || content.length < 10) return;

    const prefix = content.slice(0, 80);
    if (syncState.pendingFacts.some((f) => f.content.startsWith(prefix))) return;

    syncState.pendingFacts.push({
      content,
      key: `${memoryType}.${key}`,
      memoryType,
      importance,
    });
  }

  // ── Session Lifecycle ────────────────────────────────────────────────────

  pi.on("session_start", async (_event, ctx) => {
    sessionCwd = ctx.cwd || process.cwd();
    projectId = deriveProjectId(sessionCwd);
    agentId = resolveAgentId();

    const healthy = await checkHealth(ctx);

    if (healthy) {
      ctx.ui.notify(
        `🧠 Memory: ${totalFacts}f | ${embedModel} | agent: ${agentId} | project: ${projectId}`,
        "info",
      );
      await flushPendingFacts(ctx);
    }
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    await flushPendingFacts(ctx);
  });

  // ── Context Injection ────────────────────────────────────────────────────

  pi.on("before_agent_start", async (event, ctx) => {
    if (!initialized) {
      initialized = true;
      if (!isHealthy) await checkHealth(ctx);
      try {
        const initResult = await apiRequest<SearchResponse>("/pi-search", "POST", {
          query: "session context work arrangements orchestrator subagenty",
          limit: 5,
          min_score: 0.2,
          agent_id: agentId,
        });
        if (initResult.ok && initResult.data?.results?.length) {
          initialContextLines = initResult.data.results.map((r) => {
            const prefix = r.pi_memory_key ? `[${r.pi_memory_key}]` : `[${r.category}]`;
            const conf = r.confidence ? ` (${(r.confidence * 100).toFixed(0)}%)` : "";
            const excerpt = r.excerpt || r.content?.slice(0, 300) || "";
            return `${prefix} ${excerpt}${conf}`;
          });
        }
      } catch {}
    }

    if (!isHealthy) return undefined;

    await flushPendingFacts(ctx);

    try {
      const query = (event.prompt || "").slice(0, 200) || "session context current project";

      const result = await apiRequest<SearchResponse>("/pi-search", "POST", {
        query,
        limit: 5,
        min_score: 0.25,
        agent_id: agentId,
      });

      if (!result.ok || !result.data.results?.length) return undefined;

      const lines = result.data.results.map((m) => {
        const prefix = m.pi_memory_key
          ? `[${m.pi_memory_key}]`
          : `[${m.category}]`;
        const conf = m.confidence
          ? ` (confidence: ${(m.confidence * 100).toFixed(0)}%)`
          : "";
        const excerpt = m.excerpt || m.content?.slice(0, 300) || "";
        return `${prefix} ${excerpt}${conf}`;
      });

      const lessonLines = lines.filter(l => l.startsWith("[lesson]") || l.includes("lesson"));
      const toolLines = lines.filter(l => l.startsWith("[tool]") || l.includes("tool"));
      const projectLines = lines.filter(l => l.startsWith("[project]") || l.includes("project"));
      const userLines = lines.filter(l => l.startsWith("[user]") || l.includes("[user_pref]") || l.includes("user"));
      const otherLines = lines.filter(l =>
        !l.startsWith("[") ||
        (!lessonLines.includes(l) && !toolLines.includes(l) && !projectLines.includes(l) && !userLines.includes(l))
      );

      const blocks: string[] = [];
      if (lessonLines.length) blocks.push("### CORE_PERSONA (Lessons)\n" + lessonLines.join("\n"));
      if (toolLines.length) blocks.push("### TOOL_SPECIFICS (Tool Knowledge)\n" + toolLines.join("\n"));
      if (projectLines.length) blocks.push("### PROJECT_CONTEXT (Project State)\n" + projectLines.join("\n"));
      if (userLines.length) blocks.push("### USER_PREFERENCES (User Info)\n" + userLines.join("\n"));
      if (otherLines.length) blocks.push("### OTHER_MEMORIES\n" + otherLines.join("\n"));

      let finalResult = {
        systemPrompt: event.systemPrompt + "\n\n## Relevant Knowledge\n" + blocks.join("\n\n") + "\n",
      };

      if (initialContextLines.length > 0) {
        finalResult.systemPrompt = finalResult.systemPrompt +
          "\n\n## Session Context\n" + initialContextLines.slice(0, 10).join("\n") + "\n";
        initialContextLines = [];
      }

      return finalResult;
    } catch {
      return undefined;
    }
  });

  // ── Auto-Sync ────────────────────────────────────────────────────────────

  pi.on("turn_end", async (event: TurnEndEvent, ctx: ExtensionContext) => {
    if (!isHealthy) return;

    const assistantMsg = event.message;
    if (!assistantMsg?.content) return;

    const text = typeof assistantMsg.content === "string"
      ? assistantMsg.content
      : Array.isArray(assistantMsg.content)
        ? assistantMsg.content.map((c: any) => c.text || "").join(" ")
        : "";

    if (text.length < 100) return;

    const decisionMarkers = [
      "decision", "decided", "chosen", "selected",
      "implemented", "changed", "fixed", "added",
      "refactored", "renamed", "moved", "removed",
      "conclusion:", "summary:", "I'll use", "let's use",
    ];

    const lines = text.split("\n").filter((l: string) => l.trim());
    const decisionLines = lines.filter((l: string) =>
      decisionMarkers.some((m) => l.toLowerCase().includes(m)),
    );

    for (const line of decisionLines.slice(0, 3)) {
      const clean = line.replace(/^[-*]\s*/, "").slice(0, 300);
      queueFact(clean, `decision_${Date.now()}`, "lesson", 3);
    }

    const toolResults = event.toolResults || [];
    for (const tr of toolResults) {
      if (tr.toolName === "edit" && tr.input) {
        const filePath = (tr.input as any).path || "unknown";
        queueFact(`Edited: ${filePath}`, `edit_${filePath.replace(/[./]/g, "_")}`, "project", 2);
      }
      if (tr.toolName === "write" && tr.input) {
        const filePath = (tr.input as any).path || "unknown";
        queueFact(`Created: ${filePath}`, `create_${filePath.replace(/[./]/g, "_")}`, "project", 2);
      }
    }

    await flushPendingFacts(ctx);
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Tools
  // ═══════════════════════════════════════════════════════════════════════════

  // ── memory_add ────────────────────────────────────────────────────────────

  pi.registerTool({
    name: "memory_add",
    label: "Memory: Add Fact",
    description:
      "Store a fact, preference, lesson, or project pattern in persistent memory via Memory API v2. " +
      "Auto-sets agent_id and project_id from current session context. " +
      "Use dotted keys: 'pref.editor', 'project.rosie.lang', 'lesson.mistake'. " +
      "Supports importance (0-10).",
    promptSnippet: "Add facts, preferences, and project patterns to persistent memory",
    promptGuidelines: [
      "Use memory_add to store important decisions, user preferences, and project patterns for cross-session recall.",
      "Use memory_add with memory_type 'lesson' when the user corrects you — prevents repeating mistakes.",
      "Set importance=5+ for critical facts that should appear in system prompt context.",
    ],
    parameters: Type.Object({
      content: Type.String({
        description: "Fact content (what to remember)",
        minLength: 5,
        maxLength: 10000,
      }),
      memory_type: Type.Optional(Type.String({
        description: "Type: 'pref' (preferences), 'project' (patterns), 'tool' (tools), 'user' (identity), 'lesson' (corrections), 'general'",
      })),
      key: Type.Optional(Type.String({
        description: "Structured key (e.g., 'commit_style', 'rosie.lang'). Creates 'type.key' in memory.",
        maxLength: 100,
      })),
      tags: Type.Optional(Type.String({
        description: "Comma-separated tags for filtering",
        maxLength: 500,
      })),
      importance: Type.Optional(Type.Number({
        description: "Importance 0-10 (higher = more prominent in context). Default: 0",
        minimum: 0,
        maximum: 10,
      })),
    }),

    async execute(_id, params, signal, _update, ctx) {
      if (!isHealthy) {
        return ok("Memory API offline. Start the server:\n" +
          "  cd /home/ArndtOs/Tools/memory-api-v2 && python main.py");
      }

      const memoryType = (params.memory_type as string) || "general";
      const content = String(params.content);
      const keyName = (params.key as string) ||
        content.slice(0, 50).replace(/[^a-zA-Z0-9]/g, "_").toLowerCase();
      const fullKey = `${memoryType}.${keyName}`;

      const result = await apiRequest<{ status: string; id: string; pi_memory_key?: string }>(
        "/pi-remember",
        "POST",
        {
          content,
          agent_id: agentId,
          project_id: projectId,
          key: fullKey,
          importance: (params.importance as number) ?? 0,
        },
        signal as AbortSignal,
      );

      if (!result.ok) {
        if (isHealthy) ctx.ui.notify(`memory_add failed: ${result.error}`, "error");
        return err(`Failed: ${result.error}`);
      }

      totalFacts++;
      ctx.ui.setStatus(EXT_NAME, `🧠 ${totalFacts}f`);

      const r = result.data;
      const storedKey = r.pi_memory_key || fullKey;

      if (r.status === "duplicate") {
        return ok(`⚠️ Duplicate (stored as ${storedKey})`,
          { id: r.id, status: "duplicate" });
      }

      return ok(`✅ Stored: ${storedKey}\n   ${content.slice(0, 200)}`,
        { id: r.id, key: storedKey, status: "added" });
    },
  });

  // ── memory_search ─────────────────────────────────────────────────────────

  pi.registerTool({
    name: "memory_search",
    label: "Memory: Semantic Search",
    description:
      "Search persistent memory semantically via pgvector. " +
      "Results include confidence score (0-1), excerpt, and metadata. " +
      "Use filters to narrow by type, category, or minimum confidence.",
    promptSnippet: "Search persistent memory for facts, preferences, and patterns",
    promptGuidelines: [
      "Use memory_search to recall stored info from previous sessions.",
      "Use memory_search when user asks 'remember...' or 'what did we decide about...'.",
      "Use min_score=0.5 for precise matches, min_score=0.2 for broader recall.",
    ],
    parameters: Type.Object({
      query: Type.String({ description: "Search query in natural language", minLength: 2 }),
      limit: Type.Optional(Type.Number({
        description: "Max results (default: 10, max: 100)", default: 10, minimum: 1, maximum: 100,
      })),
      memory_type: Type.Optional(Type.String({
        description: "Filter by type: 'pref', 'project', 'tool', 'user', 'lesson', or all",
      })),
      category: Type.Optional(Type.String({
        description: "Filter by category (e.g., 'user_pref', 'decision', 'code_edit')",
      })),
      min_score: Type.Optional(Type.Number({
        description: "Minimum confidence 0.0-1.0 (default: 0.3). Higher = stricter.",
        default: 0.3, minimum: 0.0, maximum: 1.0,
      })),
      cross_agent: Type.Optional(Type.Boolean({
        description: "Include memories from all agents (default: true). Set false for only your agent.",
        default: true,
      })),
    }),

    async execute(_id, params, signal, _update, _ctx) {
      if (!isHealthy) return ok("Memory API offline");

      const result = await apiRequest<SearchResponse>(
        "/pi-search",
        "POST",
        {
          query: String(params.query),
          limit: (params.limit as number) || 10,
          memory_type: (params.memory_type as string) || undefined,
          category: (params.category as string) || undefined,
          min_score: (params.min_score as number) ?? 0.3,
          agent_id: (params.cross_agent as boolean) === false ? agentId : undefined,
        },
        signal as AbortSignal,
      );

      if (!result.ok) return err(`Search failed: ${result.error}`);

      const { results, count } = result.data;

      if (count === 0) {
        return ok("No matching memories found.");
      }

      const formatted = results.map((r, i) => {
        const tag = r.pi_memory_key
          ? `[${r.pi_memory_key}]`
          : `[${r.category}]`;
        const conf = `confidence: ${(r.confidence * 100).toFixed(0)}%`;
        const imp = r.importance && r.importance > 0 ? ` | importance: ${r.importance}` : "";
        const agent = r.agent_id !== agentId ? ` | agent: ${r.agent_id}` : "";
        const excerpt = r.content || r.excerpt || "";
        return `${i + 1}. ${tag} ${excerpt.slice(0, 250)}...\n   ${conf}${imp}${agent} | ${r.created_at.slice(0, 10)}`;
      }).join("\n\n");

      return ok(`🧠 Showing ${count} results\n\n${formatted}`, { results, count });
    },
  });

  // ── memory_list ───────────────────────────────────────────────────────────

  pi.registerTool({
    name: "memory_list",
    label: "Memory: List Facts",
    description:
      "List stored facts from persistent memory with filters and pagination. " +
      "Newest first by default. Use memory_search for semantic concept search instead.",
    promptSnippet: "Browse stored facts from persistent memory",
    promptGuidelines: [
      "Use memory_list to browse all stored facts or filter by category.",
      "Use sort_by='importance' DESC to see most important facts first.",
    ],
    parameters: Type.Object({
      limit: Type.Optional(Type.Number({
        description: "Max results (default: 20, max: 200)", default: 20, minimum: 1, maximum: 200,
      })),
      offset: Type.Optional(Type.Number({
        description: "Pagination offset", default: 0, minimum: 0,
      })),
      category: Type.Optional(Type.String({
        description: "Filter by category: 'user_pref', 'project', 'tool', 'user', 'lesson', 'decision', 'code_edit'",
      })),
      memory_type: Type.Optional(Type.String({
        description: "Filter by pi-memory type: 'pref', 'project', 'tool', 'user', 'lesson'",
      })),
      sort_by: Type.Optional(Type.String({
        description: "Sort field: 'created_at' (default), 'importance', 'category', 'updated_at'",
      })),
    }),

    async execute(_id, params, signal, _update, _ctx) {
      if (!isHealthy) return ok("Memory API offline");

      const categoryMap: Record<string, string> = {
        pref: "user_pref", project: "project", tool: "tool",
        user: "user", lesson: "lesson", general: "general",
      };

      const category = (params.memory_type as string)
        ? categoryMap[params.memory_type as string] || (params.memory_type as string)
        : (params.category as string) || undefined;

      const limit = Math.min((params.limit as number) || 20, 200);
      const offset = (params.offset as number) || 0;

      const qs = new URLSearchParams();
      qs.set("limit", String(limit));
      qs.set("offset", String(offset));
      qs.set("agent_id", agentId);
      if (category) qs.set("category", category);
      if (params.sort_by) qs.set("sort_by", String(params.sort_by));

      const result = await apiRequest<ListResponse>(
        `/memories?${qs.toString()}`,
        "GET",
        undefined,
        signal as AbortSignal,
      );

      if (!result.ok) return err(`Failed: ${result.error}`);

      const { memories, count } = result.data;

      if (count === 0) {
        return ok(`No memories found${category ? ` in '${category}'` : ""}. Use memory_add to store.`);
      }

      const text = memories.map((r, i) => {
        const tag = r.pi_memory_key ? ` [${r.pi_memory_key}]` : ` [${r.category}]`;
        const imp = r.importance && r.importance > 0 ? ` (imp: ${r.importance})` : "";
        const excerpt = r.content || "";
        return `${i + 1 + offset}.${tag}${imp} ${excerpt.slice(0, 180)}...\n   Created: ${r.created_at.slice(0, 10)}`;
      }).join("\n\n");

      return ok(`🧠 ${count} facts (offset ${offset})\n\n${text}`, { results: memories, count });
    },
  });

  // ── memory_stats ──────────────────────────────────────────────────────────

  pi.registerTool({
    name: "memory_stats",
    label: "Memory: Statistics",
    description: "Show persistent memory statistics: facts, agents, categories, embedding info.",
    promptSnippet: "Show memory usage statistics",
    parameters: Type.Object({}),
    async execute(_id, _params, signal, _update, _ctx) {
      if (!isHealthy) return ok("Memory API offline");

      const [statsResult, healthResult] = await Promise.all([
        apiRequest<StatsResponse>("/memories/stats", "GET", undefined, signal as AbortSignal),
        apiRequest<HealthResponse>("/health", "GET", undefined, signal as AbortSignal),
      ]);

      if (!statsResult.ok) return err(`Failed: ${statsResult.error}`);

      const s = statsResult.data;
      const h = healthResult.ok ? healthResult.data : null;

      const lines = ["🧠  Memory Statistics", "━".repeat(42)];

      if (h) {
        const embed = h.checks.embedding;
        const cb = h.checks.circuit_breaker;
        lines.push(`Status: ${h.status === "ok" ? "✅ OK" : "⚠️ Degraded"}`);
        lines.push(`Provider: ${embed.provider} | Model: ${embed.model}`);
        lines.push(`Dimension: ${embed.dimension} | CB: ${cb.state} (${cb.failures} failures)`);
        lines.push("");
      }

      const o = s.overview;
      lines.push(`Total: ${o.total} | Active: ${o.active} | Archived: ${o.archived}`);
      lines.push(`Avg trust: ${o.avg_trust ?? "?"} | Avg importance: ${o.avg_importance ?? "?"}`);
      lines.push(`Agents: ${o.agents} | Projects: ${o.projects}`);

      if (Object.keys(s.by_agent).length > 0) {
        lines.push("", "By Agent:");
        for (const [a, c] of Object.entries(s.by_agent)) {
          lines.push(`  ${a}: ${c}`);
        }
      }

      if (Object.keys(s.by_project).length > 0) {
        lines.push("", "By Project:");
        for (const [p, c] of Object.entries(s.by_project)) {
          lines.push(`  ${p}: ${c}`);
        }
      }

      if (Object.keys(s.by_category).length > 0) {
        lines.push("", "By Category:");
        for (const [c, cnt] of Object.entries(s.by_category)) {
          lines.push(`  ${c}: ${cnt}`);
        }
      }

      return ok(lines.join("\n"), { overview: o, byAgent: s.by_agent, byCategory: s.by_category });
    },
  });

  // ── memory_forget ─────────────────────────────────────────────────────────

  pi.registerTool({
    name: "memory_forget",
    label: "Memory: Forget",
    description:
      "Archive (soft-delete) a fact from persistent memory by ID. " +
      "Forgotten facts are excluded from search results. " +
      "Use memory_list first to find the ID of the fact to forget.",
    promptSnippet: "Archive/forget a fact from persistent memory",
    promptGuidelines: [
      "Use memory_forget to clean up outdated or incorrect memories.",
      "First use memory_list or memory_search to find the memory ID.",
    ],
    parameters: Type.Object({
      id: Type.String({
        description: "Memory ID to forget (get from memory_list or memory_search results)",
        minLength: 1,
      }),
    }),

    async execute(_id, params, signal, _update, ctx) {
      if (!isHealthy) return ok("Memory API offline");

      const memoryId = String(params.id).trim();
      if (!memoryId) return err("Provide a memory id");

      const result = await apiRequest<{ status: string; memory_id: string }>(
        `/memories/${memoryId}`,
        "DELETE",
        undefined,
        signal as AbortSignal,
      );

      if (!result.ok) {
        if (result.status === 404) return err(`Memory not found: ${memoryId}`);
        return err(`Forget failed: ${result.error}`);
      }

      totalFacts = Math.max(0, totalFacts - 1);
      ctx.ui.setStatus(EXT_NAME, `🧠 ${totalFacts}f`);

      return ok(`🗑️ Forgotten: ${memoryId}`, { memory_id: memoryId, status: "archived" });
    },
  });

  // ── memory_related ────────────────────────────────────────────────────────

  pi.registerTool({
    name: "memory_related",
    label: "Memory: Related Facts",
    description:
      "Find facts related to a specific memory by ID. " +
      "Uses entity overlap first, then semantic similarity fallback.",
    promptSnippet: "Find facts related to a specific memory",
    parameters: Type.Object({
      id: Type.String({ description: "Memory ID to find related facts for", minLength: 1 }),
      limit: Type.Optional(Type.Number({
        description: "Max results (default: 10, max: 50)", default: 10, minimum: 1, maximum: 50,
      })),
    }),

    async execute(_id, params, signal, _update, _ctx) {
      if (!isHealthy) return ok("Memory API offline");

      const memoryId = String(params.id).trim();
      const limit = (params.limit as number) || 10;

      const result = await apiRequest<{ results: MemoryRecord[]; count: number }>(
        `/memories/${memoryId}/related?limit=${limit}`,
        "GET",
        undefined,
        signal as AbortSignal,
      );

      if (!result.ok) {
        if (result.status === 404) return err(`Memory not found: ${memoryId}`);
        return err(`Failed: ${result.error}`);
      }

      const { results, count } = result.data;
      if (count === 0) return ok("No related memories found.");

      const formatted = results.map((r, i) => {
        const tag = r.pi_memory_key ? `[${r.pi_memory_key}]` : `[${r.category}]`;
        const agent = r.agent_id !== agentId ? ` (agent: ${r.agent_id})` : "";
        const excerpt = r.content || "";
        const trust = r.trust_score ? ` | trust: ${(r.trust_score * 100).toFixed(0)}%` : "";
        return `${i + 1}. ${tag}${agent} ${excerpt.slice(0, 200)}...${trust}`;
      }).join("\n\n");

      return ok(`🧠 ${count} related facts\n\n${formatted}`, { results, count });
    },
  });

  // ── memory_feedback ───────────────────────────────────────────────────────

  pi.registerTool({
    name: "memory_feedback",
    label: "Memory: Feedback",
    description:
      "Record feedback on a memory to update its trust score. " +
      "Use 'helpful' when a fact was useful, 'not_helpful' when it wasn't, " +
      "'incorrect' when a fact is wrong, 'outdated' when it's stale.",
    promptSnippet: "Rate a memory (helpful/not_helpful/incorrect/outdated)",
    promptGuidelines: [
      "Use memory_feedback to improve memory quality. Feedback updates trust_score.",
      "Use feedback_type 'incorrect' when you see wrong information.",
    ],
    parameters: Type.Object({
      id: Type.String({ description: "Memory ID to give feedback on", minLength: 1 }),
      feedback_type: Type.String({
        description: "Type: 'helpful', 'not_helpful', 'incorrect', 'outdated'",
      }),
      comment: Type.Optional(Type.String({
        description: "Optional comment explaining the feedback",
        maxLength: 500,
      })),
    }),

    async execute(_id, params, signal, _update, _ctx) {
      if (!isHealthy) return ok("Memory API offline");

      const allowed = ["helpful", "not_helpful", "incorrect", "outdated"];
      const feedbackType = String(params.feedback_type).toLowerCase();
      if (!allowed.includes(feedbackType)) {
        return err(`Invalid feedback_type. Must be one of: ${allowed.join(", ")}`);
      }

      const result = await apiRequest<{ status: string; trust_score?: number }>(
        `/memories/${String(params.id)}/feedback`,
        "POST",
        {
          agent_id: agentId,
          feedback_type: feedbackType,
          comment: (params.comment as string) || "",
        },
        signal as AbortSignal,
      );

      if (!result.ok) {
        if (result.status === 404) return err(`Memory not found: ${String(params.id)}`);
        return err(`Feedback failed: ${result.error}`);
      }

      const ts = result.data.trust_score;
      const tsStr = ts !== undefined ? ` (new trust: ${(ts * 100).toFixed(0)}%)` : "";
      return ok(`✅ Feedback recorded: ${feedbackType}${tsStr}`, result.data);
    },
  });

  // ── memory_extract ────────────────────────────────────────────────────────

  pi.registerTool({
    name: "memory_extract",
    label: "Memory: Extract Facts",
    description:
      "Use LLM to automatically extract key facts from text and save them to memory. " +
      "Useful for processing conversations, articles, or notes into structured memories.",
    promptSnippet: "Extract key facts from text and save to memory",
    parameters: Type.Object({
      text: Type.String({
        description: "Text to extract facts from (conversation, article, notes)",
        minLength: 20,
        maxLength: 8000,
      }),
      category: Type.Optional(Type.String({ description: "Category for extracted facts (default: 'general')", default: "general" })),
    }),

    async execute(_id, params, signal, _update, _ctx) {
      if (!isHealthy) return ok("Memory API offline");

      const result = await apiRequest<{ extracted: number; saved: number; duplicates: number; facts?: unknown[] }>(
        "/extract-facts",
        "POST",
        {
          text: String(params.text).slice(0, 8000),
          agent_id: agentId,
          project_id: projectId,
          category: (params.category as string) || "general",
        },
        signal as AbortSignal,
      );

      if (!result.ok) return err(`Extract failed: ${result.error}`);

      const { extracted, saved, duplicates, facts } = result.data;

      let detail = "";
      if (facts && facts.length > 0) {
        detail = "\n\n" + (facts as Array<{ content?: string }>)
          .slice(0, 5)
          .map((f, i) => `${i + 1}. ${f.content?.slice(0, 150)}`)
          .join("\n");
      }

      totalFacts += saved;
      ctx.ui.setStatus(EXT_NAME, `🧠 ${totalFacts}f`);

      return ok(`🧠 Extracted: ${extracted} facts, saved: ${saved}, duplicates: ${duplicates}${detail}`,
        { extracted, saved, duplicates });
    },
  });

  // ── memory_profile ────────────────────────────────────────────────────────

  pi.registerTool({
    name: "memory_profile",
    label: "Memory: User Profile",
    description:
      "Get or update a user profile in persistent memory. " +
      "Profiles store cross-session user preferences, identity, and context.",
    promptSnippet: "Manage user profiles in memory",
    parameters: Type.Object({
      action: Type.String({ description: "'get' to read, 'update' to write/merge" }),
      user_id: Type.String({ description: "User identifier (e.g., 'kamil', 'default')", default: "default" }),
      data: Type.Optional(Type.String({ description: "JSON data to merge (only for action='update')" })),
    }),

    async execute(_id, params, signal, _update, _ctx) {
      if (!isHealthy) return ok("Memory API offline");

      const action = String(params.action).toLowerCase();
      const userId = String(params.user_id) || "default";

      if (action === "get") {
        const result = await apiRequest<{ profile?: Record<string, unknown> }>(
          `/profiles/${userId}`, "GET", undefined, signal as AbortSignal,
        );
        if (!result.ok) return err(`Failed: ${result.error}`);
        const profile = result.data.profile;
        if (!profile || Object.keys(profile).length === 0) {
          return ok(`No profile found for user: ${userId}`);
        }
        return ok(`Profile for ${userId}:\n${JSON.stringify(profile, null, 2)}`, { profile });
      }

      if (action === "update") {
        if (!params.data) return err("Provide 'data' (JSON string) for action='update'");
        let parsed: Record<string, unknown>;
        try { parsed = JSON.parse(String(params.data)); }
        catch { return err("Invalid JSON in 'data' parameter"); }
        const result = await apiRequest<{ status: string }>(
          `/profiles/${userId}`, "POST", parsed, signal as AbortSignal,
        );
        if (!result.ok) return err(`Failed: ${result.error}`);
        return ok(`✅ Profile updated for ${userId}`, { user_id: userId, data: parsed });
      }

      return err("Action must be 'get' or 'update'");
    },
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Commands
  // ═══════════════════════════════════════════════════════════════════════════

  pi.registerCommand("memory-health", {
    description: "Check Memory API v2 health, stats, and embedding status",
    handler: async (_args, ctx) => {
      const healthy = await checkHealth(ctx);
      if (healthy) {
        ctx.ui.notify(
          `🧠 Memory v2 OK | ${totalFacts}f | model: ${embedModel} | agent: ${agentId} | project: ${projectId}`,
          "info",
        );
      } else {
        ctx.ui.notify(
          "🧠 Memory offline — start:\n  cd /home/ArndtOs/Tools/memory-api-v2 && python main.py",
          "warning",
        );
      }
    },
  });

  pi.registerCommand("memory-sync", {
    description: "Force-sync pending facts to memory immediately",
    handler: async (_args, ctx) => {
      const pending = syncState.pendingFacts.length;
      if (pending === 0) {
        ctx.ui.notify("🧠 No pending facts to sync.", "info");
        return;
      }
      syncState.lastSync = 0;
      await flushPendingFacts(ctx);
      ctx.ui.notify(`🧠 Synced ${pending} fact(s)`, "success");
    },
  });
}
