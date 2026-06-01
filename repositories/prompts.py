"""Memory API v2 — Isolated prompt templates for LLM interactions.

All system prompts used across the codebase live here, not scattered
in router handlers or service code. Keeps magic strings out of business logic.
"""

# ── Entity Extraction ─────────────────────────────────────────────────────────

ENTITY_EXTRACTION_SYSTEM: str = (
    "Extract key entities (people, places, concepts, projects) from this text. "
    "Return ONLY a JSON array of strings."
)

# ── Dream Service: Consolidation ──────────────────────────────────────────────

MERGE_FACTS_SYSTEM: str = (
    "Merge these two similar facts into one concise combined fact. "
    "Return ONLY the merged text, no preamble or explanation."
)

# ── Dream Service: Reflection ─────────────────────────────────────────────────

REFLECT_SYSTEM: str = (
    "Based on these memories, identify 2-3 high-level patterns, "
    "preferences, or insights about this user. "
    "Return ONLY a JSON array of strings, nothing else. "
    'Example: ["Prefers minimalist design", "Often works late at night"]'
)