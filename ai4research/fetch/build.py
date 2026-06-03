"""Assemble fetched sources into the compiler's on-disk source-pack contract.

Output layout (relative `local_fixture_path` keeps the snapshot portable; the compiler's
`stage_source_pack` resolves them against the pack-file directory):

    <out_dir>/
      source_containers.jsonl
      sources/transcripts/<item_id>__<video_id>.json   # [{"start": <sec>, "text": <str>}, ...]
      sources/repos/<item_id>__<repo>.md

Fixture filenames are keyed on the per-run-unique item id, so same-named repos / repeated
video ids never collide (they would otherwise silently overwrite each other). Every external
call AND every per-item write is guarded: a failed channel/video/repo becomes a recorded
coverage gap, never a crash and never a fabricated/overwritten fixture. A missing optional
dependency (FetchDependencyError) is re-raised, not swallowed as a gap.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .clients import ChannelLister, FetchDependencyError, GitHubClient, TranscriptFetcher, normalize_segments


def _safe(name) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("_") or "item"


def _valid_segments(segs) -> bool:
    """A usable transcript is a non-empty list of {start,text} dicts/snippet objects with at
    least one non-whitespace text (an all-blank transcript is a gap, not a fixture)."""
    if not isinstance(segs, list) or not segs:
        return False
    has_text = False
    for s in segs:
        if isinstance(s, dict):
            if "start" not in s or "text" not in s:
                return False
            text = s.get("text", "")
        elif hasattr(s, "start") and hasattr(s, "text"):
            text = getattr(s, "text", "")
        else:
            return False
        if str(text).strip():
            has_text = True
    return has_text


def _coerce_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        return list(value)
    except TypeError:
        return []


def run_fetch(
    out_dir,
    channels: list[str] | None = None,
    repos: list[str] | None = None,
    *,
    since: str | None = None,
    max_per_channel: int | None = None,
    channel_lister: ChannelLister | None = None,
    transcript_fetcher: TranscriptFetcher | None = None,
    github_client: GitHubClient | None = None,
    perspectives: dict | None = None,
) -> dict:
    """Fetch channels/repos into a source pack at `out_dir`. Clients are injectable (tests pass
    fakes); defaults are the real network clients (lazy). Returns a summary dict. Raises
    FetchDependencyError if an optional dependency is missing (a config error, not a gap)."""
    out = Path(out_dir)
    (out / "sources" / "transcripts").mkdir(parents=True, exist_ok=True)
    (out / "sources" / "repos").mkdir(parents=True, exist_ok=True)

    if channel_lister is None:
        from .clients import YtDlpChannelLister
        channel_lister = YtDlpChannelLister()
    if transcript_fetcher is None:
        from .clients import YouTubeTranscriptFetcher
        transcript_fetcher = YouTubeTranscriptFetcher()
    if github_client is None:
        from .clients import ApiGitHubClient
        github_client = ApiGitHubClient()

    perspectives = perspectives or {}     # locator -> source stance (provenance hint, #12)
    containers: list[dict] = []
    seen_video_ids: set = set()           # same video surfaced by two perspectives' channels: stage once
    summary = {"channels": 0, "videos": 0, "transcripts": 0, "video_gaps": 0,
               "repos": 0, "repo_gaps": 0, "errors": []}

    for ci, channel_url in enumerate(channels or []):
        try:
            videos = _coerce_list(channel_lister.list_videos(channel_url, since, max_per_channel))
        except FetchDependencyError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad channel must not abort the run
            summary["errors"].append(f"channel {channel_url}: {type(exc).__name__}: {exc}")
            containers.append({"container_id": f"C-YT-{ci}", "source_pack_type": "youtube_channel",
                               "container_locator": channel_url, "label": channel_url, "items": [],
                               "source_perspective": perspectives.get(channel_url, "basic")})
            continue
        summary["channels"] += 1
        items = []
        for vi, v in enumerate(videos):
            vid = getattr(v, "video_id", None)
            if vid and vid in seen_video_ids:
                continue                       # already staged from another perspective's search
            if vid:
                seen_video_ids.add(vid)
            item_id = f"V-{ci}-{vi}"
            summary["videos"] += 1
            try:
                locator = {"url": getattr(v, "url", None) or ""}
                segments = getattr(v, "transcript", None)
                if segments is None:
                    try:
                        segments = transcript_fetcher.fetch(getattr(v, "video_id", None))
                    except FetchDependencyError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        segments = None
                        summary["errors"].append(f"transcript {item_id}: {type(exc).__name__}: {exc}")
                if _valid_segments(segments):
                    fp = out / "sources" / "transcripts" / f"{item_id}__{_safe(getattr(v, 'video_id', item_id))}.json"
                    fp.write_text(json.dumps(normalize_segments(segments), ensure_ascii=False), encoding="utf-8")
                    locator["local_fixture_path"] = f"sources/transcripts/{fp.name}"
                    summary["transcripts"] += 1
                else:
                    summary["video_gaps"] += 1  # listed but no usable fixture -> coverage gap
                items.append({"item_id": item_id, "item_locator": locator,
                              "title": getattr(v, "title", None) or item_id,
                              "published_at": getattr(v, "published_at", None),
                              "provider_metadata": {"video_id": getattr(v, "video_id", None)}})
            except FetchDependencyError:
                raise
            except Exception as exc:  # noqa: BLE001 - a malformed item must not abort the channel
                summary["errors"].append(f"video {item_id}: {type(exc).__name__}: {exc}")
                summary["video_gaps"] += 1
        containers.append({"container_id": f"C-YT-{ci}", "source_pack_type": "youtube_channel",
                           "container_locator": channel_url, "label": channel_url, "items": items,
                           "source_perspective": perspectives.get(channel_url, "basic")})

    for ri, repo_url in enumerate(repos or []):
        item_id = f"R-{ri}"
        try:
            meta = github_client.repo_meta(repo_url, since)
            summary["repos"] += 1
            locator = {"url": getattr(meta, "blob_url", None) or ""}
            if getattr(meta, "file_text", None):
                fp = out / "sources" / "repos" / f"{item_id}__{_safe(getattr(meta, 'name', item_id))}.md"
                fp.write_text(meta.file_text, encoding="utf-8")
                locator["local_fixture_path"] = f"sources/repos/{fp.name}"
            else:
                summary["repo_gaps"] += 1
            item = {"item_id": item_id, "item_locator": locator,
                    "title": f"{getattr(meta, 'name', item_id)} {getattr(meta, 'file_path', '')}".strip(),
                    "provider_metadata": {"path": getattr(meta, "file_path", None), "stars": getattr(meta, "stars", None),
                                          "releases_in_window": getattr(meta, "releases_in_window", None),
                                          "last_release": getattr(meta, "last_release", None)}}
            containers.append({"container_id": f"C-GH-{ri}", "source_pack_type": "github_repo",
                               "container_locator": repo_url, "label": getattr(meta, "name", repo_url),
                               "items": [item], "source_perspective": perspectives.get(repo_url, "basic")})
        except FetchDependencyError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad repo (or injected client) must not abort the run
            summary["errors"].append(f"repo {repo_url}: {type(exc).__name__}: {exc}")
            summary["repo_gaps"] += 1
            containers.append({"container_id": f"C-GH-{ri}", "source_pack_type": "github_repo",
                               "container_locator": repo_url, "label": repo_url, "items": [],
                               "source_perspective": perspectives.get(repo_url, "basic")})

    (out / "source_containers.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in containers), encoding="utf-8")
    return summary
