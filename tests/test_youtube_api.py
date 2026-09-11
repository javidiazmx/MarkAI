"""The official YouTube Data API v3 route: only reachable when a channel owner authorizes it."""

from __future__ import annotations

import pytest

from markai.ingest.youtube import NoCaptionsError
from markai.ingest.youtube_api import (
    OAuthNotConfigured,
    _pick_track,
    build_youtube_api_client,
    captions_via_api,
)
from markai.models import IngestError

VIDEO = "dQw4w9WgXcQ"

SRT = "1\n00:00:00,000 --> 00:00:01,000\nHello from the owner's channel.\n"


class FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeCaptions:
    def __init__(self, list_result, download_result=None):
        self._list_result = list_result
        self._download_result = download_result
        self.downloaded_id: str | None = None

    def list(self, part, videoId):
        return FakeExecutable(self._list_result)

    def download(self, id, tfmt):
        self.downloaded_id = id
        return FakeExecutable(self._download_result)


class FakeClient:
    def __init__(self, captions: FakeCaptions):
        self._captions = captions

    def captions(self):
        return self._captions


def test_build_client_without_a_secret_file_is_opt_in_and_skipped(tmp_path):
    with pytest.raises(OAuthNotConfigured):
        build_youtube_api_client(tmp_path / "missing.json", tmp_path / "token.json")


def test_pick_track_matches_exact_language():
    items = [
        {"id": "a", "snippet": {"language": "es"}},
        {"id": "b", "snippet": {"language": "en"}},
    ]
    assert _pick_track(items, ["en"])["id"] == "b"


def test_pick_track_matches_a_regional_variant():
    items = [{"id": "a", "snippet": {"language": "en-US"}}]
    assert _pick_track(items, ["en"])["id"] == "a"


def test_pick_track_returns_none_when_nothing_matches():
    items = [{"id": "a", "snippet": {"language": "de"}}]
    assert _pick_track(items, ["en"]) is None


def test_no_caption_tracks_at_all_is_a_no_captions_error():
    client = FakeClient(FakeCaptions(list_result={"items": []}))
    with pytest.raises(NoCaptionsError):
        captions_via_api(client, VIDEO, ["en"])


def test_tracks_exist_but_not_in_the_requested_language():
    client = FakeClient(
        FakeCaptions(list_result={"items": [{"id": "a", "snippet": {"language": "de"}}]})
    )
    with pytest.raises(IngestError) as exc_info:
        captions_via_api(client, VIDEO, ["en"])
    assert "de" in str(exc_info.value.hint)


def test_a_matching_track_is_downloaded_and_parsed():
    client = FakeClient(
        FakeCaptions(
            list_result={"items": [{"id": "a", "snippet": {"language": "en"}}]},
            download_result=SRT.encode("utf-8"),
        )
    )
    segments = captions_via_api(client, VIDEO, ["en"])
    assert segments and "owner's channel" in segments[0].text
    assert client._captions.downloaded_id == "a"


def test_an_empty_download_is_reported_rather_than_silently_accepted():
    client = FakeClient(
        FakeCaptions(
            list_result={"items": [{"id": "a", "snippet": {"language": "en"}}]},
            download_result=b"",
        )
    )
    with pytest.raises(IngestError):
        captions_via_api(client, VIDEO, ["en"])


def test_a_listing_error_is_wrapped_as_an_ingest_error():
    from googleapiclient.errors import HttpError

    class FakeResp:
        status = 403
        reason = "Forbidden"

    client = FakeClient(FakeCaptions(list_result=HttpError(FakeResp(), b"not your channel")))
    with pytest.raises(IngestError):
        captions_via_api(client, VIDEO, ["en"])
