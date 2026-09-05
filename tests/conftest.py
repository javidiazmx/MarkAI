"""Shared fixtures. Every test runs offline against a temporary data directory."""

from __future__ import annotations

import pytest

from markai.config import Settings
from markai.knowledge.chunking import chunk_document
from markai.knowledge.store import KnowledgeStore
from markai.models import Document, Segment, SourceKind

WEBSITE_TEXT = """Security deposits in Chicago come with strict rules under the RLTO.

You must hold the deposit in a federally insured account in Illinois and keep it separate
from your own operating money.

Interest is owed annually at the rate the City Comptroller publishes, and it must be paid
or credited within thirty days of the end of each twelve month rental period.

Returning the deposit late, or mixing it with your own funds, exposes you to damages that
are far larger than the deposit itself.
"""

SCREENING_SEGMENTS = [
    ("Tenant screening starts with written criteria you apply to every applicant", 0.0, 12.0),
    ("Income of three times the rent is a common threshold in Chicago", 12.0, 14.0),
    ("Pull credit and eviction history from a real reporting agency", 26.0, 15.0),
    ("Call the previous landlord, not the current one, for an honest reference", 41.0, 16.0),
    ("Document every decision so your file shows the same standard for everyone", 57.0, 18.0),
    ("Never bend criteria for one applicant and not another", 75.0, 15.0),
    ("A written policy is your best defense in a fair housing complaint", 90.0, 20.0),
    ("Vacancy costs less than a bad tenant who stops paying in month three", 110.0, 20.0),
]

HEAT_SEGMENTS = [
    ("Chicago heat ordinance season runs from September fifteenth to June first", 0.0, 14.0),
    ("Daytime temperature must reach sixty eight degrees inside the unit", 14.0, 15.0),
    ("Overnight the minimum drops to sixty six degrees", 29.0, 12.0),
    ("Boiler failures in January generate immediate code violations", 41.0, 16.0),
    ("Service the boiler in August, not when the first cold snap hits", 57.0, 18.0),
    ("Keep a heating contractor on call through the winter months", 75.0, 16.0),
    ("Tenants can call three one one and inspectors do show up", 91.0, 16.0),
]


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings pointed at a temp directory, with thresholds tuned for a toy corpus."""
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        sources_file=tmp_path / "sources.yaml",
        min_relevance=0.1,
        weak_relevance=0.5,
        top_k=5,
    )


def _av_document(kind: SourceKind, title: str, locator: str, segments, **kwargs) -> Document:
    segs = [Segment(start=s, end=s + d, text=t) for t, s, d in segments]
    doc = Document(
        id=Document.make_id(kind, locator),
        kind=kind,
        title=title,
        locator=locator,
        text=" ".join(s.text for s in segs),
        segments=segs,
        link=locator,
        **kwargs,
    )
    doc.ensure_hash()
    return doc


@pytest.fixture
def toy_documents() -> list[Document]:
    """One website, one YouTube episode, one podcast episode."""
    website = Document(
        id=Document.make_id(SourceKind.WEBSITE, "https://example.com/deposits"),
        kind=SourceKind.WEBSITE,
        title="Security deposit rules for Chicago landlords",
        locator="https://example.com/deposits",
        text=WEBSITE_TEXT,
        link="https://example.com/deposits",
        published_at="2024-02-01",
    )
    website.ensure_hash()
    youtube = _av_document(
        SourceKind.YOUTUBE,
        "Tenant screening that holds up",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        SCREENING_SEGMENTS,
        episode="212",
        channel="Straight Up Chicago Investor",
        published_at="2023-04-18",
    )
    podcast = _av_document(
        SourceKind.PODCAST,
        "Winter heat rules",
        "https://audio.example.com/198.mp3",
        HEAT_SEGMENTS,
        episode="198",
        channel="Straight Up Chicago Investor",
        published_at="2022-11-02",
    )
    return [website, youtube, podcast]


@pytest.fixture
def store(settings, toy_documents) -> KnowledgeStore:
    """A knowledge base holding the toy corpus."""
    settings.ensure_dirs()
    knowledge = KnowledgeStore(settings.db_path)
    for doc in toy_documents:
        chunks = chunk_document(doc, target_words=60, overlap_words=10, av_window_seconds=40.0)
        knowledge.upsert_document(doc, chunks)
    yield knowledge
    knowledge.close()


@pytest.fixture
def empty_store(settings) -> KnowledgeStore:
    settings.ensure_dirs()
    knowledge = KnowledgeStore(settings.db_path)
    yield knowledge
    knowledge.close()
