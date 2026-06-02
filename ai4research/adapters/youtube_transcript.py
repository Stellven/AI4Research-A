"""youtube_transcript_fixture — built after the spine, through the same SourceAdapter
interface, and required for the §14.2 proving run. A YouTube item is a video URL plus a
supplied transcript fixture (or inline text). Live transcript fetch is Phase 2; provider
data (video_id, handle) stays in `provider_metadata`, which the spine never reads.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import AcquireResult, SourceAdapter, acquire_text, register

# Caption lines are far shorter than the evidence floor (extraction.MIN_EVIDENCE_NONWS_CHARS),
# so consecutive lines are coalesced into paragraphs of about this many characters — large
# enough to survive the floor, below text.MAX_PARAGRAPH_CHARS so each stays a single span.
TRANSCRIPT_PARAGRAPH_CHARS = 400
# Mirrors extraction.MIN_EVIDENCE_NONWS_CHARS (kept local to preserve adapter→operator layering):
# a trailing paragraph below this would be silently dropped, so it is merged back instead.
TRANSCRIPT_MIN_TAIL_NONWS = 80


def _nonws(s: str) -> int:
    return sum(1 for c in s if not c.isspace())


class YouTubeTranscriptFixtureAdapter(SourceAdapter):
    ADAPTER_ID = "youtube_transcript_fixture"

    def validate_item(self, item: dict) -> tuple[bool, str | None]:
        loc = item.get("item_locator") or {}
        if "url" not in loc:
            return False, "youtube item_locator needs a url"
        if "local_fixture_path" not in loc and "inline_text" not in loc:
            return False, "youtube item_locator needs local_fixture_path or inline_text"
        return True, None

    def acquire(self, item: dict) -> AcquireResult:
        loc = item.get("item_locator") or {}
        fixture = loc.get("local_fixture_path")
        if fixture and Path(fixture).suffix == ".json":
            path = Path(fixture)
            if not path.exists():
                return AcquireResult(False, failure_code="local_fixture_missing",
                                     failure_message=f"file not found: {path}")
            if not path.is_file():
                return AcquireResult(False, failure_code="not_a_file",
                                     failure_message=f"path is not a file: {path}")
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return AcquireResult(False, failure_code="read_error", failure_message=str(exc))
            # Each caption's text is whitespace-collapsed to a single line, so the assembled
            # text is already in normal form: text.normalize() leaves it unchanged, and the
            # recorded start_char (consumed post-normalize by the &t= mapper) indexes the exact
            # caption. (Recording offsets against the pre-normalize text drifts them, because
            # normalize deletes interior newlines/trailing whitespace real captions contain.)
            captions = []
            for segment in data:
                clean = " ".join(str(segment.get("text", "")).split())
                if clean:
                    captions.append((int(segment.get("start", 0)), clean))
            # Group caption indices into paragraphs, flushing once a paragraph reaches target size.
            groups: list[list[int]] = []
            cur, cur_len = [], 0
            for i, (_, clean) in enumerate(captions):
                cur.append(i)
                cur_len += len(clean)
                if cur_len >= TRANSCRIPT_PARAGRAPH_CHARS:
                    groups.append(cur)
                    cur, cur_len = [], 0
            if cur:
                groups.append(cur)
            # A trailing paragraph below the evidence floor would be silently dropped — merge it back.
            if len(groups) >= 2 and sum(_nonws(captions[i][1]) for i in groups[-1]) < TRANSCRIPT_MIN_TAIL_NONWS:
                groups[-2].extend(groups.pop())
            # Assemble the text and record each caption's start_char in the (already-normal) text.
            parts: list[str] = []
            segments = []
            offset = 0
            for gi, group in enumerate(groups):
                if gi:
                    offset += 2          # "\n\n" between paragraphs
                piece = []
                for j, idx in enumerate(group):
                    start_seconds, clean = captions[idx]
                    if j:
                        offset += 1      # " " between caption lines within a paragraph
                        piece.append(" ")
                    segments.append({"start_char": offset, "start_seconds": start_seconds})
                    piece.append(clean)
                    offset += len(clean)
                parts.append("".join(piece))
            text = "\n\n".join(parts)
            if not text.strip():
                return AcquireResult(False, failure_code="empty_document",
                                     failure_message="document has no text")
            return AcquireResult(
                True,
                text=text,
                title=item.get("title") or path.name,
                document_kind="youtube_transcript",
                provider_metadata={"segments": segments},
            )
        return acquire_text(item, path_key="local_fixture_path", document_kind="youtube_transcript")


register(YouTubeTranscriptFixtureAdapter())
