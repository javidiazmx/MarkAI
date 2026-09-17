"""A government code, too large to fetch live, split into one Document per chapter."""

from __future__ import annotations

from markai.ingest.municipal_code import ingest_municipal_code, parse_chapters
from markai.models import Document, IngestFailure
from markai.sources.manifest import MunicipalCodeSource

SAMPLE = """TITLE 1
GENERAL PROVISIONS

CHAPTER 1-4
CODE ADOPTION - ORGANIZATION
1-4-010   Municipal Code of Chicago adopted.
   This ordinance, consisting of Titles 1 through 18, shall be known as the Code.

CHAPTER 1-8
CORPORATE SEAL AND EMBLEMS
1-8-010   Seal adopted.
   The city shall have a corporate seal.

CHAPTER 1-24
RESERVED
"""


def test_each_chapter_is_split_out_with_its_heading_kept():
    chapters = parse_chapters(SAMPLE)
    numbers = [number for number, _, _ in chapters]
    assert numbers == ["1-4", "1-8", "1-24"]
    _, title, body = chapters[0]
    assert title == "CODE ADOPTION - ORGANIZATION"
    assert "CHAPTER 1-4" in body and "corporate seal" not in body


def test_ingest_yields_one_document_per_real_chapter(tmp_path):
    path = tmp_path / "chicago.txt"
    path.write_text(SAMPLE, encoding="utf-8")
    section = MunicipalCodeSource(
        jurisdiction="Chicago",
        file=str(path),
        citation_url="https://library.municode.com/il/chicago/codes/code_of_ordinances",
        published="Current through Council Journal of June 17, 2026",
    )
    results = list(ingest_municipal_code(section, tmp_path))
    documents = [r for r in results if isinstance(r, Document)]
    # "Reserved" (1-24) is too short to be worth a Document - a landlord is never pointed
    # at an empty chapter.
    assert len(documents) == 2
    assert documents[0].title == (
        "Municipal Code of Chicago, Chapter 1-4: Code Adoption - Organization"
    )
    assert documents[0].link == section.citation_url
    assert documents[0].published_at == section.published
    assert documents[0].locator == "file:chicago-municipal-code#1-4"
    assert "corporate seal" in documents[1].text.lower()


def test_a_relative_file_path_resolves_against_the_project_root(tmp_path):
    (tmp_path / "sources").mkdir()
    path = tmp_path / "sources" / "code.txt"
    path.write_text(SAMPLE, encoding="utf-8")
    section = MunicipalCodeSource(
        jurisdiction="Chicago", file="sources/code.txt", citation_url="https://example.test"
    )
    documents = [r for r in ingest_municipal_code(section, tmp_path) if isinstance(r, Document)]
    assert len(documents) == 2


def test_a_missing_file_is_a_clear_failure_not_a_crash(tmp_path):
    section = MunicipalCodeSource(
        jurisdiction="Chicago", file="nope.txt", citation_url="https://example.test"
    )
    results = list(ingest_municipal_code(section, tmp_path))
    assert len(results) == 1
    assert isinstance(results[0], IngestFailure)
    assert "not found" in results[0].reason


def test_a_file_with_no_chapter_headings_is_a_clear_failure(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("Just some text with no chapters in it.", encoding="utf-8")
    section = MunicipalCodeSource(
        jurisdiction="Chicago", file=str(path), citation_url="https://example.test"
    )
    results = list(ingest_municipal_code(section, tmp_path))
    assert len(results) == 1
    assert isinstance(results[0], IngestFailure)
    assert "CHAPTER" in results[0].reason
