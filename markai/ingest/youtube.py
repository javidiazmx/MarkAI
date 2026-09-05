"""Turns designated YouTube videos into Documents using their captions.

Captions come from ``youtube-transcript-api`` (no API key, but YouTube rate-limits it), and
titles from the public oEmbed endpoint. When a video has no captions the owner can drop a
transcript file next to it in the manifest instead.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from markai.config import Settings
from markai.ingest.transcripts import (
    _parse_vtt,
    parse_transcript_file,
    segments_to_text,
    slugify,
)
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

# Each block below now costs a full backoff ladder, so two in a row is already ~17 minutes
# of waiting. Stopping then is kinder than pretending the block will lift.
MAX_CONSECUTIVE_BLOCKS = 2

# How long to wait out a block before trying the same video again. YouTube throttles by IP
# and lifts it after a while; abandoning the run on the first block cost a whole session for
# 47 videos out of 1142.
BACKOFF_SECONDS = (30.0, 60.0, 120.0, 300.0)

# Indirection so tests never actually sleep.
_sleep: Callable[[float], None] = time.sleep


class RateLimitedError(IngestError):
    """YouTube refused the request. Every following video will be refused too."""


class NoCaptionsError(IngestError):
    """This video has no captions - a fact about the video, not about the network.

    Worth its own type: when one route is blocked and the other answers this, there is
    nothing left to wait for, so the run moves on instead of spending the backoff ladder.
    """


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
    if is_channel_url(value):
        raise IngestError(
            f"{value} is a channel, not a video.",
            hint="Put channel URLs under youtube.channels, not youtube.episodes.",
        )
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
        raise NoCaptionsError(
            f"No captions available for {video_id}.", hint=_NO_CAPTIONS_HINT
        ) from exc
    except (yta.RequestBlocked, yta.IpBlocked) as exc:
        raise RateLimitedError(
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


_CHANNEL_RE = re.compile(
    r"youtube\.com/(?:@[\w.-]+|(?:c|channel|user)/[\w.-]+)(?:/(?:videos|streams|shorts|featured))?/?$",
    re.IGNORECASE,
)


def is_channel_url(url: str) -> bool:
    """True for a channel or handle URL, which has no video id to extract."""
    return bool(_CHANNEL_RE.search((url or "").strip()))


def _channel_slug(url: str) -> str:
    return slugify(url.split("youtube.com/", 1)[-1]) or "channel"


def _is_short(entry: dict[str, Any], max_short_seconds: int) -> bool:
    """True for a YouTube Short: the /shorts/ URL form, or a very brief video."""
    url = str(entry.get("url") or entry.get("webpage_url") or "")
    if "/shorts/" in url:
        return True
    duration = entry.get("duration")
    if duration is None:
        return False  # never drop something we could not measure
    try:
        return float(duration) <= max_short_seconds
    except (TypeError, ValueError):
        return False


def _list_channel_videos(url: str, limit: int | None) -> list[dict[str, Any]]:
    """Ask yt-dlp for a channel's video list. No media is downloaded."""
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError as exc:  # pragma: no cover - yt-dlp is a hard dependency
        raise IngestError(
            "yt-dlp is not installed, so channels cannot be expanded.",
            hint='Reinstall Mark with: pip install -e "." ',
        ) from exc

    # /videos keeps it to uploads; the bare handle URL also returns the channel's tabs.
    target = url.rstrip("/")
    if not target.endswith(("/videos", "/streams", "/shorts")):
        target = f"{target}/videos"

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",  # list entries only, never resolve each video
        "skip_download": True,
        "ignoreerrors": True,
    }

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(target, download=False)
    except DownloadError as exc:
        raise IngestError(
            f"Could not read the channel {url}: {exc}",
            hint=(
                "Check the channel URL in sources.yaml. If YouTube is rate-limiting this "
                "machine, wait and re-run; the video list is cached once it succeeds."
            ),
        ) from exc
    except Exception as exc:
        raise IngestError(f"Could not read the channel {url}: {exc}", hint=None) from exc

    entries = [e for e in (info or {}).get("entries") or [] if e]
    if not entries:
        raise IngestError(
            f"The channel {url} returned no videos.",
            hint="Confirm the channel has public videos and that the URL is right.",
        )
    return entries


CHANNEL_CACHE_VERSION = 2


def expand_channel(
    url: str,
    cache_dir: Path,
    limit: int | None = None,
    refresh: bool = False,
    skip_shorts: bool = True,
    max_short_seconds: int = 180,
    lister: Callable[[str, int | None], list[dict[str, Any]]] | None = None,
) -> list[YouTubeEpisode]:
    """Every video on a channel, as episodes, newest first.

    The raw listing is cached rather than the filtered result, so changing ``skip_shorts``
    or the limit takes effect without re-reading the channel. Shorts are dropped before the
    limit is applied, otherwise a channel full of them would yield only a handful of videos.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"channel-{_channel_slug(url)}.json"

    entries: list[dict[str, Any]] | None = None
    if cache_path.exists() and not refresh:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("version") == CHANNEL_CACHE_VERSION:
                entries = cached.get("entries") or []
        except Exception as exc:
            logger.debug("ignoring bad channel cache %s: %s", cache_path, exc)

    if entries is None:
        entries = (lister or _list_channel_videos)(url, None)
        cache_path.write_text(
            json.dumps({"version": CHANNEL_CACHE_VERSION, "entries": entries}, indent=1),
            encoding="utf-8",
        )

    episodes: list[YouTubeEpisode] = []
    seen: set[str] = set()
    shorts = 0
    for entry in entries:
        # yt-dlp yields None for videos it could not read (private, removed, geo-blocked).
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id")
        if not video_id or not _ID_RE.match(str(video_id)) or video_id in seen:
            continue
        if skip_shorts and _is_short(entry, max_short_seconds):
            shorts += 1
            continue
        seen.add(video_id)
        episodes.append(YouTubeEpisode(url=watch_url(video_id), title=(entry.get("title") or None)))
        if limit and len(episodes) >= limit:
            break

    logger.info("channel %s: %d videos kept, %d shorts skipped", url, len(episodes), shorts)
    return episodes


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


def _merge_episodes(
    section: YouTubeSection,
    project_root: Path,
    cache_dir: Path | None = None,
    refresh: bool = False,
    lister: Callable[[str, int | None], list[dict[str, Any]]] | None = None,
) -> list[YouTubeEpisode]:
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

    # Hand-written entries win, so their titles and episode numbers survive expansion.
    for entry in section.episodes:
        add(entry)
    if section.urls_file:
        for entry in read_urls_file(project_root / section.urls_file):
            add(entry)
    for channel in section.channels:
        for entry in expand_channel(
            channel,
            cache_dir if cache_dir is not None else project_root,
            limit=section.max_videos_per_channel,
            refresh=refresh,
            skip_shorts=section.skip_shorts,
            max_short_seconds=section.max_short_seconds,
            lister=lister,
        ):
            add(entry)
    return episodes


def captions_via_ytdlp(
    video_id: str,
    languages: Sequence[str],
    client: httpx.Client,
    cookies_from_browser: str | None = None,
    extractor: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> list[Segment]:
    """Second way to get captions, for when the first one is blocked.

    yt-dlp is already a dependency here (it lists the channels) and it talks to YouTube over
    a different client than the caption library, so it often works when that one is blocked.
    It can also read cookies straight out of an installed browser, which needs no export and
    no extension - just the browser's name.

    Raises ``RateLimitedError`` when YouTube blocks this route too, so the caller can treat
    both routes the same way.
    """
    options: dict[str, Any] = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _QuietLogger(),  # yt-dlp writes to stderr otherwise, once per blocked video
    }
    if cookies_from_browser:
        options["cookiesfrombrowser"] = (cookies_from_browser.strip().lower(),)

    info = (extractor or _ytdlp_extract)(watch_url(video_id), options)
    tracks = {**(info.get("automatic_captions") or {}), **(info.get("subtitles") or {})}
    if not tracks:
        raise NoCaptionsError(f"No captions available for {video_id}.", hint=_NO_CAPTIONS_HINT)

    url = _pick_caption_track(tracks, languages)
    if url is None:
        available = ", ".join(sorted(tracks)[:8]) or "none"
        raise IngestError(
            f"No captions for {video_id} in {', '.join(languages)}.",
            hint=f"Languages this video does have: {available}.",
        )

    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RateLimitedError(
            f"YouTube blocked the caption download for {video_id}.", hint=_BLOCKED_HINT
        ) from exc

    segments = _parse_vtt(response.text)
    if not segments:
        raise IngestError(f"Captions for {video_id} came back empty.", hint=_NO_CAPTIONS_HINT)
    return segments


class _QuietLogger:
    """yt-dlp writes straight to stderr unless it is handed a logger."""

    def debug(self, message: str) -> None:
        logger.debug("yt-dlp: %s", message)

    info = debug
    warning = debug

    def error(self, message: str) -> None:
        logger.debug("yt-dlp error: %s", message)


def _ytdlp_extract(url: str, options: dict[str, Any]) -> dict[str, Any]:
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception as exc:
        message = str(exc).lower()
        if "sign in" in message or "bot" in message or "429" in message or "blocked" in message:
            raise RateLimitedError(
                f"YouTube blocked yt-dlp for {url}.", hint=_BLOCKED_HINT
            ) from exc
        if "cookies" in message or "keyring" in message or "could not copy" in message:
            raise IngestError(
                f"Could not read cookies from the browser: {exc}",
                hint=(
                    "Close the browser completely and try again, or name a different one in "
                    "MARKAI_YOUTUBE_COOKIES_FROM_BROWSER (firefox is the most reliable)."
                ),
            ) from exc
        raise IngestError(f"yt-dlp could not read {url}: {exc}", hint=_NO_CAPTIONS_HINT) from exc


def _pick_caption_track(tracks: dict[str, Any], languages: Sequence[str]) -> str | None:
    """The VTT URL for the best matching language: exact match first, then a prefix like en-GB."""
    for wanted in languages:
        for code in (wanted, wanted.split("-")[0]):
            for available, formats in tracks.items():
                if available != code and not available.startswith(f"{code}-"):
                    continue
                for fmt in formats or []:
                    if str(fmt.get("ext", "")).lower() == "vtt" and fmt.get("url"):
                        return str(fmt["url"])
    return None


def build_transcript_api(settings: Settings) -> Any:
    """A caption client set up the way ``settings`` asks.

    Once YouTube blocks an address, pacing is beside the point: every request from it fails.
    The two ways out are a different address (a proxy) or a signed-in identity (cookies
    exported from a browser). Both are optional; with neither, this is the plain client.
    """
    import youtube_transcript_api as yta
    from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig

    proxy_config = None
    if settings.webshare_username and settings.webshare_secret():
        proxy_config = WebshareProxyConfig(
            proxy_username=settings.webshare_username,
            proxy_password=settings.webshare_secret() or "",
        )
    elif settings.youtube_proxy():
        url = settings.youtube_proxy()
        proxy_config = GenericProxyConfig(http_url=url, https_url=url)

    http_client = None
    if settings.youtube_cookies_file:
        http_client = _session_with_cookies(settings.youtube_cookies_file, settings.project_root)

    # Log the method, never the secret.
    logger.info("youtube caption client: %s", settings.youtube_unblock_method())
    if http_client is not None:
        return yta.YouTubeTranscriptApi(proxy_config=proxy_config, http_client=http_client)
    return yta.YouTubeTranscriptApi(proxy_config=proxy_config)


def _session_with_cookies(path: Path, project_root: Path) -> Any:
    """A requests session carrying cookies exported from a signed-in browser."""
    import http.cookiejar

    import requests

    cookie_path = Path(path)
    if not cookie_path.is_absolute():
        cookie_path = project_root / cookie_path
    if not cookie_path.exists():
        raise IngestError(
            f"Cookie file not found: {cookie_path}",
            hint="Export cookies.txt from a signed-in browser, or clear "
            "MARKAI_YOUTUBE_COOKIES_FILE.",
        )
    jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        raise IngestError(
            f"Could not read {cookie_path.name}: {exc}",
            hint="It must be a Netscape-format cookies.txt, not a JSON export.",
        ) from exc
    session = requests.Session()
    session.cookies = jar  # type: ignore[assignment]
    return session


def _segments_with_backoff(
    entry: YouTubeEpisode,
    video_id: str,
    cache_dir: Path,
    api: Any | None,
    languages: Sequence[str],
    project_root: Path,
    log: Callable[[str], None] | None,
    fallback: Callable[[str], list[Segment]] | None = None,
) -> list[Segment]:
    """Fetch captions, trying the other route and waiting out a block before giving up."""
    last: RateLimitedError | None = None
    for wait in (*BACKOFF_SECONDS, None):
        try:
            return _segments_for(entry, video_id, cache_dir, api, languages, project_root)
        except RateLimitedError as exc:
            last = exc
            # Before waiting, try the other route. yt-dlp talks to YouTube differently and
            # is often not blocked at the same moment, so this frequently just works.
            if fallback is not None:
                try:
                    segments = fallback(video_id)
                except NoCaptionsError:
                    raise  # a fact about the video: nothing to wait for
                except IngestError:
                    pass  # the other route failed too, whatever the reason: wait and retry
                else:
                    _cache_segments(cache_dir, video_id, segments)
                    return segments
            if wait is None:
                break
            if log:
                log(f"YouTube is throttling. Waiting {wait:.0f}s, then retrying {video_id}.")
            _sleep(wait)
    assert last is not None
    raise last


def ingest_youtube(
    section: YouTubeSection,
    cache_dir: Path,
    client: httpx.Client | None = None,
    api: Any | None = None,
    languages: Sequence[str] | None = None,
    project_root: Path | None = None,
    refresh_channels: bool = False,
    lister: Callable[[str, int | None], list[dict[str, Any]]] | None = None,
    log: Callable[[str], None] | None = None,
    delay_seconds: float = 0.0,
    cookies_from_browser: str | None = None,
) -> Iterator[Document | IngestFailure]:
    """Yield one Document (or IngestFailure) per YouTube episode in the manifest."""
    project_root = Path(project_root or Path.cwd())
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    languages = list(languages or ["en"])

    owns_client = client is None
    client = client or make_client()

    def fallback(video_id: str) -> list[Segment]:
        return captions_via_ytdlp(video_id, languages or ["en"], client, cookies_from_browser)

    try:
        try:
            episodes = _merge_episodes(section, project_root, cache_dir, refresh_channels, lister)
        except IngestError as exc:
            locator = section.urls_file or (section.channels[0] if section.channels else "youtube")
            yield IngestFailure(SourceKind.YOUTUBE, locator, str(exc), exc.hint)
            return
        if log and section.channels:
            log(f"YouTube: {len(episodes)} videos across {len(section.channels)} channel(s)")

        blocked_in_a_row = 0
        for index, entry in enumerate(episodes, start=1):
            try:
                video_id = extract_video_id(entry.url)
            except IngestError as exc:
                yield IngestFailure(SourceKind.YOUTUBE, entry.url, str(exc), exc.hint)
                continue

            if log:
                log(f"YouTube: {entry.title or video_id}")

            if delay_seconds and index > 1:
                # Politeness, and self-interest: hammering the caption endpoint is what got
                # the machine blocked after 47 videos out of 1142.
                _sleep(delay_seconds)

            try:
                segments = _segments_with_backoff(
                    entry, video_id, cache_dir, api, languages, project_root, log, fallback
                )
                blocked_in_a_row = 0
            except RateLimitedError as exc:
                blocked_in_a_row += 1
                if blocked_in_a_row >= MAX_CONSECUTIVE_BLOCKS:
                    yield IngestFailure(
                        SourceKind.YOUTUBE,
                        watch_url(video_id),
                        f"YouTube kept blocking after {blocked_in_a_row} rounds of waiting, so "
                        f"the remaining {len(episodes) - index} videos were skipped.",
                        "Wait an hour and run `mark ingest` again. Everything already downloaded "
                        "is cached, so it picks up where it stopped. Raising "
                        "MARKAI_YOUTUBE_DELAY_SECONDS makes a block less likely next time.",
                    )
                    return
                yield IngestFailure(SourceKind.YOUTUBE, watch_url(video_id), str(exc), exc.hint)
                continue
            except IngestError as exc:
                blocked_in_a_row = 0
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
    _cache_segments(cache_dir, video_id, segments)
    return segments


def _cache_segments(cache_dir: Path, video_id: str, segments: list[Segment]) -> None:
    """Store captions so a re-run costs nothing, whichever route fetched them."""
    (cache_dir / f"{video_id}.json").write_text(
        json.dumps(
            [{"text": s.text, "start": s.start, "duration": s.end - s.start} for s in segments]
        ),
        encoding="utf-8",
    )
