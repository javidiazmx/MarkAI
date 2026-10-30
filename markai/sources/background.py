"""Everything GC Realty has published, as one readable file for another model to read.

This is a different job from ``export.py``. That one packages the reviewed fact layer so a
chatbot can answer a landlord's question safely: a few hundred rules, each with the sentence
it came from, wrapped in the jurisdiction discipline the answer needs. This one is the
opposite shape - no rules, no guardrails, no question in mind. It is the company's own
material, in full and in its own words, so a model that reads it comes away knowing who GC
Realty is and what they do.

The corpus is already on disk, chunked for retrieval. The work here is turning it back into
something a person or a model reads top to bottom:

- **Boilerplate is stripped.** A crawl of four hundred pages carries the same nav, footer
  and cookie notice four hundred times. Left in, that is most of the file and all of it is
  noise. Any line that shows up across a large share of the pages is dropped once, globally,
  and the pack says how many lines went.
- **GC's own material only, by default.** The manifest crawls county and federal sites too,
  because Jay needs the ordinances. None of that is who GC Realty is, so it is left out
  unless asked for.
- **Split by size, on document boundaries.** A page is never cut in half, and every part
  says which part it is and what the others hold.

Nothing is summarized and nothing is rewritten. If a sentence is in the pack, somebody at GC
Realty wrote it.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from markai.models import Document, SourceKind

# The owner's own properties. Everything else in the manifest is a government or reference
# site that Jay reads for ordinances - useful to Jay, and nothing to do with who GC Realty is.
OWN_DOMAINS = ("gcrealtyinc.com", "straightupchicagoinvestor.com")

# A line printed on at least this share of pages is the template, not the page. Set by what
# a site actually looks like: a nav item is on every page, a category label on a third of
# them, and a sentence somebody wrote is on one.
BOILERPLATE_SHARE = 0.25
# Below this, a crawl of a handful of pages would strip its own content: with six documents,
# one shared sentence clears any share threshold.
BOILERPLATE_MIN_DOCS = 12
# Long lines are prose even when they repeat. A boilerplate line is a nav item or a footer.
BOILERPLATE_MAX_CHARS = 160

# Roughly how many characters of markdown fit a part. The limit is not the upload - Grok
# takes far larger files - it is that a reader, human or model, does better with the company
# split into a few readable pieces than with one file it has to seek around inside.
DEFAULT_PART_CHARS = 350_000


@dataclass
class Section:
    """One group of documents that prints under its own heading."""

    key: str
    title: str
    blurb: str
    documents: list[Document]


def is_own(doc: Document) -> bool:
    where = f"{doc.link or ''} {doc.locator or ''}".lower()
    return any(domain in where for domain in OWN_DOMAINS)


def _is_blog(doc: Document) -> bool:
    where = f"{doc.link or ''} {doc.locator or ''}".lower()
    return "/blog/" in where or "/post/" in where


def _clean(text: str) -> list[str]:
    """The document as lines, with runs of blank lines collapsed."""
    out: list[str] = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line or (out and out[-1]):
            out.append(line)
    while out and not out[-1]:
        out.pop()
    return out


def boilerplate_lines(docs: list[Document]) -> set[str]:
    """The lines that belong to the site template rather than to any page.

    Counted once per document, so a nav item repeated twice on one page does not count
    twice, and measured as a share of the pages rather than a fixed number - a threshold of
    "appears 50 times" means something different across 60 pages than across 600.
    """
    if len(docs) < BOILERPLATE_MIN_DOCS:
        return set()
    seen: Counter[str] = Counter()
    for doc in docs:
        for line in {line for line in _clean(doc.text) if line}:
            if len(line) <= BOILERPLATE_MAX_CHARS:
                seen[line] += 1
    floor = max(2, int(len(docs) * BOILERPLATE_SHARE))
    return {line for line, count in seen.items() if count >= floor}


def _body(doc: Document, drop: set[str]) -> str:
    kept = [line for line in _clean(doc.text) if line not in drop]
    while kept and not kept[0]:
        kept.pop(0)
    # One blank line between paragraphs, never three.
    out: list[str] = []
    for line in kept:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def _heading(doc: Document) -> list[str]:
    bits = []
    if doc.episode:
        bits.append(f"Episode {doc.episode}")
    if doc.published_at:
        bits.append(str(doc.published_at)[:10])
    link = doc.link or (doc.locator if str(doc.locator).startswith("http") else "")
    out = [f"### {' '.join((doc.title or 'Untitled').split())}"]
    meta = " · ".join(bits)
    if link:
        meta = f"{meta} · <{link}>" if meta else f"<{link}>"
    if meta:
        out.append(f"*{meta}*")
    return out


def sections(docs: list[Document], episodes: bool) -> list[Section]:
    """The documents grouped the way somebody reading about a company would want them."""
    pages = [d for d in docs if d.kind == SourceKind.WEBSITE and not _is_blog(d)]
    posts = [d for d in docs if d.kind == SourceKind.WEBSITE and _is_blog(d)]
    out = [
        Section(
            "pages",
            "The site itself",
            "Service pages, FAQs and handbooks - how the company describes itself and how "
            "it works with owners and residents.",
            sorted(pages, key=lambda d: (d.title or "").lower()),
        ),
        Section(
            "posts",
            "What they write",
            "Every post on the blog, in full. This is the bulk of what the company knows "
            "and the clearest picture of what it spends its time on.",
            sorted(posts, key=lambda d: (str(d.published_at or ""), d.title or ""), reverse=True),
        ),
    ]
    if episodes:
        shows = [d for d in docs if d.kind in (SourceKind.PODCAST, SourceKind.YOUTUBE)]
        out.append(
            Section(
                "episodes",
                "Podcast and video",
                "Transcripts. A transcript does not record who is speaking, and the show "
                "has a co-host and a guest in most episodes - so an episode covering "
                "something is a safe thing to say, and any named person having said it is "
                "not.",
                sorted(shows, key=lambda d: (str(d.published_at or ""), d.title or "")),
            )
        )
    return [s for s in out if s.documents]


def _front_matter(business: object, found: list[Section], dropped: int, today: str) -> list[str]:
    name = getattr(business, "name", None) or "GC Realty & Development"
    say = [f"# {name} — background pack", "", f"Exported {today}. "]
    say[-1] += (
        "Everything below was written and published by this company. Nothing here was "
        "generated, summarized or rewritten: it is their own pages, posts and transcripts, "
        "copied out of the index behind their advisor."
    )
    say += ["", "## What this is for", ""]
    say += [
        "Read it as background on the company: who they are, what they do, who they do it "
        "for, and what they have taken public positions on. It is not a rulebook and it is "
        "not organised for lookups - it is the company in its own words.",
        "",
        "Two things worth knowing while you read it:",
        "",
        "- **It is a point of view, not a neutral source.** These are a property management "
        "company's own marketing, guides and opinions. Where a post states a law or a "
        "number, treat it as what this company published on the day they published it, not "
        "as verified fact - several posts are years old and ordinances change.",
        "- **Dates matter and most posts carry one.** When a post's claim about a rule or a "
        "price is doing real work, say when it was written.",
        "",
    ]
    for key, label in (
        ("name", "Who they are"),
        ("services", "What they do"),
        ("service_area", "Where they work"),
        ("contact_url", "Site"),
    ):
        value = getattr(business, key, None)
        if value:
            say.append(f"- **{label}:** {' '.join(str(value).split())}")
    say += ["", "## What is in here", ""]
    for section in found:
        words = sum(len((d.text or "").split()) for d in section.documents)
        say.append(f"- **{section.title}** — {len(section.documents)} items, ~{words:,} words")
    if dropped:
        say.append(
            f"- {dropped:,} repeated navigation and footer lines were removed once each, "
            "so the site's template is not printed on every page."
        )
    say.append("")
    return say


def _render(sections_: list[Section], drop: set[str]) -> list[tuple[str, list[str]]]:
    """Each section as (title, lines), ready to be packed into parts."""
    out = []
    for section in sections_:
        lines = [f"## {section.title}", "", section.blurb, ""]
        for doc in section.documents:
            body = _body(doc, drop)
            if not body:
                continue
            lines += _heading(doc) + ["", body, ""]
        out.append((section.title, lines))
    return out


def background_pack(
    docs: list[Document],
    business: object,
    today: str,
    episodes: bool = False,
    part_chars: int = DEFAULT_PART_CHARS,
) -> list[str]:
    """The pack, as one string per part. One part when it fits, several when it does not."""
    found = sections(docs, episodes=episodes)
    drop = boilerplate_lines([d for d in docs if d.kind == SourceKind.WEBSITE])
    rendered = _render(found, drop)
    head = _front_matter(business, found, len(drop), today)

    blocks: list[list[str]] = [head]
    for _, lines in rendered:
        blocks.append(lines)

    # Pack whole blocks into parts, then split any single block that is bigger than a part
    # on its own - always between documents, never inside one.
    parts: list[list[str]] = [[]]
    for block in blocks:
        for piece in _split_block(block, part_chars):
            if parts[-1] and _size(parts[-1]) + _size(piece) > part_chars:
                parts.append([])
            parts[-1] += piece

    total = len(parts)
    out = []
    for index, lines in enumerate(parts, start=1):
        banner = [] if total == 1 else [f"<!-- part {index} of {total} -->", ""]
        if total > 1 and index > 1:
            banner += [
                f"*(Continued — part {index} of {total}. Part 1 has the front matter that "
                "says what this is and how to read it.)*",
                "",
            ]
        out.append("\n".join(banner + lines).strip() + "\n")
    return out


def _size(lines: list[str]) -> int:
    return sum(len(line) + 1 for line in lines)


_DOC_HEADING = re.compile(r"^### ")


def _split_block(lines: list[str], part_chars: int) -> list[list[str]]:
    """One section into chunks no larger than a part, cutting only at a document heading."""
    if _size(lines) <= part_chars:
        return [lines]
    chunks: list[list[str]] = [[]]
    for line in lines:
        if _DOC_HEADING.match(line) and chunks[-1] and _size(chunks[-1]) + len(line) > part_chars:
            chunks.append([])
        chunks[-1].append(line)
    return [chunk for chunk in chunks if chunk]
