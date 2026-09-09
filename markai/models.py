"""Core data types shared by every part of MarkAI.

These are the contracts between ingestion (turns sources into ``Document`` objects),
the knowledge layer (chunks, stores, and retrieves them), and the advisor (asks Claude
and returns an ``AdvisorResponse`` with citations). Keep this module dependency-free
apart from the standard library so any layer can import it.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class SourceKind(StrEnum):
    """The three kinds of material the owner can feed Mark."""

    WEBSITE = "website"
    YOUTUBE = "youtube"
    PODCAST = "podcast"


class IngestError(Exception):
    """Raised by an ingester when a single source cannot be processed.

    ``hint`` is a plain-language next step shown to the owner (e.g. "add a transcript file").
    """

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass
class IngestFailure:
    """A source that could not be ingested, with a reason and a suggested fix."""

    kind: SourceKind
    locator: str
    reason: str
    hint: str | None = None


@dataclass
class Segment:
    """A timed span of transcript text (seconds). Web pages have no segments."""

    start: float
    end: float
    text: str


@dataclass
class Document:
    """One ingested source: a web page, a YouTube episode, or a podcast episode.

    ``locator`` is the canonical identity used for the document id (a canonical URL, or a
    ``file:``/``episode:`` string for local material). ``link`` is the best human-facing URL
    to show in a citation (episode page, watch URL, article URL) and may be ``None``.
    """

    id: str
    kind: SourceKind
    title: str
    locator: str
    text: str
    segments: list[Segment] = field(default_factory=list)
    link: str | None = None
    published_at: str | None = None
    episode: str | None = None
    channel: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""

    @staticmethod
    def make_id(kind: SourceKind, locator: str) -> str:
        """Stable document id derived from the kind and canonical locator."""
        digest = hashlib.sha1(f"{kind.value}:{locator}".encode()).hexdigest()
        return f"{kind.value}-{digest[:16]}"

    def ensure_hash(self) -> str:
        """Fill ``content_hash`` (sha256 of the text) if it is empty and return it."""
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        return self.content_hash

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data


@dataclass
class Chunk:
    """A retrieval unit cut from a Document."""

    id: str
    doc_id: str
    index: int
    text: str
    start_char: int = 0
    end_char: int = 0
    start_time: float | None = None
    end_time: float | None = None
    heading: str | None = None

    @staticmethod
    def make_id(doc_id: str, index: int) -> str:
        return f"{doc_id}:{index:04d}"


@dataclass
class RetrievedChunk:
    """A chunk returned by the retriever, with its parent document and fused score."""

    chunk: Chunk
    document: Document
    score: float
    lexical_rank: int | None = None
    vector_rank: int | None = None


@dataclass
class Citation:
    """A source Mark actually cited, rendered as a footnote by the CLI and web UI."""

    marker: str
    kind: SourceKind
    title: str
    url: str | None = None
    episode: str | None = None
    channel: str | None = None
    timestamp: str | None = None
    published_at: str | None = None
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data


@dataclass
class AdvisorResponse:
    """Mark's final answer to one question.

    ``text`` is the complete, post-processed answer (disclaimer enforced, dangling citation
    markers removed). Consumers that streamed deltas must replace what they showed with
    ``text`` when it differs, and always when ``stop_reason == "refusal"``.
    """

    text: str
    citations: list[Citation] = field(default_factory=list)
    coverage: str = "none"
    flags: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    stop_reason: str | None = None
    tool_calls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "citations": [c.to_dict() for c in self.citations],
            "coverage": self.coverage,
            "flags": list(self.flags),
            "usage": dict(self.usage),
            "model": self.model,
            "stop_reason": self.stop_reason,
            "tool_calls": list(self.tool_calls),
        }
