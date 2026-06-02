"""Discovery query planning — turning a human topic into good search queries.

This lives in the fetch tool, never the compiler: query understanding is fuzzy and benefits
from an LLM, but the deterministic compiler core must stay LLM-free. The planner has two
implementations behind one call: a deterministic heuristic (default, no dependencies) and an
optional LLM planner that reuses the compiler's ModelRuntime seam. The LLM is strictly an
improvement with a guaranteed fallback — any failure degrades to the heuristic.
"""
from __future__ import annotations

import re

# Framing/temporal words that hurt search precision: a repo/video search wants the *subject*,
# not how the question was phrased. Only ever applied to discovery, never to the report topic.
_FILLER = {
    "a", "an", "the", "of", "on", "in", "for", "to", "and", "or", "with", "about", "into",
    "from", "at", "by", "is", "are", "be", "whats", "what", "how", "why", "latest", "recent",
    "new", "news", "update", "updates", "development", "developments", "overview", "current",
    "introduction", "intro", "guide", "state", "trend", "trends", "status", "progress",
    "advances", "advancements", "explained", "explainer", "tutorial", "review", "deep", "dive",
}


def normalize_query(text: str) -> str:
    """Deterministic keyword extraction: drop framing words, keep the subject. Falls back to
    the original text if everything was filler (so it never returns an empty query)."""
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9.+#-]*", text or "")
    kept = [t for t in tokens if t.lower() not in _FILLER]
    return " ".join(kept) if kept else (text or "").strip()


# codex's --output-schema requires a top-level object (not an array), so the plan is one object.
QUERY_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["github_query", "youtube_query"],
    "properties": {
        "github_query": {"type": "string"},
        "youtube_query": {"type": "string"},
    },
}


def _plan_prompt(topic: str) -> str:
    return (
        "You plan search queries for a research source-discovery tool. Given a research topic, "
        "return concise queries to find the most relevant GitHub repositories and YouTube videos.\n"
        "Rules:\n"
        "- Disambiguate acronyms to their technical/subject meaning; drop framing words like "
        "'latest', 'overview', 'on'.\n"
        "- github_query: the few most distinctive keywords (GitHub repo search requires ALL "
        "terms to match, so keep it tight).\n"
        "- youtube_query: a short descriptive phrase (a little more context helps relevance).\n"
        "Return a JSON array with exactly one object: "
        '{"github_query": str, "youtube_query": str}.\n\n'
        f"Topic: {topic}"
    )


def plan_queries(topic: str, runtime=None) -> dict:
    """Return ``{"github", "youtube", "source", "topic"}``. With a ModelRuntime, ask the LLM
    for queries; on any failure (or no runtime) fall back to the deterministic heuristic."""
    fallback = normalize_query(topic)
    heuristic = {"github": fallback, "youtube": fallback, "source": "heuristic", "topic": topic}
    if runtime is None:
        return heuristic
    try:
        records = runtime.propose(_plan_prompt(topic), QUERY_PLAN_SCHEMA)
    except Exception:  # noqa: BLE001 - planning is best-effort; never abort discovery on it
        return heuristic
    if not records:
        return heuristic
    record = records[0]
    github = str(record.get("github_query") or "").strip() or fallback
    youtube = str(record.get("youtube_query") or "").strip() or fallback
    return {"github": github, "youtube": youtube, "source": getattr(runtime, "name", "llm"), "topic": topic}
