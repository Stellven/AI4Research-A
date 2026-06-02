"""Source-fetch client interfaces + real implementations.

Each external service sits behind a small Protocol so tests inject fakes (cassettes) and
no test needs the network or API keys. The real implementations import their library lazily
and raise `FetchDependencyError` if the optional extra is not installed. Response-parsing is
factored into pure helpers (below) so the real-client logic is unit-testable without network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


class FetchDependencyError(RuntimeError):
    """An optional fetch dependency is missing — `pip install ai4research[fetch]`."""


@dataclass
class VideoMeta:
    video_id: str
    url: str
    title: str
    published_at: str | None                 # ISO date (YYYY-MM-DD) or None
    transcript: list[dict] | None = None      # [{"start": float, "text": str}, ...] or None (no captions)


@dataclass
class RepoMeta:
    repo_url: str
    name: str
    file_path: str
    blob_url: str
    file_text: str | None                     # README/file text, or None (inaccessible)
    stars: int | None = None
    releases_in_window: int | None = None
    last_release: str | None = None           # ISO date or None


class ChannelLister(Protocol):
    def list_videos(self, channel_url: str, since: str | None, max_items: int | None) -> list[VideoMeta]: ...


class TranscriptFetcher(Protocol):
    def fetch(self, video_id: str) -> list[dict] | None: ...   # timed segments or None if no captions


class GitHubClient(Protocol):
    def repo_meta(self, repo_url: str, since: str | None) -> RepoMeta: ...


# --- pure helpers (unit-tested; no network) ---

def _iso_from_ytdlp(date8: str | None) -> str | None:
    """yt-dlp `upload_date` 'YYYYMMDD' -> 'YYYY-MM-DD'."""
    if not date8 or len(str(date8)) != 8 or not str(date8).isdigit():
        return None
    date8 = str(date8)
    return f"{date8[:4]}-{date8[4:6]}-{date8[6:]}"


def _entry_date(entry: dict) -> str | None:
    """Best-effort publish date from a yt-dlp flat entry (often absent in flat mode)."""
    iso = _iso_from_ytdlp(entry.get("upload_date"))
    if iso:
        return iso
    ts = entry.get("timestamp") or entry.get("release_timestamp")
    if isinstance(ts, (int, float)):
        import datetime as _dt
        return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).date().isoformat()
    return None


def _parse_repo(repo_url: str) -> tuple[str, str]:
    """owner, repo from common GitHub URL forms: https(s)://host/o/r(.git), host/o/r,
    git@host:o/r(.git), bare o/r. Fails loudly on anything that has no owner/repo."""
    url = (repo_url or "").strip()
    scp = re.match(r"^[\w.-]+@[^:]+:(.+)$", url)        # git@github.com:owner/repo(.git)
    if scp:
        path = scp.group(1)
    else:
        path = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", url)   # strip scheme
        head, sep, rest = path.partition("/")
        if sep and "." in head:                          # leading host (github.com / enterprise)
            path = rest
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    segs = [s for s in path.split("/") if s]
    if len(segs) < 2:
        raise ValueError(f"cannot parse owner/repo from: {repo_url!r}")
    return segs[0], segs[1]


def _blob_url(owner: str, repo: str, branch: str = "main", path: str = "README.md") -> str:
    """Canonical GitHub blob URL built from parsed owner/repo (valid for any URL form
    `_parse_repo` accepts, unlike concatenating onto the raw input)."""
    return f"https://github.com/{owner}/{repo}/blob/{branch}/{path}"


def _releases_in_window(releases, since: str | None) -> tuple[int | None, str | None]:
    """(count within window, latest date). Tolerates draft releases (published_at == null)."""
    if not isinstance(releases, list):
        return (None, None)
    dates = [((r.get("published_at") or "")[:10]) for r in releases if isinstance(r, dict)]
    dates = [d for d in dates if d]
    if not dates:
        return (None, None)
    count = sum(1 for d in dates if (since is None or d >= since))
    return (count, max(dates))


def _github_error(payload) -> str | None:
    """GitHub error bodies are dicts carrying 'message' + 'documentation_url' (and none of the
    success fields). Returns the message if `payload` is such an error, else None."""
    if isinstance(payload, dict) and "message" in payload and "name" not in payload \
            and "stargazers_count" not in payload and "content" not in payload:
        return str(payload.get("message"))
    return None


def normalize_segments(raw) -> list[dict]:
    """Normalize transcript snippets (dicts OR v1.x snippet objects) to [{start, text}]."""
    out = []
    for s in raw:
        if isinstance(s, dict):
            start, text = s.get("start", 0.0), s.get("text", "")
        else:
            start, text = getattr(s, "start", 0.0), getattr(s, "text", "")
        out.append({"start": float(start), "text": str(text)})
    return out


# --- real implementations (lazy imports; exercised live only by the optional smoke test) ---

class YtDlpChannelLister:
    """Enumerate a channel's recent uploads via yt-dlp (no API key). NOTE: flat extraction
    often omits publish dates; when a date is unavailable the item is kept (it cannot be
    excluded by `since`). For strict freshness windows, full extraction is required."""

    def list_videos(self, channel_url: str, since: str | None, max_items: int | None) -> list[VideoMeta]:
        try:
            import yt_dlp  # noqa: PLC0415
        except ImportError as exc:
            raise FetchDependencyError("yt-dlp not installed — pip install ai4research[fetch]") from exc
        with yt_dlp.YoutubeDL({"extract_flat": "in_playlist", "quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(channel_url, download=False)
        videos: list[VideoMeta] = []
        for entry in info.get("entries") or []:
            if not isinstance(entry, dict):     # yt-dlp flat mode yields None for deleted/private/blocked videos
                continue
            vid = entry.get("id")
            if not vid:
                continue
            published = _entry_date(entry)
            if since and published and published < since:
                continue
            videos.append(VideoMeta(
                video_id=vid, url=f"https://www.youtube.com/watch?v={vid}",   # canonical so the &t= deep-link joins correctly
                title=entry.get("title") or vid, published_at=published))
        videos.sort(key=lambda v: v.published_at or "", reverse=True)
        return videos[:max_items] if max_items else videos


class YouTubeTranscriptFetcher:
    """Fetch a video's timed transcript via youtube-transcript-api. Supports both the v1.x
    instance API (`YouTubeTranscriptApi().fetch`) and the v0.x classmethod
    (`get_transcript`); a no-transcript condition returns None (a coverage gap), but a library
    API mismatch surfaces as an error rather than a silent gap."""

    def fetch(self, video_id: str) -> list[dict] | None:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi  # noqa: PLC0415
        except ImportError as exc:
            raise FetchDependencyError("youtube-transcript-api not installed — pip install ai4research[fetch]") from exc
        try:
            if hasattr(YouTubeTranscriptApi, "get_transcript"):      # v0.x classmethod
                raw = YouTubeTranscriptApi.get_transcript(video_id)
            else:                                                    # v1.x instance API
                fetched = YouTubeTranscriptApi().fetch(video_id)
                raw = fetched.to_raw_data() if hasattr(fetched, "to_raw_data") else list(fetched)
        except (AttributeError, TypeError):
            raise   # our call doesn't match the installed library — surface it, don't silently gap
        except Exception:  # noqa: BLE001 - no captions / disabled / unavailable -> coverage gap
            return None
        return normalize_segments(raw)


class ApiGitHubClient:
    """Repo metrics + README via the GitHub REST API (optional GITHUB_TOKEN for rate limits).
    The primary repo fetch uses raise_for_status so 404/403/rate-limit surface (and become a
    coverage gap via the caller's failsafe) rather than a fabricated empty RepoMeta."""

    def __init__(self, token: str | None = None):
        import os
        self.token = token or os.getenv("GITHUB_TOKEN")

    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def repo_meta(self, repo_url: str, since: str | None) -> RepoMeta:
        try:
            import base64  # noqa: PLC0415
            import requests  # noqa: PLC0415
        except ImportError as exc:
            raise FetchDependencyError("requests not installed — pip install ai4research[fetch]") from exc
        owner, repo = _parse_repo(repo_url)
        api = f"https://api.github.com/repos/{owner}/{repo}"
        resp = requests.get(api, headers=self._headers(), timeout=30)
        resp.raise_for_status()                          # 404/403/rate-limit -> HTTPError -> repo gap
        info = resp.json()
        if _github_error(info):
            raise ValueError(f"github error for {owner}/{repo}: {_github_error(info)}")
        try:
            releases = requests.get(f"{api}/releases", headers=self._headers(), timeout=30).json()
        except Exception:  # noqa: BLE001
            releases = []
        rel_count, last_release = _releases_in_window(releases, since)
        readme_text = None
        blob_url = _blob_url(owner, repo, info.get("default_branch", "main"))
        try:
            readme = requests.get(f"{api}/readme", headers=self._headers(), timeout=30).json()
            if isinstance(readme, dict) and readme.get("content") and not _github_error(readme):
                readme_text = base64.b64decode(readme["content"]).decode("utf-8", "replace")
                blob_url = readme.get("html_url") or blob_url
        except Exception:  # noqa: BLE001 - no README is a gap, not a failure
            readme_text = None
        return RepoMeta(repo_url=repo_url, name=info.get("full_name") or f"{owner}/{repo}",
                        file_path="README.md", blob_url=blob_url, file_text=readme_text,
                        stars=info.get("stargazers_count"), releases_in_window=rel_count,
                        last_release=last_release)
