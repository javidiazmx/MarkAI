"""The source manifest: the YAML file where the owner lists what Mark may learn from.

``sources/sources.yaml`` is the single intake point. Every ingest run reads it, so adding a
new website, YouTube episode, or podcast episode is a matter of adding a line and re-running
``mark ingest``. If a ``sources.local.yaml`` sits next to it, that file is used instead (it is
git-ignored, for private feed URLs).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

SECRET_QUERY_KEYS = re.compile(r"(?i)\b(token|auth|key|secret|sig|signature|password|pwd)=")


class WebsiteSource(BaseModel):
    """A web page (or, with ``crawl: true``, a same-domain section of a site)."""

    url: str
    title: str | None = None
    crawl: bool = False
    max_pages: int = Field(default=25, ge=1, le=500)
    include_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)
    ignore_robots: bool = False
    notes: str | None = None

    @field_validator("url")
    @classmethod
    def _must_be_http(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"website url must start with http:// or https:// (got {value!r})")
        return value


class YouTubeEpisode(BaseModel):
    """One YouTube video. ``url`` accepts any YouTube URL form or a bare 11-character id."""

    url: str
    title: str | None = None
    episode: str | None = None
    published_at: str | None = None
    transcript_file: str | None = None
    notes: str | None = None


class YouTubeSection(BaseModel):
    """YouTube material.

    ``channels`` lists whole channels; every video on them is picked up, and new uploads
    arrive on the next ingest. ``urls_file`` is a plain text file with one URL per line
    (``#`` comments allowed; an optional ``| Episode title`` suffix after the URL).
    ``episodes`` is for naming individual videos by hand.
    """

    channels: list[str] = Field(default_factory=list)
    max_videos_per_channel: int | None = Field(default=None, ge=1)
    skip_shorts: bool = True
    max_short_seconds: int = Field(default=180, ge=0)
    channel_url: str | None = None
    channel_name: str | None = None
    urls_file: str | None = None
    episodes: list[YouTubeEpisode] = Field(default_factory=list)

    def has_sources(self) -> bool:
        return bool(self.channels or self.episodes or self.urls_file)


class PodcastEpisode(BaseModel):
    """One podcast episode, supplied as a transcript file, an audio file, or an audio URL."""

    title: str | None = None
    episode: str | None = None
    episode_url: str | None = None
    audio_url: str | None = None
    audio_file: str | None = None
    transcript_file: str | None = None
    transcript_url: str | None = None
    published_at: str | None = None
    duration_seconds: float | None = None
    notes: str | None = None


class PodcastSection(BaseModel):
    show_name: str | None = None
    rss: str | None = None
    include_titles: list[str] = Field(default_factory=list)
    max_episodes: int | None = Field(default=None, ge=1)
    episodes: list[PodcastEpisode] = Field(default_factory=list)


class ToolLink(BaseModel):
    """A calculator or tool the owner wants Mark to recommend when relevant."""

    name: str
    description: str
    url: str | None = None
    when_to_recommend: str | None = None


class BusinessProfile(BaseModel):
    """Owner-supplied context appended to Mark's system prompt (stable per process)."""

    name: str | None = None
    services: str | None = None
    contact_url: str | None = None
    contact_email: str | None = None
    service_area: str | None = None
    never_say: list[str] = Field(default_factory=list)
    extra_instructions: str | None = None

    def is_empty(self) -> bool:
        return not any(
            (
                self.name,
                self.services,
                self.contact_url,
                self.contact_email,
                self.service_area,
                self.never_say,
                self.extra_instructions,
            )
        )


class SourceManifest(BaseModel):
    """Top-level shape of ``sources/sources.yaml``."""

    websites: list[WebsiteSource] = Field(default_factory=list)
    youtube: YouTubeSection = Field(default_factory=YouTubeSection)
    podcast: PodcastSection = Field(default_factory=PodcastSection)
    tools: list[ToolLink] = Field(default_factory=list)
    business: BusinessProfile = Field(default_factory=BusinessProfile)

    def is_empty(self) -> bool:
        return not (
            self.websites or self.youtube.has_sources() or self.podcast.episodes or self.podcast.rss
        )

    def counts(self) -> dict[str, int]:
        return {
            "websites": len(self.websites),
            "youtube_channels": len(self.youtube.channels),
            "youtube_episodes": len(self.youtube.episodes),
            "youtube_urls_file": 1 if self.youtube.urls_file else 0,
            "podcast_episodes": len(self.podcast.episodes),
            "podcast_rss": 1 if self.podcast.rss else 0,
            "tools": len(self.tools),
        }

    def warnings(self) -> list[str]:
        """Non-fatal problems worth telling the owner about (e.g. secrets inside URLs)."""
        notes: list[str] = []
        candidates: list[tuple[str, str | None]] = [("podcast.rss", self.podcast.rss)]
        candidates += [(f"websites[{i}].url", w.url) for i, w in enumerate(self.websites)]
        candidates += [
            (f"podcast.episodes[{i}].audio_url", e.audio_url)
            for i, e in enumerate(self.podcast.episodes)
        ]
        for label, value in candidates:
            if value and SECRET_QUERY_KEYS.search(value):
                notes.append(
                    f"{label} looks like it contains a private token. Keep this manifest out of "
                    "git (use sources/sources.local.yaml) or share the feed another way."
                )
        return notes


def resolve_manifest_path(path: Path | str) -> Path:
    """Prefer ``sources.local.yaml`` next to the requested manifest when it exists."""
    path = Path(path)
    local = path.with_name("sources.local.yaml")
    if local.exists():
        return local
    return path


def load_manifest(path: Path | str) -> SourceManifest:
    """Read and validate the manifest. A missing file raises; an empty file yields an
    empty manifest."""
    path = resolve_manifest_path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Sources manifest not found at {path}. Run `mark init` to create a template."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level.")
    return SourceManifest.model_validate(raw)


def save_manifest(manifest: SourceManifest, path: Path | str) -> None:
    Path(path).write_text(
        yaml.safe_dump(manifest.model_dump(exclude_none=True), sort_keys=False),
        encoding="utf-8",
    )
