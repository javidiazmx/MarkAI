"""YouTube ingestion: id parsing, caption fetching, caching, and useful failures."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import youtube_transcript_api as yta

from markai.ingest.youtube import (
    RateLimitedError,
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


# --- channels ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@straightupchicagoinvestor",
        "https://www.youtube.com/@MarkAinleyGCRealty",
        "https://www.youtube.com/@somechannel/videos",
        "https://www.youtube.com/channel/UCabcdefghijklmnopqrstu",
        "https://www.youtube.com/c/SomeName",
    ],
)
def test_channel_urls_are_recognised(url):
    from markai.ingest.youtube import is_channel_url

    assert is_channel_url(url)


@pytest.mark.parametrize(
    "url",
    [f"https://www.youtube.com/watch?v={VIDEO}", f"https://youtu.be/{VIDEO}", VIDEO],
)
def test_video_urls_are_not_channels(url):
    from markai.ingest.youtube import is_channel_url

    assert not is_channel_url(url)


def test_a_channel_in_episodes_points_at_the_right_field():
    with pytest.raises(IngestError) as excinfo:
        extract_video_id("https://www.youtube.com/@straightupchicagoinvestor")
    assert "channel, not a video" in str(excinfo.value)
    assert "youtube.channels" in excinfo.value.hint


def test_expand_channel_turns_a_listing_into_episodes(tmp_path):
    from markai.ingest.youtube import expand_channel

    def lister(url, limit):
        assert limit is None
        return [{"id": f"vid{i:08d}", "title": f"Episode {i}"} for i in range(4)]

    episodes = expand_channel("https://www.youtube.com/@x", tmp_path, lister=lister)
    assert len(episodes) == 4
    assert episodes[0].url == watch_url("vid00000000")
    assert episodes[0].title == "Episode 0"


def test_expand_channel_caches_the_listing(tmp_path):
    from markai.ingest.youtube import expand_channel

    calls = []

    def lister(url, limit):
        calls.append(url)
        return [{"id": "vid00000000", "title": "Only"}]

    expand_channel("https://www.youtube.com/@x", tmp_path, lister=lister)
    expand_channel("https://www.youtube.com/@x", tmp_path, lister=lister)
    assert len(calls) == 1, "the second call must come from the cache"

    expand_channel("https://www.youtube.com/@x", tmp_path, refresh=True, lister=lister)
    assert len(calls) == 2, "refresh must re-read the channel"


def test_expand_channel_skips_junk_entries_and_duplicates(tmp_path):
    from markai.ingest.youtube import expand_channel

    def lister(url, limit):
        return [
            {"id": "vid00000000", "title": "Good"},
            {"id": "vid00000000", "title": "Duplicate"},
            {"id": "too-short", "title": "Bad id"},
            {"title": "No id at all"},
            None,
        ]

    episodes = expand_channel("https://www.youtube.com/@x", tmp_path, lister=lister)
    assert [e.title for e in episodes] == ["Good"]


def test_a_dead_channel_is_reported_not_crashed(tmp_path):
    from markai.ingest.youtube import expand_channel

    def lister(url, limit):
        raise IngestError("channel unavailable", hint="check the URL")

    with pytest.raises(IngestError):
        expand_channel("https://www.youtube.com/@gone", tmp_path, lister=lister)


def test_two_channels_are_merged_and_hand_written_titles_win(tmp_path, settings):
    from markai.ingest.youtube import _merge_episodes

    section = YouTubeSection(
        channels=["https://www.youtube.com/@a", "https://www.youtube.com/@b"],
        episodes=[YouTubeEpisode(url="vid00000000", title="Titulo a mano", episode="212")],
    )
    listings = {
        "https://www.youtube.com/@a": [
            {"id": "vid00000000", "title": "Del canal"},
            {"id": "vid00000001", "title": "Segundo"},
        ],
        "https://www.youtube.com/@b": [{"id": "vid00000002", "title": "Del otro canal"}],
    }
    episodes = _merge_episodes(section, tmp_path, tmp_path, lister=lambda url, limit: listings[url])
    assert len(episodes) == 3
    first = episodes[0]
    assert first.title == "Titulo a mano"
    assert first.episode == "212"


@respx.mock(assert_all_called=False)
def test_ingest_reads_videos_from_a_channel(respx_mock, tmp_path, settings):
    respx_mock.get("https://www.youtube.com/oembed").mock(
        return_value=httpx.Response(200, json={"title": "Screening", "author_name": "SUCI"})
    )
    section = YouTubeSection(
        channels=["https://www.youtube.com/@straightupchicagoinvestor"], channel_name="SUCI"
    )
    with httpx.Client() as client:
        documents = [
            r
            for r in ingest_youtube(
                section,
                tmp_path,
                client=client,
                api=FakeTranscriptApi(),
                project_root=settings.project_root,
                lister=lambda url, limit: [{"id": VIDEO, "title": "Screening"}],
            )
            if isinstance(r, Document)
        ]
    assert len(documents) == 1
    assert documents[0].locator == watch_url(VIDEO)
    assert documents[0].channel == "SUCI"


def test_the_run_stops_once_youtube_starts_blocking(tmp_path, settings):
    """2,000 identical block failures help nobody: stop and say to retry later."""
    from markai.ingest.youtube import BACKOFF_SECONDS, MAX_CONSECUTIVE_BLOCKS

    attempts_per_round = len(BACKOFF_SECONDS) + 1

    section = YouTubeSection(episodes=[YouTubeEpisode(url=f"vid{i:08d}") for i in range(40)])
    api = FakeTranscriptApi(error=yta.IpBlocked("vid00000000"))
    with httpx.Client() as client:
        results = list(
            ingest_youtube(
                section, tmp_path, client=client, api=api, project_root=settings.project_root
            )
        )

    assert len(results) == MAX_CONSECUTIVE_BLOCKS
    assert len(api.calls) == MAX_CONSECUTIVE_BLOCKS * attempts_per_round, (
        "each round waits out the backoff ladder, then the run stops"
    )
    last = results[-1]
    assert isinstance(last, IngestFailure)
    assert "rounds of waiting" in last.reason
    assert "cached" in last.hint


def test_a_video_without_captions_does_not_trip_the_breaker(tmp_path, settings):
    """Missing captions are normal and must not look like rate limiting."""
    section = YouTubeSection(episodes=[YouTubeEpisode(url=f"vid{i:08d}") for i in range(12)])
    api = FakeTranscriptApi(error=yta.TranscriptsDisabled("vid00000000"))
    with httpx.Client() as client:
        results = list(
            ingest_youtube(
                section, tmp_path, client=client, api=api, project_root=settings.project_root
            )
        )
    assert len(results) == 12, "every video should be attempted"
    assert all(isinstance(r, IngestFailure) for r in results)


# --- shorts -------------------------------------------------------------------------------

SHORTS_AND_EPISODES = [
    {"id": "shortAAAAAA", "title": "Short 1", "duration": 45},
    {"id": "longAAAAAAA", "title": "Episode 400", "duration": 3600},
    {"id": "shortBBBBBB", "title": "Short 2", "url": "https://www.youtube.com/shorts/shortBBBBBB"},
    {"id": "longBBBBBBB", "title": "Episode 399", "duration": 2700},
    {"id": "shortCCCCCC", "title": "Short 3", "duration": 30},
    {"id": "longCCCCCCC", "title": "Episode 398", "duration": 1800},
]


def test_shorts_are_dropped_by_duration_and_by_url(tmp_path):
    from markai.ingest.youtube import expand_channel

    episodes = expand_channel(
        "https://www.youtube.com/@x", tmp_path, lister=lambda u, limit: SHORTS_AND_EPISODES
    )
    assert [e.title for e in episodes] == ["Episode 400", "Episode 399", "Episode 398"]


def test_a_video_with_no_duration_is_kept(tmp_path):
    from markai.ingest.youtube import expand_channel

    episodes = expand_channel(
        "https://www.youtube.com/@x",
        tmp_path,
        lister=lambda u, limit: [{"id": "unknownDDDD", "title": "No duration given"}],
    )
    assert [e.title for e in episodes] == ["No duration given"]


def test_the_limit_applies_after_shorts_are_removed(tmp_path):
    """Limiting first would return 1 real video out of a listing that is half shorts."""
    from markai.ingest.youtube import expand_channel

    episodes = expand_channel(
        "https://www.youtube.com/@x",
        tmp_path,
        limit=2,
        lister=lambda u, limit: SHORTS_AND_EPISODES,
    )
    assert [e.title for e in episodes] == ["Episode 400", "Episode 399"]


def test_shorts_can_be_kept_on_request(tmp_path):
    from markai.ingest.youtube import expand_channel

    episodes = expand_channel(
        "https://www.youtube.com/@x",
        tmp_path,
        skip_shorts=False,
        lister=lambda u, limit: SHORTS_AND_EPISODES,
    )
    assert len(episodes) == 6


def test_changing_the_filter_does_not_re_read_the_channel(tmp_path):
    """The raw listing is cached, so settings can change without another YouTube call."""
    from markai.ingest.youtube import expand_channel

    calls = []

    def lister(url, limit):
        calls.append(url)
        return SHORTS_AND_EPISODES

    assert len(expand_channel("https://www.youtube.com/@x", tmp_path, lister=lister)) == 3
    kept = expand_channel("https://www.youtube.com/@x", tmp_path, skip_shorts=False)
    assert len(kept) == 6
    assert len(calls) == 1


def test_the_full_listing_is_fetched_so_filtering_is_honest(tmp_path):
    """yt-dlp must not be asked to truncate: we cannot filter what we never received."""
    from markai.ingest.youtube import expand_channel

    seen_limits = []

    def lister(url, limit):
        seen_limits.append(limit)
        return SHORTS_AND_EPISODES

    expand_channel("https://www.youtube.com/@x", tmp_path, limit=2, lister=lister)
    assert seen_limits == [None]


# --- getting past an IP block ----------------------------------------------------------


def test_no_proxy_configured_builds_a_plain_client(settings):
    from markai.ingest.youtube import build_transcript_api

    assert settings.youtube_unblock_method() == "none"
    assert build_transcript_api(settings) is not None


def test_a_proxy_url_reaches_the_caption_client(settings):
    """Politeness does nothing once the address is blocked; a different address does."""
    from markai.ingest.youtube import build_transcript_api

    configured = settings.model_copy(update={"youtube_proxy_url": "http://user:pw@proxy.test:8080"})
    assert configured.youtube_unblock_method() == "proxy"
    assert build_transcript_api(configured) is not None


def test_webshare_credentials_win_over_a_plain_proxy(settings):
    configured = settings.model_copy(
        update={
            "youtube_proxy_url": "http://proxy.test:8080",
            "webshare_username": "u",
            "webshare_password": "pw",
        }
    )
    assert configured.youtube_unblock_method() == "webshare proxy"


def test_cookies_are_loaded_from_a_netscape_file(settings, tmp_path):
    from markai.ingest.youtube import build_transcript_api

    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tPREF\tf1=50000000\n",
        encoding="utf-8",
    )
    configured = settings.model_copy(update={"youtube_cookies_file": cookies})
    assert configured.youtube_unblock_method() == "cookies file"
    assert build_transcript_api(configured) is not None


def test_a_missing_cookie_file_says_so_once(settings, tmp_path):
    """One clear failure beats the same message repeated 1,142 times."""
    from markai.ingest.youtube import build_transcript_api

    configured = settings.model_copy(update={"youtube_cookies_file": tmp_path / "nope.txt"})
    with pytest.raises(IngestError) as excinfo:
        build_transcript_api(configured)
    assert "nope.txt" in str(excinfo.value)


def test_the_proxy_secret_never_reaches_the_status_line(settings):
    """doctor prints the method. The URL can carry a username and password."""
    configured = settings.model_copy(update={"youtube_proxy_url": "http://user:s3cret@p.test"})
    assert "s3cret" not in configured.youtube_unblock_method()
    assert "user" not in configured.youtube_unblock_method()


# --- the yt-dlp caption route ----------------------------------------------------------
#
# yt-dlp is already a dependency (it lists the channels) and talks to YouTube over a
# different client, so it often works at a moment when the caption library is blocked.

VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
Screening starts with written criteria.

00:00:04.000 --> 00:00:08.000
Apply them to every applicant, every time.
"""


def _info(langs=("en",), ext="vtt"):
    return {
        "automatic_captions": {
            lang: [{"ext": ext, "url": f"https://caption.test/{lang}.{ext}"}] for lang in langs
        }
    }


@respx.mock(assert_all_called=False)
def test_ytdlp_route_returns_timed_segments(respx_mock):
    from markai.ingest.youtube import captions_via_ytdlp

    respx_mock.get("https://caption.test/en.vtt").mock(return_value=httpx.Response(200, text=VTT))
    with httpx.Client() as client:
        segments = captions_via_ytdlp(VIDEO, ["en"], client, extractor=lambda u, o: _info())

    assert len(segments) == 2
    assert segments[0].start == 1.0 and segments[0].end == 4.0
    assert "written criteria" in segments[0].text


def test_naming_a_browser_asks_ytdlp_for_its_cookies():
    """The point of this route: no export, no extension, just the browser's name."""
    from markai.ingest.youtube import captions_via_ytdlp

    seen: dict[str, object] = {}

    def extractor(url, options):
        seen.update(options)
        return _info()

    with httpx.Client() as client, respx.mock:
        respx.get("https://caption.test/en.vtt").mock(return_value=httpx.Response(200, text=VTT))
        captions_via_ytdlp(VIDEO, ["en"], client, "Firefox", extractor=extractor)

    assert seen["cookiesfrombrowser"] == ("firefox",), "normalised, and passed as a tuple"


def test_a_regional_caption_track_still_counts_as_english():
    from markai.ingest.youtube import _pick_caption_track

    tracks = {"en-GB": [{"ext": "vtt", "url": "https://c.test/en-GB.vtt"}]}
    assert _pick_caption_track(tracks, ["en"]) == "https://c.test/en-GB.vtt"


def test_a_video_with_only_other_languages_says_which_it_has():
    from markai.ingest.youtube import captions_via_ytdlp

    with httpx.Client() as client, pytest.raises(IngestError) as excinfo:
        captions_via_ytdlp(VIDEO, ["en"], client, extractor=lambda u, o: _info(langs=("de", "fr")))
    assert "de" in (excinfo.value.hint or "")


def test_the_fallback_runs_when_the_first_route_is_blocked(tmp_path, settings, monkeypatch):
    """The whole point: a block on one route is not the end of the run."""
    import markai.ingest.youtube as yt

    monkeypatch.setattr(yt, "_ytdlp_extract", lambda url, options: _info())
    section = YouTubeSection(episodes=[YouTubeEpisode(url=VIDEO)])
    api = FakeTranscriptApi(error=yta.IpBlocked(VIDEO))

    with respx.mock:
        respx.get("https://caption.test/en.vtt").mock(return_value=httpx.Response(200, text=VTT))
        respx.get("https://www.youtube.com/oembed").mock(
            return_value=httpx.Response(200, json={"title": "Screening", "author_name": "SUCI"})
        )
        with httpx.Client() as client:
            results = list(
                yt.ingest_youtube(
                    section, tmp_path, client=client, api=api, project_root=settings.project_root
                )
            )

    documents = [r for r in results if isinstance(r, Document)]
    assert len(documents) == 1, "the second route rescued the video"
    assert "written criteria" in documents[0].text
    assert (tmp_path / f"{VIDEO}.json").exists(), "and it is cached, so a re-run is free"


def test_no_captions_on_either_route_skips_the_backoff(tmp_path, settings, monkeypatch):
    """No captions is an answer, not a reason to keep waiting."""
    import markai.ingest.youtube as yt

    waited: list[float] = []
    monkeypatch.setattr(yt, "_sleep", lambda seconds: waited.append(seconds))
    monkeypatch.setattr(yt, "_ytdlp_extract", lambda url, options: {"automatic_captions": {}})

    section = YouTubeSection(episodes=[YouTubeEpisode(url=VIDEO)])
    api = FakeTranscriptApi(error=yta.IpBlocked(VIDEO))
    with httpx.Client() as client:
        results = list(
            yt.ingest_youtube(
                section, tmp_path, client=client, api=api, project_root=settings.project_root
            )
        )

    assert waited == [], "no waiting for something that will never arrive"
    assert len(results) == 1 and isinstance(results[0], IngestFailure)
    assert "No captions" in results[0].reason


# --- finding a way in without being told -------------------------------------------------


def test_the_fallback_tries_each_browser_and_remembers_the_one_that_works():
    """The owner should not have to know which browser they use, let alone export from it."""
    from markai.ingest.youtube import CaptionFallback

    tried: list[str | None] = []

    def extractor(url, options):
        browser = options.get("cookiesfrombrowser")
        tried.append(browser[0] if browser else None)
        if browser and browser[0] == "edge":
            return _info()
        raise RateLimitedError("blocked")

    with httpx.Client() as client, respx.mock:
        respx.get("https://caption.test/en.vtt").mock(return_value=httpx.Response(200, text=VTT))
        with pytest.MonkeyPatch.context() as mp:
            import markai.ingest.youtube as yt

            mp.setattr(yt, "_ytdlp_extract", extractor)
            fetch = CaptionFallback(client, ["en"])
            assert len(fetch("vid1")) == 2
            assert tried == [None, "firefox", "edge"], "anonymous first, then browsers in order"

            tried.clear()
            assert len(fetch("vid2")) == 2
            assert tried == ["edge"], "the working route is remembered, not rediscovered"


def test_the_sweep_runs_once_even_when_nothing_works():
    """Seven dead browsers per video, 1,142 times, would be its own kind of failure."""
    from markai.ingest.youtube import BROWSERS_TO_TRY, CaptionFallback

    calls: list[object] = []

    def extractor(url, options):
        calls.append(options.get("cookiesfrombrowser"))
        raise RateLimitedError("blocked")

    with httpx.Client() as client, pytest.MonkeyPatch.context() as mp:
        import markai.ingest.youtube as yt

        mp.setattr(yt, "_ytdlp_extract", extractor)
        fetch = CaptionFallback(client, ["en"])
        with pytest.raises(RateLimitedError):
            fetch("vid1")
        assert len(calls) == len(BROWSERS_TO_TRY) + 1

        calls.clear()
        with pytest.raises(RateLimitedError):
            fetch("vid2")
        assert len(calls) == 1, "one more try, not another sweep"
        assert fetch.attempts, "and it can say what it tried"


def test_a_configured_browser_skips_the_sweep():
    from markai.ingest.youtube import CaptionFallback

    tried: list[object] = []

    def extractor(url, options):
        tried.append(options.get("cookiesfrombrowser"))
        return _info()

    with httpx.Client() as client, respx.mock, pytest.MonkeyPatch.context() as mp:
        import markai.ingest.youtube as yt

        mp.setattr(yt, "_ytdlp_extract", extractor)
        respx.get("https://caption.test/en.vtt").mock(return_value=httpx.Response(200, text=VTT))
        CaptionFallback(client, ["en"], "firefox")("vid1")

    assert tried == [("firefox",)], "asked for, so not second-guessed"
