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


@pytest.fixture(autouse=True)
def no_real_env_file(monkeypatch):
    """`Settings()` must never read this developer's real `.env`.

    The stated convention is `Settings(_env_file=None, ...)` in a test - but every CLI test
    goes through `runner.invoke(app, [...])`, which calls the CLI's own `_settings()`, a bare
    `Settings()` with no override. `env_file` is a fixed absolute path on the class
    (`markai/config.py`), so on a machine with a real `.env` configured (this one has real
    `MARKAI_LEAD_EMAIL_TO`/SMTP settings), those real values leak into `mark leads`/`mark
    doctor` output for every field a test doesn't itself monkeypatch an env var for - a false
    failure that has nothing to do with the test's own logic.
    """
    from markai.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No real seconds and no real network.

    The delays and the backoff ladder are wall-clock time. The yt-dlp caption fallback is a
    live call to YouTube, and it fires whenever a test simulates a block - so by default it
    is stubbed out as "that route is blocked too". A test that wants the fallback patches
    ``_ytdlp_extract`` itself.
    """
    from markai.ingest import pipeline, websites, youtube

    monkeypatch.setattr(websites, "_sleep", lambda _seconds: None)
    monkeypatch.setattr(youtube, "_sleep", lambda _seconds: None)
    monkeypatch.setattr(pipeline, "_sleep", lambda _seconds: None)

    def blocked(url, options):
        raise youtube.RateLimitedError(f"stubbed: no network in tests ({url})")

    monkeypatch.setattr(youtube, "_ytdlp_extract", blocked)


@pytest.fixture(autouse=True)
def plain_console(monkeypatch):
    """`mark`'s output, asserted on as plain text, with no ANSI in it to strip.

    `NO_COLOR` alone does not do this: Rich's Windows terminal detection can decide the
    process is attached to a real console (checking the OS console handle, not
    `sys.stdout.isatty()`) even though `CliRunner` has swapped in a non-tty capture
    stream, so `console.is_terminal` comes back true and bold/dim SGR codes still go out
    even with color disabled. `force_terminal=False` is the one setting that overrides
    that detection outright, which is why every test-time console gets rebuilt with it
    rather than trusting environment variables Rich might second-guess.

    ``width=200`` too: a non-terminal Console falls back to 79 columns, and a table with
    several columns (``mark leads list``'s When/Name/Email/Why/State/Why-not, six wide)
    folds a long cell - "javier@example.com" - across a line break at that width, which
    breaks a plain `in result.stdout` substring check without the underlying data being
    wrong at all. Wide enough that nothing this project prints wraps.
    """
    import markai.cli as cli

    monkeypatch.setattr(cli, "console", cli.Console(force_terminal=False, no_color=True, width=200))
    monkeypatch.setattr(
        cli, "err", cli.Console(stderr=True, force_terminal=False, no_color=True, width=200)
    )


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
        youtube_delay_seconds=0.0,
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
