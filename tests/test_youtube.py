"""YouTube ingestion: id parsing, caption fetching, caching, and useful failures."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import youtube_transcript_api as yta

from markai.ingest.youtube import (
    extract_video_id,
    fetch_transcript_segments,
    fetch_video_title,
    ingest_youtube,
    read_urls_file,
    watch_url,
)
from markai.models import Document, IngestError, IngestFailure
from markai.sources.manifest import YouTubeEpisode, YouTubeSection
from tests.fakes import FakeTranscriptApi

VIDEO = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "value",
    [
        f"https://www.youtube.com/watch?v={VIDEO}",
        f"https://www.youtube.com/watch?v={VIDEO}&t=30s",
        f"https://youtu.be/{VIDEO}",
        f"https://www.youtube.com/shorts/{VIDEO}",
        f"https://www.youtube.com/live/{VIDEO}",
        f"https://www.youtube.com/embed/{VIDEO}",
        VIDEO,
    ],
)
def test_every_url_form_yields_the_id(value):
    assert extract_video_id(value) == VIDEO


@pytest.mark.parametrize("value", ["", "https://example.com/watch?v=short", "not a url"])
def test_bad_input_is_a_helpful_error(value):
    with pytest.raises(IngestError) as excinfo:
        extract_video_id(value)
    assert excinfo.value.hint


def test_watch_url_is_canonical():
    assert watch_url(VIDEO) == f"https://www.youtube.com/watch?v={VIDEO}"


@respx.mock(assert_all_called=False)
def test_title_comes_from_oembed(respx_mock):
    respx_mock.get("https://www.youtube.com/oembed").mock(
        return_value=httpx.Response(200, json={"title": "Screening", "author_name": "SUCI"})
    )
    with httpx.Client() as client:
        assert fetch_video_title(VIDEO, client) == ("Screening", "SUCI")


@respx.mock(assert_all_called=False)
def test_title_falls_back_when_oembed_fails(respx_mock):
    respx_mock.get("https://www.youtube.com/oembed").mock(return_value=httpx.Response(404))
    with httpx.Client() as client:
        title, author = fetch_video_title(VIDEO, client)
    assert VIDEO in title and author is None


def test_segments_carry_the_requested_languages():
    api = FakeTranscriptApi()
    segments = fetch_transcript_segments(VIDEO, api=api, languages=["en", "en-US"])
    assert api.calls == [(VIDEO, ["en", "en-US"])]
    assert segments[0].end == 5.0
    assert "Security deposits" in segments[0].text


def test_disabled_captions_suggest_a_transcript_file():
    api = FakeTranscriptApi(error=yta.TranscriptsDisabled(VIDEO))
    with pytest.raises(IngestError) as excinfo:
        fetch_transcript_segments(VIDEO, api=api)
    assert "transcript_file" in excinfo.value.hint


def test_rate_limiting_says_to_wait():
    api = FakeTranscriptApi(error=yta.RequestBlocked(VIDEO))
    with pytest.raises(IngestError) as excinfo:
        fetch_transcript_segments(VIDEO, api=api)
    assert "rate-limiting" in excinfo.value.hint


def test_urls_file_parsing(tmp_path):
    path = tmp_path / "urls.txt"
    path.write_text(
        f"# a comment\n\nhttps://youtu.be/{VIDEO} | Screening episode\nhttps://youtu.be/aBcDeFgHiJk\n",
        encoding="utf-8",
    )
    episodes = read_urls_file(path)
    assert [e.title for e in episodes] == ["Screening episode", None]
    assert len(episodes) == 2


def test_missing_urls_file_is_a_clear_error(tmp_path):
    with pytest.raises(IngestError):
        read_urls_file(tmp_path / "nope.txt")


@respx.mock(assert_all_called=False)
def test_ingest_builds_a_document_and_caches_the_transcript(respx_mock, tmp_path, settings):
    respx_mock.get("https://www.youtube.com/oembed").mock(
        return_value=httpx.Response(200, json={"title": "Screening", "author_name": "SUCI"})
    )
    section = YouTubeSection(
        channel_name="SUCI", episodes=[YouTubeEpisode(url=VIDEO, episode="212")]
    )
    api = FakeTranscriptApi()
    with httpx.Client() as client:
        results = list(
            ingest_youtube(
                section, tmp_path, client=client, api=api, project_root=settings.project_root
            )
        )
    documents = [r for r in results if isinstance(r, Document)]
    assert len(documents) == 1
    doc = documents[0]
    assert doc.locator == watch_url(VIDEO)
    assert doc.episode == "212"
    assert doc.channel == "SUCI"
    assert doc.segments and doc.content_hash

    cached = tmp_path / f"{VIDEO}.json"
    assert cached.exists()
    assert json.loads(cached.read_text())[0]["text"]


@respx.mock(assert_all_called=False)
def test_cached_transcript_is_reused_without_calling_youtube(respx_mock, tmp_path, settings):
    respx_mock.get("https://www.youtube.com/oembed").mock(return_value=httpx.Response(404))
    (tmp_path / f"{VIDEO}.json").write_text(
        json.dumps([{"text": "cached line", "start": 0.0, "duration": 3.0}]), encoding="utf-8"
    )
    section = YouTubeSection(episodes=[YouTubeEpisode(url=VIDEO)])
    api = FakeTranscriptApi(error=AssertionError("should not be called"))
    with httpx.Client() as client:
        documents = [
            r
            for r in ingest_youtube(
                section, tmp_path, client=client, api=api, project_root=settings.project_root
            )
            if isinstance(r, Document)
        ]
    assert "cached line" in documents[0].text


@respx.mock(assert_all_called=False)
def test_a_broken_video_does_not_stop_the_others(respx_mock, tmp_path, settings):
    respx_mock.get("https://www.youtube.com/oembed").mock(return_value=httpx.Response(404))
    section = YouTubeSection(episodes=[YouTubeEpisode(url="not-a-url"), YouTubeEpisode(url=VIDEO)])
    with httpx.Client() as client:
        results = list(
            ingest_youtube(
                section,
                tmp_path,
                client=client,
                api=FakeTranscriptApi(),
                project_root=settings.project_root,
            )
        )
    assert len([r for r in results if isinstance(r, IngestFailure)]) == 1
    assert len([r for r in results if isinstance(r, Document)]) == 1


@respx.mock(assert_all_called=False)
def test_transcript_file_overrides_the_caption_api(respx_mock, tmp_path, settings):
    respx_mock.get("https://www.youtube.com/oembed").mock(return_value=httpx.Response(404))
    transcript = tmp_path / "ep.txt"
    transcript.write_text("Written by hand.", encoding="utf-8")
    section = YouTubeSection(episodes=[YouTubeEpisode(url=VIDEO, transcript_file=str(transcript))])
    api = FakeTranscriptApi(error=AssertionError("should not be called"))
    with httpx.Client() as client:
        documents = [
            r
            for r in ingest_youtube(
                section, tmp_path, client=client, api=api, project_root=tmp_path
            )
            if isinstance(r, Document)
        ]
    assert documents[0].text == "Written by hand."
