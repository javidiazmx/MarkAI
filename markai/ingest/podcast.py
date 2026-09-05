"""Turns podcast episodes into Documents, preferring transcripts over transcription.

Order of preference per episode: an explicit ``transcript_file``, a matching file in
``data/raw/podcast/transcripts``, a ``<podcast:transcript>`` URL from the feed, and only then
local speech-to-text (which needs the optional ``markai[transcribe]`` extra and runs at
roughly real time).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from markai.config import Settings
from markai.ingest.transcripts import parse_transcript_file, segments_to_text, slugify
from markai.ingest.websites import canonical_url, make_client
from markai.models import Document, IngestError, IngestFailure, Segment, SourceKind
from markai.sources.manifest import PodcastEpisode, PodcastSection

logger = logging.getLogger(__name__)

TRANSCRIPT_SUFFIXES = (".srt", ".vtt", ".txt", ".json")
NO_TRANSCRIPT_HINT = (
    "Add a transcript to data/raw/podcast/transcripts/ (export .srt or .txt from your podcast "
    "host, Descript, Riverside, or Otter), add the YouTube URL of this episode to sources.yaml "
    'instead, or install local transcription with: pip install "markai[transcribe]"'
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _parse_duration(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if ":" in text:
            parts = [float(p) for p in text.split(":")]
            seconds = 0.0
            for part in parts:
                seconds = seconds * 60 + part
            return seconds
        return float(text)
    except ValueError:
        return None


def load_feed_episodes(
    rss_url: str,
    include_titles: list[str],
    max_episodes: int | None,
    client: httpx.Client | None = None,
) -> list[PodcastEpisode]:
    """Read a podcast RSS feed and return the episodes the manifest asks for."""
    import feedparser

    owns_client = client is None
    client = client or make_client()
    try:
        response = client.get(rss_url)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except httpx.HTTPError as exc:
        raise IngestError(
            f"Could not download the podcast feed: {exc}",
            hint="Check the RSS URL in sources.yaml, or paste it into a browser to confirm it.",
        ) from exc
    finally:
        if owns_client:
            client.close()

    wanted = [t.strip().lower() for t in (include_titles or []) if t.strip()]
    episodes: list[PodcastEpisode] = []

    for entry in getattr(parsed, "entries", []):
        title = (entry.get("title") or "").strip()
        episode_number = entry.get("itunes_episode")
        episode_number = str(episode_number).strip() if episode_number else None

        if wanted:
            low = title.lower()
            if not any(
                (term in low) or (term.isdigit() and episode_number == term) for term in wanted
            ):
                continue

        audio_url = None
        for enclosure in entry.get("enclosures", []) or []:
            href = enclosure.get("href")
            if not href:
                continue
            if str(enclosure.get("type", "")).startswith("audio/"):
                audio_url = href
                break
            audio_url = audio_url or href

        transcript = entry.get("podcast_transcript") or {}
        transcript_url = transcript.get("url") if isinstance(transcript, dict) else None

        published_at = None
        published_parsed = entry.get("published_parsed")
        if published_parsed:
            published_at = datetime(*published_parsed[:6], tzinfo=UTC).date().isoformat()

        episodes.append(
            PodcastEpisode(
                title=title or None,
                episode=episode_number,
                episode_url=entry.get("link"),
                audio_url=audio_url,
                transcript_url=transcript_url,
                published_at=published_at,
                duration_seconds=_parse_duration(entry.get("itunes_duration")),
                show_notes=_show_notes(entry),
            )
        )

    if max_episodes:
        episodes = episodes[:max_episodes]
    return episodes


# Below this, a description is a one-line teaser rather than something worth storing.
MIN_SHOW_NOTES_WORDS = 40


def _show_notes(entry: Any) -> str | None:
    """The episode's own notes, as plain text.

    Show notes are the one piece of real content a feed always carries: guests, topics,
    timestamps, links. Ignoring them is why a 480-episode feed produced 480 failures and
    nothing else.
    """
    from bs4 import BeautifulSoup

    raw = ""
    contents = entry.get("content") or []
    if contents and isinstance(contents, list):
        raw = str(contents[0].get("value") or "")
    for key in ("summary", "description", "itunes_summary", "subtitle"):
        if len(raw.strip()) < 200:
            candidate = str(entry.get(key) or "")
            if len(candidate) > len(raw):
                raw = candidate
    if not raw.strip():
        return None

    soup = BeautifulSoup(raw, "lxml")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all(["br", "li"]):
        tag.append("\n")
    for tag in soup.find_all(["p", "div", "h1", "h2", "h3", "h4"]):
        tag.append("\n\n")  # a real blank line, because the chunker splits on paragraphs
    text = re.sub(r"[ \t]+", " ", soup.get_text(" "))
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def download_file(url: str, dest_dir: Path, client: httpx.Client) -> Path:
    """Stream a file to ``dest_dir``, naming it after the URL path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = Path(httpx.URL(url).path).name or "download"
    suffix = Path(name).suffix
    dest = dest_dir / f"{slugify(Path(name).stem) or 'download'}{suffix}"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with dest.open("wb") as handle:
                for block in response.iter_bytes():
                    handle.write(block)
    except httpx.HTTPError as exc:
        raise IngestError(f"Download failed for {url}: {exc}", hint=NO_TRANSCRIPT_HINT) from exc
    return dest


def transcribe_audio(path: Path, model_size: str) -> list[Segment]:
    """Local speech-to-text via faster-whisper (optional extra)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise IngestError("faster-whisper is not installed.", hint=NO_TRANSCRIPT_HINT) from exc

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    raw_segments, _info = model.transcribe(str(path), vad_filter=True)
    segments = [
        Segment(start=float(s.start), end=float(s.end), text=s.text.strip())
        for s in raw_segments
        if s.text and s.text.strip()
    ]
    if not segments:
        raise IngestError(
            f"Transcription produced no text for {path.name}.", hint=NO_TRANSCRIPT_HINT
        )
    return segments


def match_transcript_file(
    episode: PodcastEpisode, transcripts_dir: Path, project_root: Path
) -> Path | None:
    """Find the transcript file for an episode: explicit path, episode number, or title match."""
    if episode.transcript_file:
        path = Path(episode.transcript_file)
        if not path.is_absolute():
            path = project_root / path
        return path if path.exists() else None

    transcripts_dir = Path(transcripts_dir)
    if not transcripts_dir.exists():
        return None
    candidates = [
        p for p in sorted(transcripts_dir.iterdir()) if p.suffix.lower() in TRANSCRIPT_SUFFIXES
    ]

    if episode.episode and str(episode.episode).strip().isdigit():
        number = str(episode.episode).strip()
        pattern = re.compile(rf"(?<!\d){number}(?!\d)")
        for path in candidates:
            if pattern.search(path.stem):
                return path

    if episode.title:
        title_words = [w for w in _WORD_RE.findall(episode.title.lower()) if len(w) > 2]
        if len(title_words) >= 2:
            for path in candidates:
                stem_words = set(_WORD_RE.findall(path.stem.lower()))
                hits = sum(1 for w in title_words if w in stem_words)
                if hits / len(title_words) >= 0.8:
                    return path
    return None


def _merge_episodes(
    section: PodcastSection, client: httpx.Client | None
) -> tuple[list[PodcastEpisode], IngestFailure | None]:
    episodes = list(section.episodes)
    if not section.rss:
        return episodes, None
    try:
        feed_episodes = load_feed_episodes(
            section.rss, section.include_titles, section.max_episodes, client
        )
    except IngestError as exc:
        return episodes, IngestFailure(SourceKind.PODCAST, section.rss, str(exc), exc.hint)

    known_numbers = {e.episode for e in episodes if e.episode}
    known_titles = {(e.title or "").strip().lower() for e in episodes if e.title}
    for entry in feed_episodes:
        if entry.episode and entry.episode in known_numbers:
            continue
        if entry.title and entry.title.strip().lower() in known_titles:
            continue
        episodes.append(entry)
    return episodes, None


def resolve_transcript_plan(
    section: PodcastSection, settings: Settings, client: httpx.Client | None = None
) -> list[tuple[PodcastEpisode, str]]:
    """Decide how each episode would be transcribed, without downloading audio.

    Raises ``IngestError`` when the feed cannot be read: an empty plan and an unreachable
    feed mean very different things to whoever is about to run an ingest.
    """
    episodes, failure = _merge_episodes(section, client)
    if failure and not episodes:
        raise IngestError(failure.reason, hint=failure.hint)
    plan: list[tuple[PodcastEpisode, str]] = []
    for episode in episodes:
        if match_transcript_file(episode, settings.podcast_transcripts_dir, settings.project_root):
            method = "transcript_file"
        elif episode.transcript_url:
            method = "transcript_url"
        elif episode.audio_file or episode.audio_url:
            method = "audio_transcribe"
        else:
            method = "unavailable"
        plan.append((episode, method))
    return plan


def _episode_locator(episode: PodcastEpisode) -> str:
    if episode.audio_url:
        return canonical_url(episode.audio_url)
    local = episode.audio_file or episode.transcript_file
    if local:
        return f"file:{Path(local).as_posix()}"
    return f"episode:{episode.episode or slugify(episode.title or 'untitled')}"


def ingest_podcast(
    section: PodcastSection,
    settings: Settings,
    cache_dir: Path,
    client: httpx.Client | None = None,
    allow_transcription: bool = True,
    log: Callable[[str], None] | None = None,
) -> Iterator[Document | IngestFailure]:
    """Yield one Document (or IngestFailure) per podcast episode."""
    owns_client = client is None
    client = client or make_client(settings.http_timeout_seconds)
    transcripts_dir = settings.podcast_transcripts_dir
    audio_dir = settings.podcast_audio_dir
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    try:
        episodes, failure = _merge_episodes(section, client)
        if failure:
            yield failure

        for episode in episodes:
            label = episode.title or episode.episode or episode.audio_url or "episode"
            if log:
                log(f"Podcast: {label}")
            try:
                segments, method = _segments_for_episode(
                    episode, settings, transcripts_dir, audio_dir, client, allow_transcription
                )
            except IngestError as exc:
                # No transcript is not the same as nothing to learn. The feed's own show
                # notes name the guest, the topics and the links, which is real material
                # and already downloaded.
                notes = (episode.show_notes or "").strip()
                if len(notes.split()) < MIN_SHOW_NOTES_WORDS:
                    yield IngestFailure(
                        SourceKind.PODCAST, _episode_locator(episode), str(exc), exc.hint
                    )
                    continue
                segments, method = [], "show_notes"
            except Exception as exc:
                yield IngestFailure(SourceKind.PODCAST, _episode_locator(episode), str(exc), None)
                continue

            locator = _episode_locator(episode)
            document = Document(
                id=Document.make_id(SourceKind.PODCAST, locator),
                kind=SourceKind.PODCAST,
                title=episode.title or f"Episode {episode.episode or ''}".strip(),
                locator=locator,
                text=(episode.show_notes or "")
                if method == "show_notes"
                else (segments_to_text(segments)),
                segments=segments,
                link=episode.episode_url or episode.audio_url,
                published_at=episode.published_at,
                episode=episode.episode,
                channel=section.show_name,
                metadata={
                    "duration_seconds": episode.duration_seconds,
                    "transcript_method": method,
                },
            )
            document.ensure_hash()
            yield document
    finally:
        if owns_client:
            client.close()


def _segments_for_episode(
    episode: PodcastEpisode,
    settings: Settings,
    transcripts_dir: Path,
    audio_dir: Path,
    client: httpx.Client,
    allow_transcription: bool,
) -> tuple[list[Segment], str]:
    path = match_transcript_file(episode, transcripts_dir, settings.project_root)
    if path:
        return parse_transcript_file(path), "transcript_file"

    if episode.transcript_url:
        downloaded = download_file(episode.transcript_url, transcripts_dir, client)
        if downloaded.suffix.lower() in TRANSCRIPT_SUFFIXES:
            return parse_transcript_file(downloaded), "transcript_url"

    audio_path: Path | None = None
    if episode.audio_file:
        candidate = Path(episode.audio_file)
        if not candidate.is_absolute():
            candidate = settings.project_root / candidate
        if candidate.exists():
            audio_path = candidate
    if audio_path is None and episode.audio_url and allow_transcription:
        audio_path = download_file(episode.audio_url, audio_dir, client)

    if audio_path is None:
        raise IngestError(
            f"No transcript available for {episode.title or episode.episode or 'this episode'}.",
            hint=NO_TRANSCRIPT_HINT,
        )
    if not allow_transcription:
        raise IngestError(
            f"Transcription is disabled for {episode.title or 'this episode'}.",
            hint=NO_TRANSCRIPT_HINT,
        )
    return transcribe_audio(audio_path, settings.transcribe_model), "audio_transcribe"
