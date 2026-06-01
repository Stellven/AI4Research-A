"""Source adapter registry. Importing this package registers the built-in adapters."""
from __future__ import annotations

from .base import AcquireResult, SourceAdapter, SOURCE_PACK_MAP, acquire_text, get_adapter, register
from . import local_document    # noqa: F401  registers local_document_file (required)
from . import youtube_transcript  # noqa: F401  registers youtube_transcript_fixture
from . import github_file        # noqa: F401  registers github_file_fixture

__all__ = ["AcquireResult", "SourceAdapter", "SOURCE_PACK_MAP", "acquire_text", "get_adapter", "register"]
