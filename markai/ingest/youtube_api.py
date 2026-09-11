"""Official YouTube Data API v3 caption download, for a channel you own or manage.

Sidesteps every anti-scraping check yt-dlp and youtube-transcript-api eventually run into,
because it is not scraping: Google's own API, authorized by the channel owner through OAuth.

In practice this only covers manually-uploaded caption tracks. ``captions.download``
refuses auto-generated (ASR) tracks with a 403 ("permissions... not sufficient... video
owner might not have enabled third-party contributions") even when the caller owns the
channel - a long-standing, intentional restriction on Google's side, confirmed against a
real video on a channel this OAuth grant owns. Most of a podcast channel's episodes rely on
YouTube's auto-captions rather than an uploaded track, so this route is a narrow win, not
the fix for the blocked-scraper problem it was built to solve - which is exactly why it
falls through to the scraping routes on any IngestError rather than failing the video.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from markai.ingest.transcripts import _parse_srt
from markai.models import IngestError

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


class OAuthNotConfigured(IngestError):
    """No client secret file configured - this route is opt-in and simply skipped."""


def build_youtube_api_client(client_secret_file: Path, token_file: Path) -> Any:
    """One-time browser consent, then a cached token reused silently after that.

    ``run_local_server`` opens the system's default browser for the owner to approve access
    and briefly serves a local callback to catch the redirect - normal for an installed-app
    OAuth flow, and only happens once per token file.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    if not client_secret_file.exists():
        raise OAuthNotConfigured(
            f"No OAuth client secret at {client_secret_file}.",
            hint=(
                "Download one from Google Cloud Console (APIs & Services > Credentials > "
                "OAuth client ID > Desktop app) and set "
                "MARKAI_YOUTUBE_API_CLIENT_SECRET_FILE to its path."
            ),
        )

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def _pick_track(items: list[dict[str, Any]], languages: Sequence[str]) -> dict[str, Any] | None:
    by_lang = {item["snippet"]["language"]: item for item in items}
    for lang in languages:
        if lang in by_lang:
            return by_lang[lang]
        # "en" should also match an "en-US"-style entry the API sometimes returns.
        prefix = lang.split("-")[0]
        for code, item in by_lang.items():
            if code.split("-")[0] == prefix:
                return item
    return None


def captions_via_api(client: Any, video_id: str, languages: Sequence[str]) -> list:
    """Download the caption track for one video, authorized as its owner.

    Raises ``NoCaptionsError`` (a fact about the video, permanent) or a plain
    ``IngestError`` (this video is not covered by the caller's OAuth grant, or something
    else went wrong) - the caller falls back to the scraping routes on either.
    """
    from googleapiclient.errors import HttpError

    from markai.ingest.youtube import NoCaptionsError

    try:
        listing = client.captions().list(part="snippet", videoId=video_id).execute()
    except HttpError as exc:
        raise IngestError(f"Could not list captions for {video_id}: {exc}") from exc

    items = listing.get("items", [])
    if not items:
        raise NoCaptionsError(f"No caption tracks for {video_id}.")

    track = _pick_track(items, languages)
    if track is None:
        available = ", ".join(sorted({i["snippet"]["language"] for i in items})[:8])
        raise IngestError(
            f"No captions for {video_id} in {', '.join(languages)}.",
            hint=f"Languages this video does have: {available or 'none'}.",
        )

    try:
        body = client.captions().download(id=track["id"], tfmt="srt").execute()
    except HttpError as exc:
        raise IngestError(f"Could not download captions for {video_id}: {exc}") from exc

    text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
    segments = _parse_srt(text)
    if not segments:
        raise IngestError(f"Captions for {video_id} came back empty.")
    return segments
