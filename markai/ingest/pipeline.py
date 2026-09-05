"""Runs the whole ingest: manifest in, knowledge base out.

Two entry points. ``plan_ingest`` shows what a run would do (and warns before hours of
transcription); ``run_ingest`` does it. A failure in one source never stops the rest.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from rich.markup import escape
from rich.table import Table

from markai.config import Settings
from markai.ingest.podcast import ingest_podcast, resolve_transcript_plan
from markai.ingest.websites import ingest_websites, make_client
from markai.ingest.youtube import expand_channel, ingest_youtube, read_urls_file
from markai.knowledge.chunking import chunk_document
from markai.models import Document, IngestFailure, SourceKind
from markai.sources.manifest import SourceManifest

logger = logging.getLogger(__name__)

UNKNOWN_EPISODE_MINUTES = 60.0


@dataclass
class IngestPlan:
    """What an ingest run would fetch, computed without downloading audio."""

    web_pages: int = 0
    youtube_videos: int = 0
    podcast_by_method: dict[str, int] = field(default_factory=dict)
    transcription_minutes: float = 0.0
    youtube_available: int = 0
    podcast_error: str | None = None
    youtube_error: str | None = None

    def needs_transcription(self) -> bool:
        return self.podcast_by_method.get("audio_transcribe", 0) > 0

    def summary_table(self) -> Table:
        table = Table(title="Ingest plan", show_header=True, header_style="bold")
        table.add_column("Source")
        table.add_column("Count", justify="right")
        table.add_column("Notes")
        table.add_row("Websites", str(self.web_pages), "pages or crawl seeds")
        held_back = max(self.youtube_available - self.youtube_videos, 0)
        youtube_note = "videos (captions)"
        if held_back:
            youtube_note = (
                f"videos (captions) - capped; {self.youtube_available} available, "
                f"{held_back} held back by max_videos_per_channel"
            )
        table.add_row("YouTube", str(self.youtube_videos), youtube_note)
        labels = {
            "transcript_file": "transcript already on disk",
            "transcript_url": "transcript linked in the feed",
            "audio_transcribe": "needs local transcription",
            "unavailable": "no transcript and no audio",
        }
        for method, label in labels.items():
            count = self.podcast_by_method.get(method, 0)
            if count:
                note = label
                if method == "audio_transcribe":
                    hours = self.transcription_minutes / 60.0
                    note = f"{label} (~{hours:.1f} h of audio, roughly the same in wall clock)"
                table.add_row("Podcast", str(count), note)
        if self.podcast_error:
            table.add_row(
                "Podcast", "?", f"[red]could not read the feed[/red]\n{self.podcast_error}"
            )
        elif not self.podcast_by_method:
            table.add_row("Podcast", "0", "nothing configured")
        if self.youtube_error:
            table.add_row(
                "YouTube", "?", f"[red]could not read the URL list[/red]\n{self.youtube_error}"
            )
        return table


@dataclass
class IngestReport:
    """What an ingest run actually did."""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    failures: list[IngestFailure] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def summary_dict(self) -> dict[str, Any]:
        """Counts only. Never put document text in here; it is written to the database."""
        return {
            "added": len(self.added),
            "updated": len(self.updated),
            "skipped": len(self.skipped),
            "pruned": len(self.pruned),
            "failures": [
                {"kind": f.kind.value, "locator": f.locator, "reason": f.reason}
                for f in self.failures
            ],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def summary_table(self) -> Table:
        table = Table(title="Ingest results", show_header=True, header_style="bold")
        table.add_column("Outcome")
        table.add_column("Count", justify="right")
        table.add_column("Sources")
        for label, items in (
            ("Added", self.added),
            ("Updated", self.updated),
            ("Unchanged", self.skipped),
            ("Removed", self.pruned),
        ):
            preview = escape("\n".join(items[:5])) + ("\n…" if len(items) > 5 else "")
            table.add_row(label, str(len(items)), preview)
        if self.failures:
            preview = escape(
                "\n".join(
                    f"{f.locator}: {f.reason}" + (f"\n  → {f.hint}" if f.hint else "")
                    for f in self.failures[:5]
                )
            )
            table.add_row("Failed", str(len(self.failures)), preview)
        return table


def plan_ingest(
    manifest: SourceManifest,
    settings: Settings,
    only: set[SourceKind] | None = None,
    client: httpx.Client | None = None,
) -> IngestPlan:
    """Estimate the work an ingest run would do. Only the RSS feed is fetched."""
    plan = IngestPlan()

    if _wanted(SourceKind.WEBSITE, only):
        plan.web_pages = sum(w.max_pages if w.crawl else 1 for w in manifest.websites)

    if _wanted(SourceKind.YOUTUBE, only):
        count = len(manifest.youtube.episodes)
        if manifest.youtube.urls_file:
            try:
                count += len(read_urls_file(settings.project_root / manifest.youtube.urls_file))
            except Exception as exc:
                logger.debug("could not read urls_file for the plan: %s", exc)
                plan.youtube_error = str(exc)
        available = count
        for channel in manifest.youtube.channels:
            # Uses the cached listing when there is one, so --dry-run stays cheap on re-runs.
            try:
                everything = expand_channel(
                    channel,
                    settings.youtube_cache_dir,
                    limit=None,
                    skip_shorts=manifest.youtube.skip_shorts,
                    max_short_seconds=manifest.youtube.max_short_seconds,
                )
            except Exception as exc:
                logger.debug("could not list channel %s: %s", channel, exc)
                plan.youtube_error = str(exc)
                continue
            available += len(everything)
            per_channel = manifest.youtube.max_videos_per_channel
            count += min(len(everything), per_channel) if per_channel else len(everything)
        plan.youtube_videos = count
        plan.youtube_available = available

    if _wanted(SourceKind.PODCAST, only) and (manifest.podcast.rss or manifest.podcast.episodes):
        try:
            resolved = resolve_transcript_plan(manifest.podcast, settings, client)
        except Exception as exc:
            logger.debug("podcast plan failed: %s", exc)
            plan.podcast_error = str(exc)
            resolved = []
        for episode, method in resolved:
            plan.podcast_by_method[method] = plan.podcast_by_method.get(method, 0) + 1
            if method == "audio_transcribe":
                seconds = episode.duration_seconds
                plan.transcription_minutes += seconds / 60.0 if seconds else UNKNOWN_EPISODE_MINUTES
    return plan


def _wanted(kind: SourceKind, only: set[SourceKind] | None) -> bool:
    return only is None or kind in only


def run_ingest(
    manifest: SourceManifest,
    store: Any,
    embedder: Any | None,
    settings: Settings,
    only: set[SourceKind] | None = None,
    force: bool = False,
    prune: bool = False,
    allow_transcription: bool = True,
    client: httpx.Client | None = None,
    api: Any | None = None,
    log: Callable[[str], None] | None = None,
) -> IngestReport:
    """Ingest every configured source into the knowledge base."""
    report = IngestReport(started_at=datetime.now(UTC).isoformat(timespec="seconds"))
    owns_client = client is None
    client = client or make_client(settings.http_timeout_seconds)
    seen_ids: set[str] = set()

    try:
        for item in _iter_sources(
            manifest, settings, only, client, api, allow_transcription, force, log
        ):
            if isinstance(item, IngestFailure):
                report.failures.append(item)
                continue
            try:
                _store_document(item, store, embedder, settings, force, report)
                seen_ids.add(item.id)
            except Exception as exc:
                logger.exception("failed to store %s", item.locator)
                report.failures.append(
                    IngestFailure(item.kind, item.locator, f"Could not save: {exc}", None)
                )

        _handle_orphans(store, only, seen_ids, prune, report, log)
    finally:
        if owns_client:
            client.close()

    report.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        store.record_ingest_run(report.summary_dict())
    except Exception as exc:
        logger.debug("could not record the ingest run: %s", exc)
    return report


def _iter_sources(
    manifest: SourceManifest,
    settings: Settings,
    only: set[SourceKind] | None,
    client: httpx.Client,
    api: Any | None,
    allow_transcription: bool,
    force: bool,
    log: Callable[[str], None] | None,
) -> Iterator[Document | IngestFailure]:
    if _wanted(SourceKind.WEBSITE, only) and manifest.websites:
        yield from _guarded(
            SourceKind.WEBSITE,
            lambda: ingest_websites(
                manifest.websites, settings.web_cache_dir, client, settings, log
            ),
        )

    if _wanted(SourceKind.YOUTUBE, only) and manifest.youtube.has_sources():
        yield from _guarded(
            SourceKind.YOUTUBE,
            lambda: ingest_youtube(
                manifest.youtube,
                settings.youtube_cache_dir,
                client=client,
                api=api,
                languages=settings.youtube_languages,
                project_root=settings.project_root,
                refresh_channels=force,
                log=log,
            ),
        )

    if _wanted(SourceKind.PODCAST, only) and (manifest.podcast.rss or manifest.podcast.episodes):
        yield from _guarded(
            SourceKind.PODCAST,
            lambda: ingest_podcast(
                manifest.podcast,
                settings,
                settings.raw_dir / "podcast",
                client=client,
                allow_transcription=allow_transcription,
                log=log,
            ),
        )


def _guarded(kind: SourceKind, factory) -> Iterator[Document | IngestFailure]:
    """Run an ingester, converting an unexpected explosion into one failure."""
    try:
        yield from factory()
    except Exception as exc:
        logger.exception("%s ingestion failed", kind.value)
        yield IngestFailure(kind, kind.value, f"{kind.value} ingestion failed: {exc}", None)


def _store_document(
    document: Document,
    store: Any,
    embedder: Any | None,
    settings: Settings,
    force: bool,
    report: IngestReport,
) -> None:
    label = f"{document.title} ({document.locator})"
    content_hash = document.ensure_hash()
    existing = store.document_hash(document.id)

    if existing == content_hash and not force:
        report.skipped.append(label)
        return

    chunks = chunk_document(
        document,
        target_words=settings.chunk_target_words,
        overlap_words=settings.chunk_overlap_words,
        av_window_seconds=settings.av_window_seconds,
    )
    embeddings = None
    model_name = None
    if embedder is not None and chunks:
        embeddings = embedder.embed_documents([c.text for c in chunks])
        model_name = embedder.name

    store.upsert_document(document, chunks, embeddings, embedding_model=model_name)
    (report.updated if existing is not None else report.added).append(label)


def _handle_orphans(
    store: Any,
    only: set[SourceKind] | None,
    seen_ids: set[str],
    prune: bool,
    report: IngestReport,
    log: Callable[[str], None] | None,
) -> None:
    try:
        stored = store.list_locators()
    except Exception as exc:
        logger.debug("could not list stored documents: %s", exc)
        return

    orphans = []
    for doc_id, locator in stored.items():
        if doc_id in seen_ids:
            continue
        if only is not None and not any(doc_id.startswith(f"{k.value}-") for k in only):
            continue
        orphans.append((doc_id, locator))

    for doc_id, locator in orphans:
        if prune:
            store.delete_document(doc_id)
            report.pruned.append(locator)
        elif log:
            log(f"Still stored but no longer in sources.yaml: {locator} (use --prune to remove)")


__all__ = ["IngestPlan", "IngestReport", "plan_ingest", "run_ingest", "Path"]
