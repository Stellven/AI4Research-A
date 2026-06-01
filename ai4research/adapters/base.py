"""SourceAdapter interface (design §8.2). An adapter turns one selected item into an
acquisition attempt and, on success, document text. Provider-specific behavior lives
only inside adapters; the spine operates on generic rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class AcquireResult:
    ok: bool
    text: str | None = None
    title: str | None = None
    document_kind: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None


def acquire_text(item: dict, *, path_key: str, document_kind: str) -> AcquireResult:
    """Shared local read used by every Phase 0 adapter: inline text or a local fixture
    file. Provider families differ only in `path_key`/`document_kind`, which keeps the
    spine source-agnostic. A missing fixture is the canonical Phase 0 coverage gap."""
    loc = item.get("item_locator") or {}
    if loc.get("inline_text") is not None:
        text, title = loc["inline_text"], item.get("title")
    elif loc.get(path_key):
        path = Path(loc[path_key])
        if not path.exists():
            return AcquireResult(False, failure_code="local_fixture_missing",
                                 failure_message=f"file not found: {path}")
        if not path.is_file():
            return AcquireResult(False, failure_code="not_a_file",
                                 failure_message=f"path is not a file: {path}")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return AcquireResult(False, failure_code="read_error", failure_message=str(exc))
        title = item.get("title") or path.name
    else:
        return AcquireResult(False, failure_code="invalid_locator",
                             failure_message=f"item_locator needs {path_key} or inline_text")
    if not text.strip():
        return AcquireResult(False, failure_code="empty_document",
                             failure_message="document has no text")
    return AcquireResult(True, text=text, title=title, document_kind=document_kind)


class SourceAdapter:
    ADAPTER_ID: str = ""
    VERSION: str = "0.1.0"

    def validate_item(self, item: dict) -> tuple[bool, str | None]:
        return True, None

    def acquire(self, item: dict) -> AcquireResult:
        raise NotImplementedError


# container source_pack_type -> (source_family, adapter_id, document_kind)
SOURCE_PACK_MAP: dict[str, tuple[str, str, str]] = {
    "local_document_set": ("local_document", "local_document_file", "local_document"),
    "youtube_channel": ("youtube_video", "youtube_transcript_fixture", "youtube_transcript"),
    "github_repo": ("github_file", "github_file_fixture", "github_document"),
}

_ADAPTERS: dict[str, SourceAdapter] = {}


def register(adapter: SourceAdapter) -> None:
    _ADAPTERS[adapter.ADAPTER_ID] = adapter


def get_adapter(adapter_id: str) -> SourceAdapter | None:
    return _ADAPTERS.get(adapter_id)
