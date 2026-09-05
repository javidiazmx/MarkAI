"""Turns designated YouTube videos into Documents using their captions.

Captions come from ``youtube-transcript-api`` (no API key, but YouTube rate-limits it), and
titles from the public oEmbed endpoint. When a video has no captions the owner can drop a
transcript file next to it in the manifest instead.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from markai.ingest.transcripts import parse_transcript_file, segments_to_text
from markai.ingest.websites import make_client
from markai.models import Document, IngestError, IngestFailure, Segment, SourceKind
from markai.sources.manifest import YouTubeEpisode, YouTubeSection

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL_PATTERNS = (
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"/shorts/([A-Za-z0-9_-]{11})"),
    re.compile(r"/live/([A-Za-z0-9_-]{11})"),
    re.compile(r"/embed/([A-Za-z0-9_-]{11})"),
    re.compile(r"/v/([A-Za-z0-9_-]{11})"),
)

_NO_CAPTIONS_HINT = (
    "Captions are off for this video. Add a transcript_file for it in sources.yaml, or add "
    "the episode audio/transcript under the podcast section instead."
)
_BLOCKED_HINT = (
    "YouTube is rate-limiting this machine. Wait an hour and re-run `mark ingest`, or supply "
    "a transcript_file for this episode."
)


def extract_video_id(url_or_id: str) -> str:
    """Pull the 11-character video id out of any YouTube URL form (or a bare id)."""
    value = (url_or_id or "").strip()
    if not value:
        raise IngestError("Empty YouTube URL.", hint="Add the full watch URL to sources.yaml.")
    if _ID_RE.match(value):
        return value
    for pattern in _URL_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    raise IngestError(
        f"Could not find a YouTube video id in {value!r}.",
        hint="Use the full watch URL, e.g. https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )


def watch_url(video_id: str) -> str:
    """Canonical watch URL for a video id."""
    return f"https://www.youtube.com/watch?v={video_id}"


def fetch_video_title(video_id: str, client: httpx.Client) -> tuple[str, str | None]:
    """Title and channel name via oEmbed; a placeholder title when that fails."""
    try:
        response = client.get(
            "https://www.youtube.com/oembed",
            params={"url": watch_url(video_id), "format": "json"},
        )
        response.raise_for_status()
        data = response.json()
        return str(data.get("title") or f"YouTube video {video_id}"), data.get("author_name")
    except Exception as exc:
        logger.debug("oEmbed failed for %s: %s", video_id, exc)
        return f"YouTube video {video_id}", None


def _snippets_to_segments(raw: Any) -> list[Segment]:
    segments: list[Segment] = []
    for item in raw:
        if isinstance(item, dict):
            text, start, duration = (
                item.get("text", ""),
                item.get("start", 0.0),
                item.get("duration", 0.0),
            )
        else:
            text = getattr(item, "text", "")
            start = getattr(item, "start", 0.0)
            duration = getattr(item, "duration", 0.0)
        text = (text or "").strip()
        if not text:
            continue
        start = float(start or 0.0)
        segments.append(Segment(start=start, end=start + float(duration or 0.0), text=text))
    return segments


def fetch_transcript_segments(
    video_id: str,
    api: Any | None = None,
    languages: Sequence[str] = ("en",),
) -> list[Segment]:
    """Fetch captions for a video and convert them into timed segments."""
    import youtube_transcript_api as yta

    if api is None:
        api = yta.YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=list(languages))
    except (yta.TranscriptsDisabled, yta.NoTranscriptFound) as exc:
        raise IngestError(f"No captions available for {video_id}.", hint=_NO_CAPTIONS_HINT) from exc
    except (yta.RequestBlocked, yta.IpBlocked) as exc:
        raise IngestError(
            f"YouTube blocked the caption request for {video_id}.", hint=_BLOCKED_HINT
        ) from exc
    except yta.VideoUnavailable as exc:
        raise IngestError(
            f"Video {video_id} is unavailable.",
            hint="Check the URL, or remove the entry from sources.yaml.",
        ) from exc
    except yta.YouTubeTranscriptApiException as exc:
        raise IngestError(
            f"Could not fetch captions for {video_id}: {exc}", hint=_NO_CAPTIONS_HINT
        ) from exc

    raw = fetched.to_raw_data() if hasattr(fetched, "to_raw_data") else list(fetched)
    segments = _snippets_to_segments(raw)
    if not segments:
        raise IngestError(f"Captions for {video_id} were empty.", hint=_NO_CAPTIONS_HINT)
    return segments


def read_urls_file(path: Path) -> list[YouTubeEpisode]:
    """Read a plain list of YouTube URLs, one per line, ``#`` comments allowed."""
    path = Path(path)
    if not path.exists():
        raise IngestError(
            f"YouTube urls_file not found: {path}",
            hint="Create the file with one YouTube URL per line, or remove urls_file.",
        )
    episodes: list[YouTubeEpisode] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        title = None
        if "|" in stripped:
            url_part, _, title_part = stripped.partition("|")
            stripped = url_part.strip()
            title = title_part.strip() or None
        episodes.append(YouTubeEpisode(url=stripped, title=title))
    return episodes


def _merge_episodes(section: YouTubeSection, project_root: Path) -> list[YouTubeEpisode]:
    episodes: list[YouTubeEpisode] = []
    seen: set[str] = set()

    def add(entry: YouTubeEpisode) -> None:
        try:
            video_id = extract_video_id(entry.url)
        except IngestError:
            episodes.append(entry)  # keep it so the caller can report the failure
            return
        if video_id in seen:
            return
        seen.add(video_id)
        episodes.append(entry)

    for entry in section.episodes:
        add(entry)
    if section.urls_file:
        for entry in read_urls_file(project_root / section.urls_file):
            add(entry)
    return episodes


def ingest_youtube(
    section: YouTubeSection,
    cache_dir: Path,
    client: httpx.Client | None = None,
    api: Any | None = None,
    languages: Sequence[str] | None = None,
    project_root: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> Iterator[Document | IngestFailure]:
    """Yield one Document (or IngestFailure) per YouTube episode in the manifest."""
    project_root = Path(project_root or Path.cwd())
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    languages = list(languages or ["en"])

    owns_client = client is None
    client = client or make_client()
    try:
        try:
            episodes = _merge_episodes(section, project_root)
        except IngestError as exc:
            yield IngestFailure(SourceKind.YOUTUBE, section.urls_file or "", str(exc), exc.hint)
            return

        for entry in episodes:
            try:
                video_id = extract_video_id(entry.url)
            except IngestError as exc:
                yield IngestFailure(SourceKind.YOUTUBE, entry.url, str(exc), exc.hint)
                continue

            if log:
                log(f"YouTube: {entry.title or video_id}")

            try:
                segments = _segments_for(entry, video_id, cache_dir, api, languages, project_root)
            except IngestError as exc:
                yield IngestFailure(SourceKind.YOUTUBE, watch_url(video_id), str(exc), exc.hint)
                continue
            except Exception as exc:  # never let one video stop the run
                yield IngestFailure(SourceKind.YOUTUBE, watch_url(video_id), str(exc), None)
                continue

            title, author = (entry.title, None)
            if not title:
                title, author = fetch_video_title(video_id, client)

            locator = watch_url(video_id)
            document = Document(
                id=Document.make_id(SourceKind.YOUTUBE, locator),
                kind=SourceKind.YOUTUBE,
                title=title,
                locator=locator,
                text=segments_to_text(segments),
                segments=segments,
                link=locator,
                published_at=entry.published_at,
                episode=entry.episode,
                channel=section.channel_name or author,
                metadata={
                    "video_id": video_id,
                    "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            )
            document.ensure_hash()
            yield document
    finally:
        if owns_client:
            client.close()


def _segments_for(
    entry: YouTubeEpisode,
    video_id: str,
    cache_dir: Path,
    api: Any | None,
    languages: Sequence[str],
    project_root: Path,
) -> list[Segment]:
    if entry.transcript_file:
        path = Path(entry.transcript_file)
        if not path.is_absolute():
            path = project_root / path
        if not path.exists():
            raise IngestError(
                f"transcript_file not found: {path}",
                hint="Check the path in sources.yaml (it is relative to the project folder).",
            )
        return parse_transcript_file(path)

    cache_path = cache_dir / f"{video_id}.json"
    if cache_path.exists():
        try:
            return _snippets_to_segments(json.loads(cache_path.read_text(encoding="utf-8")))
        except Exception as exc:
            logger.debug("ignoring bad cache %s: %s", cache_path, exc)

    segments = fetch_transcript_segments(video_id, api=api, languages=languages)
    cache_path.write_text(
        json.dumps(
            [{"text": s.text, "start": s.start, "duration": s.end - s.start} for s in segments]
        ),
        encoding="utf-8",
    )
    return segments
