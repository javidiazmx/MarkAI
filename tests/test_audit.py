"""``mark audit``: does the knowledge base actually answer, and can it say why not.

Each test below is one of the failures this project actually shipped and had to find by
reading a bad answer. The point of the command is that they surface without that.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from markai.audit import PROBE_QUESTIONS, audit
from markai.cli import app
from markai.knowledge.chunking import chunk_document
from markai.knowledge.retriever import Retriever
from markai.knowledge.store import KnowledgeStore
from markai.models import Document, SourceKind
from markai.sources.manifest import PodcastSection, SourceManifest, WebsiteSource, YouTubeSection

runner = CliRunner()

FOOTER = (
    "The GC Realty Experience is providing the right solutions and being easy to do business "
    "with, leaving you with a remarkable customer experience every single time."
)


def _doc(locator: str, text: str, kind: SourceKind = SourceKind.WEBSITE) -> Document:
    doc = Document(
        id=Document.make_id(kind, locator),
        kind=kind,
        title=locator.rsplit("/", 1)[-1] or locator,
        locator=locator,
        text=text,
        link=locator,
    )
    doc.ensure_hash()
    return doc


def _fill(store: KnowledgeStore, documents: list[Document]) -> None:
    for doc in documents:
        store.upsert_document(doc, chunk_document(doc, target_words=60, overlap_words=10))


def _manifest(urls: list[str]) -> SourceManifest:
    return SourceManifest(websites=[WebsiteSource(url=u) for u in urls])


@pytest.fixture
def store(settings):
    settings.ensure_dirs()
    knowledge = KnowledgeStore(settings.db_path)
    yield knowledge
    knowledge.close()


def _audit(store, settings, manifest):
    return audit(store, manifest, Retriever(store, None, settings))


def test_an_empty_store_is_a_problem_not_a_clean_bill(store, settings):
    report = _audit(store, settings, _manifest(["https://a.test"]))
    assert not report.ok()
    assert report.problems[0].area == "empty"


def test_a_listed_site_that_contributed_nothing_is_named(store, settings):
    """The podcast site resolved, ingested, and produced zero pages. Nothing said so."""
    _fill(store, [_doc("https://a.test/x", "Deposits earn interest in Chicago. " * 30)])
    report = _audit(store, settings, _manifest(["https://a.test", "https://silent.test"]))

    assert not report.ok()
    silent = [f for f in report.problems if "silent.test" in f.detail]
    assert silent and "probe" in silent[0].fix
    assert ("silent.test", 0) in report.source_coverage


def test_a_configured_feed_with_nothing_stored_is_a_problem(store, settings):
    _fill(store, [_doc("https://a.test/x", "Deposits earn interest in Chicago. " * 30)])
    manifest = _manifest(["https://a.test"])
    manifest.podcast = PodcastSection(rss="https://feeds.test/rss")
    manifest.youtube = YouTubeSection(channels=["https://www.youtube.com/@x"])

    report = _audit(store, settings, manifest)
    areas = {f.detail.split()[0] for f in report.problems}
    assert "Podcast" in areas and "YouTube" in areas


def test_the_same_text_stored_twice_is_reported(store, settings):
    """Two domains serving one site. A search returns the same passage twice."""
    body = "Security deposits must be returned within 45 days of move out. " * 20
    _fill(store, [_doc("https://a.test/p", body), _doc("https://b.test/p", body)])
    report = _audit(store, settings, _manifest(["https://a.test", "https://b.test"]))

    duplicates = [f for f in report.problems if "more than once" in f.detail]
    assert duplicates and "--prune" in duplicates[0].fix


def test_a_footer_on_every_page_is_reported_as_furniture(store, settings):
    """The failure that produced five identical listings for a deposit question."""
    docs = [
        _doc(f"https://a.test/listing-{i}", f"{FOOTER}\nUnit {i} at {100 + i} West Street.")
        for i in range(12)
    ]
    docs.append(_doc("https://a.test/blog", f"{FOOTER}\nDeposit interest is owed yearly. " * 20))
    _fill(store, docs)

    report = _audit(store, settings, _manifest(["https://a.test"]))
    furniture = [f for f in report.problems if "menu or footer" in f.fix]
    assert furniture, "text on a quarter of the store is furniture, not content"
    assert "copies of" in furniture[0].detail


def test_missing_embeddings_are_reported_by_share(store, settings):
    _fill(store, [_doc("https://a.test/x", "Deposits earn interest in Chicago. " * 40)])
    report = _audit(store, settings, _manifest(["https://a.test"]))

    search = [f for f in report.findings if f.area == "search"]
    assert search and "only matches exact words" in search[0].detail
    assert search[0].severity == "warning", "keyword-only still works; it is not broken"


def test_probes_run_in_both_languages_and_report_coverage(store, settings):
    _fill(store, [_doc("https://a.test/x", "Security deposits and the RLTO in Chicago. " * 30)])
    report = _audit(store, settings, _manifest(["https://a.test"]))

    assert len(report.probes) == len(PROBE_QUESTIONS)
    assert any("cuanto le regreso" in q for _t, q, _c, _top in report.probes), "Spanish too"
    for _topic, _question, coverage, _top in report.probes:
        assert coverage in ("covered", "weak", "none")


def test_a_healthy_store_passes(store, settings, toy_documents):
    for doc in toy_documents:
        store.upsert_document(doc, chunk_document(doc, target_words=60, overlap_words=10))
    manifest = SourceManifest(websites=[WebsiteSource(url="https://example.com/deposits")])

    report = audit(store, manifest, Retriever(store, None, settings))
    blocking = [f for f in report.problems if f.area in ("empty", "content", "source")]
    assert blocking == [], f"a clean corpus should not be flagged: {blocking}"


def test_the_command_exits_non_zero_when_something_is_broken(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites:\n  - url: https://nothing.test\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))

    result = runner.invoke(app, ["audit"])
    assert result.exit_code == 1
    assert "problem" in result.stdout.lower()


def test_a_site_that_redirects_elsewhere_is_not_called_silent(store, settings):
    """The pages are stored under the host that answered, not the one that was asked."""
    doc = _doc("https://newname.test/deposits", "Deposit rules for the county. " * 30)
    doc.metadata = {"requested_url": "https://oldname.test/"}
    doc.ensure_hash()
    _fill(store, [doc])

    report = _audit(store, settings, _manifest(["https://oldname.test/"]))
    silent = [f for f in report.problems if "oldname.test" in f.detail]
    assert silent == [], "it answered, just under another name"
    assert any("oldname.test -> newname.test" in name for name, _n in report.source_coverage)
