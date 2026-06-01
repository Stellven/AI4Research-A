"""youtube_transcript_fixture — built after the spine, through the same SourceAdapter
interface, and required for the §14.2 proving run. A YouTube item is a video URL plus a
supplied transcript fixture (or inline text). Live transcript fetch is Phase 2; provider
data (video_id, handle) stays in `provider_metadata`, which the spine never reads.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import AcquireResult, SourceAdapter, acquire_text, register


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
            parts = []
            segments = []
            offset = 0
            for segment in data:
                segment_text = str(segment.get("text", ""))
                if not segment_text.strip():
                    continue
                if parts:
                    offset += 2
                segments.append({"start_char": offset, "start_seconds": int(segment.get("start", 0))})
                parts.append(segment_text)
                offset += len(segment_text)
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
