"""Runs the whole ingest: manifest in, knowledge base out.

Two entry points. ``plan_ingest`` shows what a run would do (and warns before hours of
transcription); ``run_ingest`` does it. A failure in one source never stops the rest.
"""

from __future__ import annotations

import logging
import time
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
from markai.ingest.youtube import (
    build_transcript_api,
    expand_channel,
    ingest_youtube,
    read_urls_file,
)
from markai.knowledge.chunking import chunk_document
from markai.models import Document, IngestError, IngestFailure, SourceKind
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
    embedded: int = 0
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
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
            "duplicates": len(self.duplicates),
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
            ("Duplicate", self.duplicates),
            ("Removed", self.pruned),
        ):
            preview = escape("\n".join(items[:5])) + ("\n…" if len(items) > 5 else "")
            table.add_row(label, str(len(items)), preview)
        if self.failures:
            grouped = self.failure_summary()
            preview = "\n".join(f"{count} x {reason}" for reason, count in grouped[:6])
            if len(grouped) > 6:
                preview += f"\n... and {len(grouped) - 6} other kinds"
            table.add_row("Failed", str(len(self.failures)), escape(preview))
        if self.embedded:
            table.add_row("Embedded", str(self.embedded), "chunks given semantic search")
        return table

    def failure_summary(self) -> list[tuple[str, int]]:
        """Failures counted by kind, commonest first. 1,600 rows help nobody; six do."""
        counts: dict[str, int] = {}
        for failure in self.failures:
            # Drop the URL so the same problem on many pages collapses into one line.
            reason = failure.reason.split(":")[0].strip() or failure.reason
            counts[reason] = counts.get(reason, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def write_details(self, path: Path) -> Path:
        """Write every failure to a file, since the table only has room for a summary."""
        lines = [
            f"Ingest run {self.started_at} -> {self.finished_at}",
            f"added {len(self.added)}, updated {len(self.updated)}, "
            f"unchanged {len(self.skipped)}, removed {len(self.pruned)}, "
            f"failed {len(self.failures)}",
            "",
        ]
        if self.failures:
            lines.append("FAILURES")
            for reason, count in self.failure_summary():
                lines.append(f"  {count:>6} x {reason}")
            lines.append("")
            for failure in self.failures:
                lines.append(f"[{failure.kind.value}] {failure.locator}")
                lines.append(f"    {failure.reason}")
                if failure.hint:
                    lines.append(f"    -> {failure.hint}")
        for label, items in (
            ("ADDED", self.added),
            ("UPDATED", self.updated),
            ("UNCHANGED", self.skipped),
            ("REMOVED", self.pruned),
        ):
            if items:
                lines.extend(["", label, *(f"  {item}" for item in items)])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


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
        report.embedded = _backfill_embeddings(store, embedder, settings, log).done
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
        # Built once per run so a proxy or cookie jar is set up a single time, and so a bad
        # cookie file is one clear failure rather than one per video.
        captions = api
        if captions is None:
            try:
                captions = build_transcript_api(settings)
            except IngestError as exc:
                yield IngestFailure(SourceKind.YOUTUBE, "youtube", str(exc), exc.hint)
                return

        yield from _guarded(
            SourceKind.YOUTUBE,
            lambda: ingest_youtube(
                manifest.youtube,
                settings.youtube_cache_dir,
                client=client,
                api=captions,
                languages=settings.youtube_languages,
                project_root=settings.project_root,
                refresh_channels=force,
                log=log,
                delay_seconds=settings.youtube_delay_seconds,
                cookies_from_browser=settings.youtube_cookies_from_browser,
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

    twin = store.locator_with_hash(content_hash, document.id)
    if twin is not None:
        # Same bytes already stored under another address. Keeping both would put two
        # identical passages in every search result.
        report.duplicates.append(f"{label} - same text as {twin}")
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


# A Voyage account with no payment method is capped at 3 requests a minute, so a batch that
# is refused needs a real wait, not an instant retry.
EMBED_BACKOFF_SECONDS = (20.0, 40.0, 60.0, 120.0)

_sleep: Callable[[float], None] = time.sleep


@dataclass
class EmbedResult:
    """What a backfill actually managed.

    A bare count cannot tell "nothing was pending" from "every batch was refused", and
    reporting the second as the first told the owner their knowledge base was ready when
    not one passage had been embedded.
    """

    done: int = 0
    remaining: int = 0
    error: str | None = None

    def __bool__(self) -> bool:
        return self.done > 0


def _looks_like_a_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("rate limit", "429", "too many requests", " rpm", " tpm", "quota")
    )


def _backfill_embeddings(
    store: Any,
    embedder: Any | None,
    settings: Settings,
    log: Callable[[str], None] | None,
) -> EmbedResult:
    """Embed stored chunks that have none yet, so adding a key later costs no downloads."""
    if embedder is None:
        return EmbedResult()
    try:
        pending = store.chunks_missing_embeddings(embedder.name)
    except Exception as exc:
        logger.debug("could not look for chunks missing embeddings: %s", exc)
        return EmbedResult(error=str(exc))
    if not pending:
        return EmbedResult()

    if log:
        log(f"Embedding {len(pending)} stored passages with {embedder.name}")

    result = EmbedResult(remaining=len(pending))
    batch_size = max(settings.embedding_batch_size, 1)
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        vectors = None
        for wait in (*EMBED_BACKOFF_SECONDS, None):
            try:
                vectors = embedder.embed_documents([c.text for c in batch])
                break
            except Exception as exc:
                if wait is None or not _looks_like_a_rate_limit(exc):
                    # Not something waiting will fix. Stop, and say how far we got: the
                    # embedded passages are already stored, so a re-run resumes here.
                    logger.warning("embedding stopped after %s passages: %s", result.done, exc)
                    result.error = str(exc)
                    return result
                if log:
                    log(f"Voyage is rate-limiting. Waiting {wait:.0f}s, then trying again.")
                _sleep(wait)
        if vectors is None:  # pragma: no cover - the loop above always sets or returns
            return result

        store.set_embeddings({c.id: v for c, v in zip(batch, vectors, strict=False)}, embedder.name)
        result.done += len(batch)
        result.remaining = len(pending) - result.done
        if log and result.done % (batch_size * 10) == 0:
            log(f"  {result.done:,} of {len(pending):,} passages embedded")
    return result


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
