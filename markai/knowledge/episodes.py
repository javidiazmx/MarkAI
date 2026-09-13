"""A searchable index of the podcast and the YouTube episodes.

Two questions a landlord actually asks: "which episode covers this?" and "who was the guest
on that one?" Both are answerable from what ingest already stored, so nothing here calls a
model or the network.

- **Number** comes from the feed (``Document.episode``).
- **Guest** is read off the title. Titles are written by people, so the parser is tolerant
  and refuses rather than guesses: a name it is not confident about comes back as ``None``.
- **Topics** are the terms that set the episode apart from the rest of the corpus, scored
  with the BM25 index's own idf inside a band on how many documents use the term at all. The
  band is what separates a subject from trivia: a word in one document is a name or a brand,
  a word in a quarter of them is furniture like the show's own footer. Words already on
  screen, the title and the guest, are excluded rather than repeated.
- **Timestamp** is the start of the passage that matched, which is what makes the answer
  useful: the episode plus the minute, not just the episode.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from markai.models import Document, RetrievedChunk, SourceKind

logger = logging.getLogger(__name__)

AV_KINDS: tuple[SourceKind, ...] = (SourceKind.PODCAST, SourceKind.YOUTUBE)

# The hosts are never "the guest". Add co-hosts here as the show adds them.
HOSTS: frozenset[str] = frozenset({"mark ainley"})

MAX_TOPICS = 6
MAX_QUOTE_CHARS = 220

# Caption tracks mark a change of speaker with ">>", which reads as noise inside a quote.
_SPEAKER = re.compile(r"\s*>>+\s*")

# "Ep. 214:", "Episode 214 -", "#214", "214." at the front of a title.
_EPISODE_PREFIX = re.compile(
    r"^\s*(?:ep(?:isode)?\.?\s*|#)\s*\d+\s*[:\-–|]?\s*|^\s*\d{1,4}\s*[:\-–|]\s*",
    re.IGNORECASE,
)
_LEAD_IN = re.compile(
    r"\b(?:with|w/|ft\.?|feat\.?|featuring|guest|guests|con|invitado|invitada)\b\s*:?\s*",
    re.IGNORECASE,
)
_NAME_PART = re.compile(r"^(?:[A-Z][\w'’\-]*|de|del|la|van|von|der|di|da|Mc|O')$")
# "with Jay Patel and Tedi Nati", "W/Duke Dennis & Bryan Sonn": two guests, one episode.
_ANOTHER_GUEST = re.compile(r"\s+(?:and|&|y)\s+|\s*,\s*", re.IGNORECASE)
MAX_GUESTS = 3
_NUMBERED = re.compile(r"\d")


def strip_episode_prefix(title: str) -> str:
    """``"Ep. 214: Boilers 101"`` becomes ``"Boilers 101"``."""
    return _EPISODE_PREFIX.sub("", title or "").strip()


def _looks_like_a_name(text: str) -> bool:
    words = text.split()
    if not (2 <= len(words) <= 4) or len(text) > 40 or _NUMBERED.search(text):
        return False
    if text.lower() in HOSTS:
        return False
    return all(_NAME_PART.match(word.strip(".,")) for word in words)


def _names_in(tail: str) -> str | None:
    """One guest, or the two or three an episode sometimes has, joined as they read."""
    parts = [part.strip(" .:-–|") for part in _ANOTHER_GUEST.split(tail)]
    parts = [part for part in parts if part]
    if not parts or len(parts) > MAX_GUESTS:
        return None
    if not all(_looks_like_a_name(part) for part in parts):
        return None
    return " and ".join(parts)


def guest_from_title(title: str) -> str | None:
    """The guest's name if the title names one, otherwise ``None``.

    Titles come from a human typing into a feed, so this reads the shapes that actually
    occur and declines everything else. A wrong name is worse than no name.
    """
    text = strip_episode_prefix(title or "")
    if not text:
        return None

    # "... with Jane Doe", "... ft. Jane Doe and John Roe", "Guest: Jane Doe"
    lead_in = False
    for match in _LEAD_IN.finditer(text):
        lead_in = True
        tail = text[match.end() :]
        tail = re.split(r"[|(\[]|\s[-–]\s|\bon\b|\babout\b|\bsobre\b", tail)[0]
        found = _names_in(tail)
        if found:
            return found

    # "Jane Doe on Boilers", "Jane Doe: Boilers". Only for a title that never said "with":
    # "Cracking Cash Flow on Chicago's West Side with Jay Patel" is three capitalised words
    # in front of "on" and not a person, and a title that names its guest with a lead-in
    # has already had its say.
    if lead_in:
        return None
    head = re.split(r"\bon\b|:|\||\s[-–]\s", text, maxsplit=1)[0].strip(" .:-–|")
    if _looks_like_a_name(head):
        return head
    return None


def deep_link(document: Document, start_time: float | None) -> str | None:
    """The best URL for a moment: a YouTube watch link jumps to the second."""
    url = document.link or document.locator
    if not url or not url.startswith(("http://", "https://")):
        return None
    if document.kind == SourceKind.YOUTUBE and start_time is not None:
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}t={int(start_time)}s"
    return url


def _singular(term: str) -> str:
    """Enough of a stem to see that "porch" and "porches" are one word, not two."""
    for suffix, replacement in (("ies", "y"), ("ches", "ch"), ("shes", "sh"), ("sses", "ss")):
        if len(term) > 4 and term.endswith(suffix):
            return term[: -len(suffix)] + replacement
    return term[:-1] if len(term) > 4 and term.endswith("s") else term


def dedupe_terms(terms: list[str]) -> list[str]:
    """Keep the first of "deposit" and "deposits"; they are not two topics."""
    kept: list[str] = []
    stems: set[str] = set()
    for term in terms:
        stem = _singular(term)
        if stem in stems:
            continue
        stems.add(stem)
        kept.append(term)
    return kept


@dataclass
class EpisodeMoment:
    """One episode, and the minute in it that answered the question."""

    kind: SourceKind
    title: str
    number: str | None = None
    guest: str | None = None
    url: str | None = None
    timestamp: str | None = None
    start_time: float | None = None
    published_at: str | None = None
    channel: str | None = None
    topics: list[str] = field(default_factory=list)
    quote: str = ""
    score: float = 0.0

    def label(self) -> str:
        """``Ep. 214 · 12:30`` for a list, falling back to the date."""
        bits = []
        if self.number:
            bits.append(f"Ep. {self.number}")
        if self.timestamp:
            bits.append(self.timestamp)
        if not bits and self.published_at:
            bits.append(self.published_at)
        return " · ".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "number": self.number,
            "title": self.title,
            "guest": self.guest,
            "url": self.url,
            "timestamp": self.timestamp,
            "published_at": self.published_at,
            "channel": self.channel,
            "topics": self.topics,
            "quote": self.quote,
        }


@dataclass
class EpisodeEntry:
    """One episode as the catalog lists it, with no question attached."""

    kind: SourceKind
    title: str
    number: str | None = None
    guest: str | None = None
    url: str | None = None
    published_at: str | None = None
    channel: str | None = None
    transcribed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "number": self.number,
            "title": self.title,
            "guest": self.guest,
            "url": self.url,
            "published_at": self.published_at,
            "channel": self.channel,
            "transcribed": self.transcribed,
        }


def format_timestamp(seconds: float) -> str:
    """``m:ss`` under an hour, ``h:mm:ss`` above it."""
    total = max(int(seconds), 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _timestamp(seconds: float | None) -> str | None:
    return None if seconds is None else format_timestamp(seconds)


def _already_shown(document: Any, guest: str | None) -> set[str]:
    """Words the reader can already see. A topic list that repeats them wastes its slots."""
    from markai.knowledge.retriever import tokenize

    return set(tokenize(f"{document.title} {guest or ''} {document.channel or ''}"))


def clean_quote(text: str) -> str:
    """One line of transcript, without the caption track's speaker markers."""
    return _SPEAKER.sub(" ", " ".join((text or "").split())).strip()[:MAX_QUOTE_CHARS]


def _moment(retriever: Any, rc: RetrievedChunk) -> EpisodeMoment:
    doc = rc.document
    guest = guest_from_title(doc.title)
    terms = retriever.distinctive_terms(
        doc.id, limit=MAX_TOPICS + 4, exclude=_already_shown(doc, guest)
    )
    return EpisodeMoment(
        kind=doc.kind,
        title=doc.title,
        number=doc.episode,
        guest=guest,
        url=deep_link(doc, rc.chunk.start_time),
        timestamp=_timestamp(rc.chunk.start_time),
        start_time=rc.chunk.start_time,
        published_at=doc.published_at,
        channel=doc.channel,
        topics=dedupe_terms(terms)[:MAX_TOPICS],
        quote=clean_quote(rc.chunk.text),
        score=rc.score,
    )


def find_moments(
    retriever: Any,
    query: str,
    limit: int = 5,
    kinds: tuple[SourceKind, ...] = AV_KINDS,
    channel: str | None = None,
) -> list[EpisodeMoment]:
    """The episodes that talk about ``query``, best first, one moment per episode.

    ``channel`` restricts to one show/channel by name (a case-insensitive substring
    match against the stored channel, so "Straight Up Chicago Investor podcast" still
    matches the stored "Straight Up Chicago Investor"). Without it, a two-host show and
    a single host's own separate channel compete in the same ranking - naming the show
    is the only way a question about one of them doesn't get answered from the other.
    """
    if not (query or "").strip() or retriever.is_empty():
        return []
    needle = (channel or "").strip().lower()
    # Retrieve wide and then keep only what is spoken: on most questions the blog posts
    # outrank the transcripts, so a plain top-k would come back with no episodes at all.
    # Filtering to one channel narrows the field further, so widen the pull accordingly.
    result = retriever.retrieve(query, k=max(limit * 20, 100) if needle else max(limit * 10, 50))
    best: dict[str, RetrievedChunk] = {}
    for rc in sorted(result.chunks, key=lambda c: -c.score):
        if rc.document.kind not in kinds or rc.document.id in best:
            continue
        if needle and needle not in (rc.document.channel or "").lower():
            continue
        best[rc.document.id] = rc
    return [_moment(retriever, rc) for rc in list(best.values())[:limit]]


def _sort_key(entry: EpisodeEntry) -> tuple:
    number = -1
    if entry.number and entry.number.isdigit():
        number = int(entry.number)
    return (number, entry.published_at or "", entry.title)


def catalog(
    store: Any,
    guest: str | None = None,
    kinds: tuple[SourceKind, ...] = AV_KINDS,
    limit: int | None = None,
) -> list[EpisodeEntry]:
    """Every episode in the knowledge base, newest first, optionally filtered by guest."""
    needle = (guest or "").strip().lower()
    entries: list[EpisodeEntry] = []
    for kind in kinds:
        for doc in store.list_documents(kind):
            name = guest_from_title(doc.title)
            if needle and needle not in (name or "").lower() and needle not in doc.title.lower():
                continue
            entries.append(
                EpisodeEntry(
                    kind=doc.kind,
                    title=doc.title,
                    number=doc.episode,
                    guest=name,
                    url=doc.link or (doc.locator if doc.locator.startswith("http") else None),
                    published_at=doc.published_at,
                    channel=doc.channel,
                    transcribed=doc.metadata.get("transcript_method") != "show_notes",
                )
            )
    entries.sort(key=_sort_key, reverse=True)
    return entries[:limit] if limit else entries


# --- the tool Jay can call ---------------------------------------------------------------

EPISODE_TOOL: dict[str, Any] = {
    "name": "find_episode",
    "description": (
        "Search the podcast and video transcripts for the episodes that discuss a topic. "
        "Call this when the user asks which episode covers something, asks for a link to "
        "an episode, asks who talked about a topic, or when pointing them at an episode "
        "would answer better than a summary. Returns the episode number, title, guest, a "
        "link that jumps to the moment, and a short quote. Do not guess an episode number "
        "or a guest name yourself: if this returns nothing, say there isn't one.\n\n"
        "The sources span more than one show - the Straight Up Chicago Investor podcast "
        "(two hosts plus guests) and Mark Ainley's own separate GC Realty channel are "
        "different, and a question naming one should not be answered from the other. "
        "Pass `channel` whenever the user names a specific show, host, or channel "
        '("the Straight Up Chicago Investor podcast", "Mark Ainley\'s channel", '
        '"Chicago Landlord Secrets"), and never claim a quote or a fact came from a '
        "named show unless the result you got back actually came from it."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "What to look for, in the user's own words. English or Spanish.",
            },
            "limit": {
                "type": "integer",
                "description": "How many episodes to return, 1 to 5. Use 3 unless asked for more.",
            },
            "channel": {
                "type": ["string", "null"],
                "description": (
                    "Restrict to one show/channel by name, when the user named one - e.g. "
                    "'Straight Up Chicago Investor' or 'Mark Ainley'. A substring match, so "
                    "the exact stored name doesn't need to be known. Omit (null) otherwise."
                ),
            },
        },
        "required": ["topic", "limit", "channel"],
        "additionalProperties": False,
    },
}


def run_episode_tool(retriever: Any, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Serve ``find_episode``. Always returns a JSON-serializable dict, never raises."""
    topic = str((tool_input or {}).get("topic") or "").strip()
    if not topic:
        return {"error": "topic is required."}
    try:
        limit = int((tool_input or {}).get("limit") or 3)
    except (TypeError, ValueError):
        limit = 3
    limit = max(1, min(limit, 5))
    channel = (tool_input or {}).get("channel") or None
    try:
        moments = find_moments(retriever, topic, limit=limit, channel=channel)
    except Exception as exc:  # a search failing must not fail the answer
        logger.warning("find_episode failed: %s", exc)
        return {"error": "The episode index could not be searched."}
    result: dict[str, Any] = {
        "topic": topic,
        "episodes": [m.to_dict() for m in moments],
        "found": len(moments),
    }
    if channel:
        result["channel_filter"] = channel
        if not moments:
            result["note"] = (
                f"Nothing found on this topic from a channel matching {channel!r}. "
                "Say so plainly rather than answering from a different show."
            )
    return result
