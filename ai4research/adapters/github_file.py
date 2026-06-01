"""github_file_fixture — optional Phase 0 adapter, built through the same interface when
needed. A GitHub item is a repo file/README URL plus a supplied file fixture. Live API
reads are Phase 2; structured metrics (stars, release cadence) are a Phase 2 schema
extension. Provider data (path, sha) stays in `provider_metadata`.
"""
from __future__ import annotations

from .base import AcquireResult, SourceAdapter, acquire_text, register


class GitHubFileFixtureAdapter(SourceAdapter):
    ADAPTER_ID = "github_file_fixture"

    def validate_item(self, item: dict) -> tuple[bool, str | None]:
        loc = item.get("item_locator") or {}
        if "url" not in loc:
            return False, "github item_locator needs a url"
        if "local_fixture_path" not in loc and "inline_text" not in loc:
            return False, "github item_locator needs local_fixture_path or inline_text"
        return True, None

    def acquire(self, item: dict) -> AcquireResult:
        return acquire_text(item, path_key="local_fixture_path", document_kind="github_document")


register(GitHubFileFixtureAdapter())
