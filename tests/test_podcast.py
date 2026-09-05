"""Podcast ingestion: feed parsing, transcript matching, and the preference order."""

from __future__ import annotations

import builtins

import httpx
import pytest
import respx

from markai.ingest.podcast import (
    NO_TRANSCRIPT_HINT,
    _episode_page,
    _show_notes,
    ingest_podcast,
    load_feed_episodes,
    match_transcript_file,
    resolve_transcript_plan,
    transcribe_audio,
)
from markai.models import Document, IngestError, IngestFailure
from markai.sources.manifest import PodcastEpisode, PodcastSection

RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>Straight Up Chicago Investor</title>
    <item>
      <title>Deposits, interest, and the RLTO</title>
      <link>https://example.com/episodes/145</link>
      <itunes:episode>145</itunes:episode>
      <itunes:duration>00:55:00</itunes:duration>
      <pubDate>Thu, 04 Mar 2021 10:00:00 +0000</pubDate>
      <enclosure url="https://audio.example.com/145.mp3" type="audio/mpeg" length="1000"/>
      <podcast:transcript url="https://example.com/145.srt" type="application/srt"/>
    </item>
    <item>
      <title>Winter heat rules</title>
      <link>https://example.com/episodes/198</link>
      <itunes:episode>198</itunes:episode>
      <itunes:duration>3300</itunes:duration>
      <pubDate>Wed, 02 Nov 2022 10:00:00 +0000</pubDate>
      <enclosure url="https://audio.example.com/198.mp3" type="audio/mpeg" length="1000"/>
    </item>
  </channel>
</rss>
"""


@respx.mock(assert_all_called=False)
def test_feed_parsing_maps_every_field(respx_mock):
    respx_mock.get("https://feeds.example.com/suci").mock(
        return_value=httpx.Response(200, text=RSS, headers={"content-type": "application/rss+xml"})
    )
    with httpx.Client() as client:
        episodes = load_feed_episodes("https://feeds.example.com/suci", [], None, client)

    assert len(episodes) == 2
    first = episodes[0]
    assert first.episode == "145"
    assert first.episode_url == "https://example.com/episodes/145"
    assert first.audio_url == "https://audio.example.com/145.mp3"
    assert first.transcript_url == "https://example.com/145.srt"
    assert first.published_at == "2021-03-04"
    assert first.duration_seconds == 3300.0
    assert episodes[1].duration_seconds == 3300.0


@respx.mock(assert_all_called=False)
def test_include_titles_filters_by_text_or_episode_number(respx_mock):
    respx_mock.get("https://feeds.example.com/suci").mock(
        return_value=httpx.Response(200, text=RSS)
    )
    with httpx.Client() as client:
        by_text = load_feed_episodes("https://feeds.example.com/suci", ["winter"], None, client)
        by_number = load_feed_episodes("https://feeds.example.com/suci", ["145"], None, client)
    assert [e.episode for e in by_text] == ["198"]
    assert [e.episode for e in by_number] == ["145"]


@respx.mock(assert_all_called=False)
def test_max_episodes_limits_the_feed(respx_mock):
    respx_mock.get("https://feeds.example.com/suci").mock(
        return_value=httpx.Response(200, text=RSS)
    )
    with httpx.Client() as client:
        episodes = load_feed_episodes("https://feeds.example.com/suci", [], 1, client)
    assert len(episodes) == 1


@respx.mock(assert_all_called=False)
def test_an_unreachable_feed_is_a_helpful_error(respx_mock):
    respx_mock.get("https://feeds.example.com/suci").mock(return_value=httpx.Response(500))
    with httpx.Client() as client, pytest.raises(IngestError) as excinfo:
        load_feed_episodes("https://feeds.example.com/suci", [], None, client)
    assert excinfo.value.hint


def test_transcript_matched_by_episode_number(tmp_path):
    (tmp_path / "SUCI Ep 212 - Mixed Use.srt").write_text("x", encoding="utf-8")
    (tmp_path / "2120.srt").write_text("x", encoding="utf-8")
    episode = PodcastEpisode(title="Mixed use", episode="212")
    match = match_transcript_file(episode, tmp_path, tmp_path)
    assert match is not None and "212 -" in match.name


def test_transcript_matched_by_title_words(tmp_path):
    (tmp_path / "winter-heat-rules-for-landlords.txt").write_text("x", encoding="utf-8")
    episode = PodcastEpisode(title="Winter heat rules")
    assert match_transcript_file(episode, tmp_path, tmp_path) is not None


def test_unrelated_filename_does_not_match(tmp_path):
    (tmp_path / "completely-different-topic.txt").write_text("x", encoding="utf-8")
    episode = PodcastEpisode(title="Winter heat rules")
    assert match_transcript_file(episode, tmp_path, tmp_path) is None


def test_explicit_transcript_file_wins(tmp_path):
    explicit = tmp_path / "chosen.srt"
    explicit.write_text("x", encoding="utf-8")
    (tmp_path / "212.srt").write_text("x", encoding="utf-8")
    episode = PodcastEpisode(episode="212", transcript_file=str(explicit))
    assert match_transcript_file(episode, tmp_path, tmp_path) == explicit


def test_transcribe_audio_without_the_extra_explains_the_options(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("no module named faster_whisper")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(IngestError) as excinfo:
        transcribe_audio(tmp_path / "a.mp3", "small")
    assert excinfo.value.hint == NO_TRANSCRIPT_HINT
    assert "markai[transcribe]" in excinfo.value.hint


def test_transcript_file_is_preferred_over_audio(settings):
    settings.ensure_dirs()
    (settings.podcast_transcripts_dir / "145.txt").write_text(
        "Deposit interest is owed every year.", encoding="utf-8"
    )
    section = PodcastSection(
        show_name="SUCI",
        episodes=[
            PodcastEpisode(
                title="Deposits",
                episode="145",
                audio_url="https://audio.example.com/145.mp3",
                episode_url="https://example.com/episodes/145",
            )
        ],
    )
    with httpx.Client() as client:
        results = list(
            ingest_podcast(section, settings, settings.raw_dir / "podcast", client=client)
        )
    documents = [r for r in results if isinstance(r, Document)]
    assert len(documents) == 1
    assert "Deposit interest" in documents[0].text
    assert documents[0].metadata["transcript_method"] == "transcript_file"
    assert documents[0].link == "https://example.com/episodes/145"


def test_no_transcript_and_no_audio_reports_the_options(settings):
    settings.ensure_dirs()
    section = PodcastSection(episodes=[PodcastEpisode(title="Orphan episode", episode="999")])
    with httpx.Client() as client:
        results = list(
            ingest_podcast(section, settings, settings.raw_dir / "podcast", client=client)
        )
    failures = [r for r in results if isinstance(r, IngestFailure)]
    assert len(failures) == 1
    assert failures[0].hint == NO_TRANSCRIPT_HINT


def test_transcription_can_be_declined(settings):
    settings.ensure_dirs()
    section = PodcastSection(
        episodes=[PodcastEpisode(title="Audio only", audio_url="https://audio.example.com/1.mp3")]
    )
    with httpx.Client() as client:
        results = list(
            ingest_podcast(
                section,
                settings,
                settings.raw_dir / "podcast",
                client=client,
                allow_transcription=False,
            )
        )
    assert all(isinstance(r, IngestFailure) for r in results)


def test_plan_reports_the_method_per_episode(settings):
    settings.ensure_dirs()
    (settings.podcast_transcripts_dir / "145.txt").write_text("x", encoding="utf-8")
    section = PodcastSection(
        episodes=[
            PodcastEpisode(title="Deposits", episode="145"),
            PodcastEpisode(title="Audio only", audio_url="https://audio.example.com/1.mp3"),
            PodcastEpisode(title="Nothing at all"),
        ]
    )
    methods = [method for _episode, method in resolve_transcript_plan(section, settings)]
    assert methods == ["transcript_file", "audio_transcribe", "unavailable"]


@respx.mock(assert_all_called=False)
def test_plan_raises_when_the_feed_cannot_be_read(respx_mock, settings):
    """An unreachable feed must not look like an empty podcast."""
    respx_mock.get("https://feeds.example.com/suci").mock(return_value=httpx.Response(403))
    section = PodcastSection(rss="https://feeds.example.com/suci")
    with httpx.Client() as client, pytest.raises(IngestError):
        resolve_transcript_plan(section, settings, client)


@respx.mock(assert_all_called=False)
def test_a_bad_feed_does_not_hide_episodes_listed_by_hand(respx_mock, settings):
    respx_mock.get("https://feeds.example.com/suci").mock(return_value=httpx.Response(500))
    settings.ensure_dirs()
    (settings.podcast_transcripts_dir / "145.txt").write_text("x", encoding="utf-8")
    section = PodcastSection(
        rss="https://feeds.example.com/suci",
        episodes=[PodcastEpisode(title="Deposits", episode="145")],
    )
    with httpx.Client() as client:
        plan = resolve_transcript_plan(section, settings, client)
    assert [method for _e, method in plan] == ["transcript_file"]


# --- show notes ----------------------------------------------------------------------------
#
# A 480-episode feed produced 480 failures and stored nothing, because an episode without a
# transcript was treated as an episode with no content. The notes were in the feed all along.


NOTES_HTML = (
    "<p>Mark Ainley and Tom Shallcross sit down with <b>Jared Kott</b> to talk about "
    "scaling to 500 units on the Southeast side of Chicago.</p>"
    "<p>Jared walks through his BRRRR criteria, the zip codes he stopped buying in, how he "
    "underwrites a gut rehab, and what he actually pays his property manager.</p>"
    "<ul><li>02:14 The first deal</li><li>18:40 Financing the portfolio</li></ul>"
    "<script>tracker()</script>"
)


def test_show_notes_are_read_as_plain_text():
    notes = _show_notes({"content": [{"value": NOTES_HTML}], "summary": "teaser"})
    assert "Jared Kott" in notes
    assert "<p>" not in notes and "tracker()" not in notes
    assert "\n\n" in notes, "paragraph breaks survive, because the chunker splits on them"


def test_show_notes_prefer_the_longest_description_available():
    entry = {"summary": "Short.", "content": [{"value": NOTES_HTML}]}
    assert len(_show_notes(entry).split()) > 40


def test_show_notes_are_none_when_the_feed_carries_none():
    assert _show_notes({}) is None


@respx.mock(assert_all_called=False)
def test_an_episode_with_no_transcript_is_stored_from_its_notes(respx_mock, settings, tmp_path):
    feed = f"""<?xml version="1.0"?><rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
    version="2.0"><channel><title>SUCI</title>
    <item><title>Episode 11: The Boss of the Southeast</title>
      <itunes:episode>11</itunes:episode>
      <link>https://suci.test/11</link>
      <enclosure url="https://audio.test/11.mp3" type="audio/mpeg" length="1"/>
      <description><![CDATA[{NOTES_HTML}]]></description>
    </item></channel></rss>"""
    respx_mock.get("https://feeds.test/rss").mock(
        return_value=httpx.Response(200, text=feed, headers={"content-type": "application/rss+xml"})
    )
    settings.ensure_dirs()
    section = PodcastSection(show_name="SUCI", rss="https://feeds.test/rss")

    with httpx.Client() as client:
        results = list(
            ingest_podcast(section, settings, tmp_path, client=client, allow_transcription=False)
        )

    documents = [r for r in results if isinstance(r, Document)]
    assert len(documents) == 1, "notes are content, not a failure"
    doc = documents[0]
    assert "Jared Kott" in doc.text
    assert doc.episode == "11"
    assert doc.metadata["transcript_method"] == "show_notes"
    assert doc.segments == [], "notes have no timestamps, so no segments are invented"
    assert doc.content_hash


@respx.mock(assert_all_called=False)
def test_a_one_line_teaser_is_still_a_failure(respx_mock, settings, tmp_path):
    """Storing "Ep 12." as a document would just pollute search results."""
    feed = """<?xml version="1.0"?><rss version="2.0"><channel><title>SUCI</title>
    <item><title>Episode 12</title><link>https://suci.test/12</link>
      <enclosure url="https://audio.test/12.mp3" type="audio/mpeg" length="1"/>
      <description>A quick chat.</description>
    </item></channel></rss>"""
    respx_mock.get("https://feeds.test/rss").mock(
        return_value=httpx.Response(200, text=feed, headers={"content-type": "application/rss+xml"})
    )
    settings.ensure_dirs()
    section = PodcastSection(show_name="SUCI", rss="https://feeds.test/rss")

    with httpx.Client() as client:
        results = list(
            ingest_podcast(section, settings, tmp_path, client=client, allow_transcription=False)
        )
    assert [r for r in results if isinstance(r, Document)] == []
    assert len(results) == 1 and isinstance(results[0], IngestFailure)


def test_the_episode_link_is_the_page_not_the_audio_file():
    """This show's feed puts the mp3 in <link>; a citation must not be a 40 MB download."""
    entry = {
        "links": [
            {"href": "https://traffic.test/SUCI476.mp3", "rel": "enclosure", "type": "audio/mpeg"}
        ],
        "id": "https://www.straightupchicagoinvestor.com/podcast/476",
        "link": "https://traffic.test/SUCI476.mp3",
    }
    assert _episode_page(entry) == "https://www.straightupchicagoinvestor.com/podcast/476"


def test_the_audio_link_is_kept_when_the_feed_offers_nothing_else():
    entry = {"link": "https://traffic.test/SUCI476.mp3"}
    assert _episode_page(entry) == "https://traffic.test/SUCI476.mp3"


def test_a_normal_feed_link_is_left_alone():
    entry = {
        "links": [{"href": "https://suci.test/476", "rel": "alternate", "type": "text/html"}],
        "link": "https://suci.test/476",
    }
    assert _episode_page(entry) == "https://suci.test/476"
