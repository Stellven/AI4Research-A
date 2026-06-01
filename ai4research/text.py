"""Deterministic text normalization and span segmentation (design §7.8, §7.9, §8.3).

These are the offset-critical functions: every span's `(start_char, end_char)` indexes
into `normalized_text`, and `SpanOffsetGate` requires `normalized_text[start:end] == text`.
The segmenter therefore only ever slices the normalized string — it never rebuilds text —
so the invariant holds by construction.
"""
from __future__ import annotations

import re

# Near-identity normalization rules, applied in order (design §8.3 O7).
NORMALIZATION_RULES = ["crlf_to_lf", "rstrip_lines", "collapse_blank_lines", "trim"]

# Paragraph-first chunking targets (design §7.9).
MAX_PARAGRAPH_CHARS = 900
WINDOW_TARGET_CHARS = 800  # split oversized paragraphs into ~700-900 char sentence windows

_BLANK_LINES = re.compile(r"\n{3,}")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def normalize(raw_text: str) -> str:
    """Canonical text. Newlines to `\\n`, trailing per-line whitespace stripped,
    runs of blank lines collapsed to one, leading/trailing whitespace trimmed.
    Paragraph breaks (a single blank line) are preserved."""
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def _paragraph_offsets(normalized: str) -> list[tuple[int, str]]:
    """`(start_offset, paragraph_text)` for each paragraph. Paragraphs are separated by
    exactly `\\n\\n` after normalization, so offsets are exact arithmetic."""
    out: list[tuple[int, str]] = []
    cursor = 0
    for part in normalized.split("\n\n"):
        if part.strip():
            out.append((cursor, part))
        cursor += len(part) + 2  # +2 for the "\n\n" separator that split removed
    return out


def _windows(paragraph: str, base: int) -> list[tuple[int, int, str]]:
    """Split a long paragraph into sentence-aware windows. Returns `(start, end, text)`
    with offsets relative to `base` (the paragraph's offset in the normalized text).
    Each window text is an exact slice of the paragraph, so offset-exactness is preserved."""
    bounds = [m.end() for m in _SENTENCE_END.finditer(paragraph)]
    if not bounds or bounds[-1] != len(paragraph):
        bounds.append(len(paragraph))

    windows: list[tuple[int, int, str]] = []
    win_start = 0
    for end in bounds:
        if end - win_start >= WINDOW_TARGET_CHARS:
            windows.append((base + win_start, base + end, paragraph[win_start:end]))
            # skip inter-window whitespace so the next window starts on real text
            nxt = end
            while nxt < len(paragraph) and paragraph[nxt].isspace():
                nxt += 1
            win_start = nxt
    if win_start < len(paragraph):
        windows.append((base + win_start, base + len(paragraph), paragraph[win_start:]))
    return windows


def segment(normalized: str) -> list[tuple[int, int, str]]:
    """Paragraph-first chunker. Paragraphs <= MAX_PARAGRAPH_CHARS are one span; longer
    ones split into sentence-aware windows. Returns `(start_char, end_char, text)`,
    offsets into `normalized`, no empty spans."""
    spans: list[tuple[int, int, str]] = []
    for base, para in _paragraph_offsets(normalized):
        if len(para) <= MAX_PARAGRAPH_CHARS:
            spans.append((base, base + len(para), para))
        else:
            spans.extend(_windows(para, base))
    return [(s, e, t) for (s, e, t) in spans if e > s]
