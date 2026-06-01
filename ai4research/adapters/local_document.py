"""local_document_file — the required Phase 0 adapter (design §8.2). Reads a local
text/markdown file (or inline text). Because a plain `.md` flows through the whole
spine, the spine cannot be source-specific.
"""
from __future__ import annotations

from .base import AcquireResult, SourceAdapter, acquire_text, register


class LocalDocumentFileAdapter(SourceAdapter):
    ADAPTER_ID = "local_document_file"

    def validate_item(self, item: dict) -> tuple[bool, str | None]:
        loc = item.get("item_locator") or {}
        if "local_path" not in loc and "inline_text" not in loc:
            return False, "item_locator needs local_path or inline_text"
        return True, None

    def acquire(self, item: dict) -> AcquireResult:
        return acquire_text(item, path_key="local_path", document_kind="local_document")


register(LocalDocumentFileAdapter())
