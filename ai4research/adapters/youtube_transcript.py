"""youtube_transcript_fixture — built after the spine, through the same SourceAdapter
interface, and required for the §14.2 proving run. A YouTube item is a video URL plus a
supplied transcript fixture (or inline text). Live transcript fetch is Phase 2; provider
data (video_id, handle) stays in `provider_metadata`, which the spine never reads.
"""
from __future__ import annotations

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
        return acquire_text(item, path_key="local_fixture_path", document_kind="youtube_transcript")


register(YouTubeTranscriptFixtureAdapter())
