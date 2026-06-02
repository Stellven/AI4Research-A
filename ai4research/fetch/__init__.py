"""Live-retrieval tool: turn YouTube channel + GitHub repo URLs into the compiler's
on-disk source pack + fixtures. SEPARATE from the deterministic compiler — the compiler
never imports this, and they meet only at the on-disk contract (build.run_fetch output).

Network/parsing dependencies are OPTIONAL (`pip install ai4research[fetch]`) and imported
lazily inside the real clients; importing this package needs only the standard library.
"""
from __future__ import annotations

from .build import run_fetch
from .clients import (
    ChannelLister,
    FetchDependencyError,
    GitHubClient,
    RepoMeta,
    TranscriptFetcher,
    VideoMeta,
)

__all__ = [
    "run_fetch",
    "ChannelLister",
    "TranscriptFetcher",
    "GitHubClient",
    "VideoMeta",
    "RepoMeta",
    "FetchDependencyError",
]
