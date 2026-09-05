"""The ingest pipeline: planning, change detection, pruning, and failure isolation."""

from __future__ import annotations

import httpx
import respx

from markai.ingest.pipeline import IngestReport, plan_ingest, run_ingest
from markai.knowledge.chunking import chunk_document
from markai.knowledge.store import KnowledgeStore
from markai.models import SourceKind
from markai.sources.manifest import (
    PodcastEpisode,
    PodcastSection,
    SourceManifest,
    WebsiteSource,
    YouTubeEpisode,
    YouTubeSection,
)
from tests.fakes import FakeEmbedder, FakeTranscriptApi

VIDEO = "dQw4w9WgXcQ"
PAGE = """<html><head><title>Deposits</title></head><body><h1>Deposits</h1>
<p>Chicago landlords owe interest on security deposits every year.</p>
<p>Hold the money in a separate federally insured account in Illinois.</p></body></html>"""


def _manifest() -> SourceManifest:
    return SourceManifest(
        websites=[WebsiteSource(url="https://example.com/deposits")],
        youtube=YouTubeSection(
            channel_name="SUCI", episodes=[YouTubeEpisode(url=VIDEO, episode="212")]
        ),
    )


def _mock_web(respx_mock) -> None:
    respx_mock.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://example.com/deposits").mock(
        return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    )
    respx_mock.get("https://www.youtube.com/oembed").mock(
        return_value=httpx.Response(200, json={"title": "Screening", "author_name": "SUCI"})
    )


def test_plan_counts_work_without_downloading(settings):
    settings.ensure_dirs()
    (settings.podcast_transcripts_dir / "145.txt").write_text("x", encoding="utf-8")
    manifest = _manifest()
    manifest.podcast = PodcastSection(
        episodes=[
            PodcastEpisode(title="Deposits", episode="145"),
            PodcastEpisode(title="Audio", audio_url="https://a.test/1.mp3", duration_seconds=1800),
        ]
    )
    plan = plan_ingest(manifest, settings)
    assert plan.web_pages == 1
    assert plan.youtube_videos == 1
    assert plan.podcast_by_method["audio_transcribe"] == 1
    assert plan.needs_transcription() is True
    assert plan.transcription_minutes == 30.0
    assert plan.summary_table() is not None


def test_plan_without_audio_needs_no_transcription(settings):
    plan = plan_ingest(_manifest(), settings)
    assert plan.needs_transcription() is False


def test_plan_respects_only(settings):
    plan = plan_ingest(_manifest(), settings, only={SourceKind.WEBSITE})
    assert plan.web_pages == 1
    assert plan.youtube_videos == 0


@respx.mock(assert_all_called=False)
def test_first_run_adds_and_second_run_skips(respx_mock, settings):
    _mock_web(respx_mock)
    settings = settings.model_copy(update={"crawl_delay_seconds": 0.0})
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    manifest = _manifest()

    with httpx.Client() as client:
        first = run_ingest(manifest, store, None, settings, client=client, api=FakeTranscriptApi())
        assert len(first.added) == 2
        assert first.skipped == []
        assert first.failures == []

        second = run_ingest(manifest, store, None, settings, client=client, api=FakeTranscriptApi())
        assert len(second.skipped) == 2
        assert second.added == []

        forced = run_ingest(
            manifest, store, None, settings, force=True, client=client, api=FakeTranscriptApi()
        )
        assert len(forced.updated) == 2
    store.close()


@respx.mock(assert_all_called=False)
def test_embeddings_are_generated_when_an_embedder_is_supplied(respx_mock, settings):
    _mock_web(respx_mock)
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    with httpx.Client() as client:
        run_ingest(
            _manifest(), store, FakeEmbedder(), settings, client=client, api=FakeTranscriptApi()
        )
    assert store.stats().embedded_chunks > 0
    assert store.stats().embedding_model == "fake-embeddings"
    store.close()


@respx.mock(assert_all_called=False)
def test_one_bad_source_does_not_stop_the_rest(respx_mock, settings):
    _mock_web(respx_mock)
    respx_mock.get("https://broken.test/page").mock(return_value=httpx.Response(500))
    respx_mock.get("https://broken.test/robots.txt").mock(return_value=httpx.Response(404))
    settings = settings.model_copy(update={"crawl_delay_seconds": 0.0})
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)

    manifest = _manifest()
    manifest.websites.append(WebsiteSource(url="https://broken.test/page"))
    with httpx.Client() as client:
        report = run_ingest(manifest, store, None, settings, client=client, api=FakeTranscriptApi())

    assert len(report.added) == 2
    assert len(report.failures) == 1
    assert "broken.test" in report.failures[0].locator
    store.close()


@respx.mock(assert_all_called=False)
def test_prune_removes_sources_that_left_the_manifest(respx_mock, settings, toy_documents):
    _mock_web(respx_mock)
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    stale = toy_documents[2]
    store.upsert_document(stale, chunk_document(stale))

    with httpx.Client() as client:
        kept = run_ingest(
            _manifest(), store, None, settings, client=client, api=FakeTranscriptApi()
        )
        assert kept.pruned == []
        assert store.get_document(stale.id) is not None

        pruned = run_ingest(
            _manifest(), store, None, settings, prune=True, client=client, api=FakeTranscriptApi()
        )
    assert stale.locator in pruned.pruned
    assert store.get_document(stale.id) is None
    store.close()


@respx.mock(assert_all_called=False)
def test_the_run_is_recorded_with_counts_not_content(respx_mock, settings):
    _mock_web(respx_mock)
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    with httpx.Client() as client:
        report = run_ingest(
            _manifest(), store, None, settings, client=client, api=FakeTranscriptApi()
        )

    summary = report.summary_dict()
    assert summary["added"] == 2
    assert "interest on security deposits" not in str(summary)
    assert store.stats().last_ingest_at is not None
    assert report.summary_table() is not None
    store.close()


@respx.mock(assert_all_called=False)
def test_only_limits_which_kinds_run(respx_mock, settings):
    _mock_web(respx_mock)
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    with httpx.Client() as client:
        report = run_ingest(
            _manifest(), store, None, settings, only={SourceKind.WEBSITE}, client=client
        )
    assert len(report.added) == 1
    assert store.stats().documents_by_kind["youtube"] == 0
    store.close()


@respx.mock(assert_all_called=False)
def test_plan_reports_an_unreachable_feed_instead_of_zero(respx_mock, settings):
    respx_mock.get("https://feeds.example.com/suci").mock(return_value=httpx.Response(403))
    manifest = _manifest()
    manifest.podcast = PodcastSection(rss="https://feeds.example.com/suci")
    with httpx.Client() as client:
        plan = plan_ingest(manifest, settings, client=client)
    assert plan.podcast_error is not None
    assert "403" in plan.podcast_error
    assert plan.podcast_by_method == {}


def test_plan_reports_a_missing_urls_file(settings):
    manifest = _manifest()
    manifest.youtube.urls_file = "sources/no-existe.txt"
    plan = plan_ingest(manifest, settings)
    assert plan.youtube_error is not None


def test_the_plan_says_how_many_videos_the_cap_holds_back(settings, tmp_path):
    """A capped count alone hides how much of the channel is being left out."""
    from markai.ingest.youtube import expand_channel

    settings.ensure_dirs()
    listing = [{"id": f"vid{i:08d}", "title": f"Ep {i}", "duration": 1800} for i in range(30)]
    expand_channel(
        "https://www.youtube.com/@x", settings.youtube_cache_dir, lister=lambda u, limit: listing
    )

    manifest = SourceManifest(
        youtube=YouTubeSection(channels=["https://www.youtube.com/@x"], max_videos_per_channel=10)
    )
    plan = plan_ingest(manifest, settings, only={SourceKind.YOUTUBE})
    assert plan.youtube_videos == 10
    assert plan.youtube_available == 30
    assert "30 available" in str(plan.summary_table().columns[2]._cells)

    manifest.youtube.max_videos_per_channel = None
    uncapped = plan_ingest(manifest, settings, only={SourceKind.YOUTUBE})
    assert uncapped.youtube_videos == 30


@respx.mock(assert_all_called=False)
def test_a_later_run_with_a_key_embeds_what_is_already_stored(respx_mock, settings):
    """The whole point: no re-downloading just to turn on semantic search."""
    _mock_web(respx_mock)
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)

    with httpx.Client() as client:
        first = run_ingest(
            _manifest(), store, None, settings, client=client, api=FakeTranscriptApi()
        )
        assert first.embedded == 0
        assert store.stats().embedded_chunks == 0

        # Same manifest, nothing changed upstream, but now there is an embedder.
        second = run_ingest(
            _manifest(), store, FakeEmbedder(), settings, client=client, api=FakeTranscriptApi()
        )

    assert len(second.skipped) == 2, "sources must not be re-fetched"
    assert second.added == []
    assert second.embedded > 0
    assert store.stats().embedded_chunks == second.embedded
    store.close()


def test_failures_are_grouped_so_the_table_stays_readable():
    from markai.models import IngestFailure

    report = IngestReport(started_at="t0", finished_at="t1")
    report.failures = [
        IngestFailure(SourceKind.WEBSITE, f"https://x.test/{i}", "Page is too large: url")
        for i in range(1607)
    ]
    report.failures.append(
        IngestFailure(SourceKind.WEBSITE, "https://y.test", "HTTP 404 for https://y.test")
    )
    grouped = report.failure_summary()
    assert grouped[0] == ("Page is too large", 1607)
    assert len(grouped) == 2


def test_the_details_file_holds_every_failure(tmp_path):
    from markai.models import IngestFailure

    report = IngestReport(started_at="t0", finished_at="t1", added=["A (https://a)"])
    report.failures = [
        IngestFailure(SourceKind.WEBSITE, f"https://x.test/{i}", "too large", "raise the limit")
        for i in range(200)
    ]
    path = report.write_details(tmp_path / "last-ingest.txt")
    body = path.read_text(encoding="utf-8")
    assert "https://x.test/199" in body, "every failure, not a preview"
    assert "raise the limit" in body
    assert "A (https://a)" in body
