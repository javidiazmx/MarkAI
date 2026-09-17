"""A government code of ordinances, supplied as a local text file - too large to fetch
live and too large to be one Document, so this splits it into one Document per chapter,
which is also the unit a real citation to it names ("Chapter 5-12" reads as one thing to
a landlord, not a fragment of Title 5).

The file is a plain export (a "Save Text" download from a code library site, or similar) -
not a URL this ingester fetches, so there is no robots.txt, no rate limit, no network at
all. What changed since the last run is judged the normal way: each chapter is its own
Document with its own content hash, so `mark ingest` only re-embeds a chapter that actually
changed the next time the file is refreshed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from markai.models import Document, IngestFailure, SourceKind
from markai.sources.manifest import MunicipalCodeSource

# A chapter heading is its own line ("CHAPTER 5-12"), immediately followed by the chapter's
# title in capitals on the next non-blank line. Matches "5-12", "14B-4", or similar - some
# titles split a chapter number with a letter, not just digits either side of the hyphen.
_CHAPTER_RE = re.compile(r"(?m)^CHAPTER\s+(\S+)\s*$")


def parse_chapters(text: str) -> list[tuple[str, str, str]]:
    """Every chapter as ``(number, title, body)``, in the order the file has them.

    ``body`` includes the heading lines themselves, so a chapter's own text always shows
    its number and title - useful once it is just a chunk in a citation, disconnected from
    the chapters around it.
    """
    matches = list(_CHAPTER_RE.finditer(text))
    chapters: list[tuple[str, str, str]] = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        number = match.group(1)
        body = text[start:end].strip()
        # The title is the next non-blank line after the heading.
        lines = body.splitlines()
        title = ""
        for line in lines[1:]:
            stripped = line.strip()
            if stripped:
                title = stripped
                break
        chapters.append((number, title, body))
    return chapters


def ingest_municipal_code(
    section: MunicipalCodeSource, project_root: Path
) -> Iterator[Document | IngestFailure]:
    """Yield one Document per chapter in the code file."""
    path = Path(section.file)
    if not path.is_absolute():
        path = project_root / path
    if not path.exists():
        yield IngestFailure(
            SourceKind.WEBSITE,
            section.file,
            f"Municipal code file not found: {path}",
            "Check the path in sources.yaml (it is relative to the project folder).",
        )
        return

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        yield IngestFailure(SourceKind.WEBSITE, section.file, f"Could not read {path}: {exc}", None)
        return
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1")

    chapters = parse_chapters(text)
    if not chapters:
        yield IngestFailure(
            SourceKind.WEBSITE,
            section.file,
            f"No 'CHAPTER <number>' headings found in {path}.",
            "Check the file actually contains the expected chapter-heading format.",
        )
        return

    for number, title, body in chapters:
        if len(body) < 40:
            # A "Reserved" chapter, or a stray heading with nothing under it - not
            # something a landlord would ever be pointed at.
            continue
        locator = f"file:{section.jurisdiction.lower()}-municipal-code#{number}"
        heading = f"Municipal Code of {section.jurisdiction}, Chapter {number}"
        if title:
            heading += f": {title.title()}"
        document = Document(
            id=Document.make_id(SourceKind.WEBSITE, locator),
            kind=SourceKind.WEBSITE,
            title=heading,
            locator=locator,
            text=body,
            link=section.citation_url,
            published_at=section.published,
        )
        document.ensure_hash()
        yield document
