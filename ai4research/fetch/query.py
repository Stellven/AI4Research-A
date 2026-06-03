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


# codex's --output-schema requires a top-level object; the perspectives are an array property so
# one LLM call enumerates multiple stances. _valid_record does not recurse into array items, so the
# per-field `or normalize_query(topic)` guards below are load-bearing for malformed inner items.
QUERY_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["perspectives"],
    "properties": {
        "perspectives": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["perspective", "github_query", "youtube_query"],
                "properties": {
                    "perspective": {"type": "string"},
                    "github_query": {"type": "string"},
                    "youtube_query": {"type": "string"},
                },
            },
        },
    },
}

MAX_PERSPECTIVES = 4   # bound the fan-out (one LLM call, a few stances) — no query explosion

# Comparison framings, longest-first so "meaningfully better than" splits before "better than".
_COMPARISON_MARKERS = ("meaningfully better than", "better than", "worse than", "superior to",
                       "compared to", " versus ", " vs ")


def _is_comparison(topic: str) -> bool:
    low = (topic or "").lower()
    return any(m in low for m in _COMPARISON_MARKERS)


def _split_operands(topic: str) -> list:
    """Split a comparison topic into its two sides at the first marker (markers longest-first)."""
    text, low = (topic or ""), (topic or "").lower()
    for marker in _COMPARISON_MARKERS:
        i = low.find(marker)
        if i >= 0:
            return [normalize_query(text[:i]), normalize_query(text[i + len(marker):])]
    return [normalize_query(text)]


def heuristic_perspectives(topic: str) -> list:
    """The deterministic floor: always a basic (today's exact normalize_query) pair + a contrarian
    pair, so a skeptic query is structurally guaranteed even with zero LLM."""
    base = normalize_query(topic)
    basic = {"perspective": "basic", "github": base, "youtube": base, "source": "heuristic"}
    if _is_comparison(topic):
        swapped = " ".join(reversed([o for o in _split_operands(topic) if o])).strip()
        gh = (swapped + " limitations criticism").strip()
        yt = (swapped + " criticism debate").strip()
    else:
        gh = (base + " limitations criticism").strip()
        yt = (base + " criticism debate").strip()
    skeptic = {"perspective": "skeptic", "github": gh or base, "youtube": yt or base, "source": "heuristic"}
    return [basic, skeptic]


def _plan_prompt(topic: str) -> str:
    return (
        "You plan search queries for a research source-discovery tool, enumerating MULTIPLE "
        "PERSPECTIVES so the gathered corpus is not one-sided. For the topic, return 2-4 DISTINCT "
        "stances; for each, queries to find the most relevant GitHub repositories and YouTube videos "
        "FOR THAT STANCE.\n"
        "Rules:\n"
        "- The FIRST perspective MUST be the mainstream/proponent stance.\n"
        "- For comparison-framed topics (\"X vs Y\", \"is X better than Y\") you MUST include a "
        "skeptic/contrarian stance — bias its queries toward 'worse than', 'limitations', 'failure "
        "modes', 'criticism', and benchmark/comparison repos, NOT the canonical implementation repo "
        "— and an empirical-benchmark stance.\n"
        "- Disambiguate acronyms to their technical meaning; drop framing words like 'latest'.\n"
        "- github_query: the few most distinctive keywords (GitHub ANDs all terms, keep it tight).\n"
        "- youtube_query: a short descriptive phrase. 'perspective': a short label.\n"
        'Return a JSON object {"perspectives": [{"perspective": str, "github_query": str, '
        '"youtube_query": str}, ...]}.\n\n'
        f"Topic: {topic}"
    )


def plan_queries(topic: str, runtime=None) -> list:
    """Return a LIST of perspective records ``[{"perspective", "github", "youtube", "source"}]`` —
    element 0 is the basic/proponent stance. With a ModelRuntime the LLM enumerates stances; on any
    failure (or no runtime) fall back to the deterministic two-perspective floor. For comparison
    topics a skeptic stance is guaranteed (LLM proposes, code guarantees)."""
    base = normalize_query(topic)
    if runtime is None:
        return heuristic_perspectives(topic)
    try:
        records = runtime.propose(_plan_prompt(topic), QUERY_PLAN_SCHEMA)
    except Exception:  # noqa: BLE001 - planning is best-effort; never abort discovery on it
        return heuristic_perspectives(topic)
    proposed = (records[0].get("perspectives") if records and isinstance(records[0], dict) else None) or []
    out: list = []
    seen: set = set()
    for item in proposed:
        if not isinstance(item, dict):
            continue
        github = str(item.get("github_query") or "").strip() or base       # load-bearing guards
        youtube = str(item.get("youtube_query") or "").strip() or base
        name = str(item.get("perspective") or "").strip() or "perspective"
        key = (github.lower(), youtube.lower())
        if key in seen:                                                     # drop near-identical clones
            continue
        seen.add(key)
        out.append({"perspective": name, "github": github, "youtube": youtube,
                    "source": getattr(runtime, "name", "llm")})
        if len(out) >= MAX_PERSPECTIVES:
            break
    if not out:
        return heuristic_perspectives(topic)
    # code GUARANTEES a contrarian stance on comparison topics even if the LLM omitted one
    if _is_comparison(topic) and not any(
            any(w in p["perspective"].lower() for w in ("skeptic", "contrarian", "critic")) for p in out):
        out.append(heuristic_perspectives(topic)[1])
    return out
