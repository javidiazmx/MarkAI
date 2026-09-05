"""Cut a ``Document`` into retrieval-sized ``Chunk`` objects.

Two strategies, chosen by whether the document carries timed segments:

* **Text documents** (web pages, transcripts without timing): paragraphs are packed to roughly
  ``target_words`` words with ``overlap_words`` carried into the next chunk. A paragraph that is
  itself far too long is split on sentence boundaries first and on words as a last resort.
* **Audio/video documents** (segments with timing): fixed time windows of ``av_window_seconds``
  with ~15 seconds of overlap, so every chunk has a ``start_time``/``end_time`` that citations
  can turn into a deep link.

Chunk ids are deterministic (``Chunk.make_id(doc.id, index)``) and no chunk is ever empty.
This module deliberately does not import ``markai.ingest``; the small transcript-joining
helper is re-implemented here.
"""

from __future__ import annotations

import logging
import re

from markai.models import Chunk, Document, Segment

logger = logging.getLogger(__name__)

AV_OVERLAP_SECONDS = 15.0

_PARAGRAPH_BREAK = re.compile(r"\n[ \t\r\f\v]*\n+")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])[\"')\]]*\s+")
_WORD = re.compile(r"\S+")
_NOISE_TAG = re.compile(r"[\[(](?:[^\[\]()]{0,40})[\])]")
_WHITESPACE = re.compile(r"\s+")


def approx_tokens(text: str) -> int:
    """Rough token estimate used for budgeting (four characters per token)."""
    return len(text) // 4


def _clean_segment_text(text: str) -> str:
    """Drop caption noise like ``[Music]`` or ``(laughs)`` and collapse whitespace."""

    def _keep_or_drop(match: re.Match[str]) -> str:
        inner = match.group(0)[1:-1].strip()
        # Only treat short, word-only brackets as noise tags ("[Music]", "(laughs)").
        if inner and re.fullmatch(r"[A-Za-z ,'-]{1,40}", inner) and len(inner.split()) <= 3:
            return " "
        return match.group(0)

    cleaned = _NOISE_TAG.sub(_keep_or_drop, text)
    return _WHITESPACE.sub(" ", cleaned).strip()


def _join_segments(segments: list[Segment]) -> str:
    """Join segment texts with single spaces (the tiny ``segments_to_text`` equivalent)."""
    parts = [_clean_segment_text(s.text) for s in segments]
    return " ".join(p for p in parts if p)


def _has_timing(segments: list[Segment]) -> bool:
    """A transcript loaded from a plain ``.txt`` has ``start == end == 0`` everywhere."""
    return any(s.end > 0 or s.start > 0 for s in segments)


# --- text documents ----------------------------------------------------------------------------


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) offsets of non-blank paragraphs, in document order."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        spans.append((pos, match.start()))
        pos = match.end()
    spans.append((pos, len(text)))
    return [_trim_span(text, s, e) for s, e in spans if text[s:e].strip()]


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Shrink a span so it excludes surrounding whitespace."""
    segment = text[start:end]
    lead = len(segment) - len(segment.lstrip())
    trail = len(segment) - len(segment.rstrip())
    return start + lead, end - trail


def _split_span(text: str, start: int, end: int, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    """Split ``text[start:end]`` at ``pattern`` matches, returning trimmed sub-spans."""
    spans: list[tuple[int, int]] = []
    pos = start
    for match in pattern.finditer(text, start, end):
        if match.start() > pos:
            spans.append(_trim_span(text, pos, match.start()))
        pos = match.end()
    if pos < end:
        spans.append(_trim_span(text, pos, end))
    return [s for s in spans if text[s[0] : s[1]].strip()]


def _word_spans(text: str, start: int, end: int, max_words: int) -> list[tuple[int, int]]:
    """Split a span into runs of at most ``max_words`` words."""
    words = list(_WORD.finditer(text, start, end))
    spans: list[tuple[int, int]] = []
    for i in range(0, len(words), max_words):
        group = words[i : i + max_words]
        spans.append((group[0].start(), group[-1].end()))
    return spans


def _text_units(text: str, target_words: int) -> list[tuple[int, int]]:
    """Paragraph spans, with oversized paragraphs broken into sentences and then words."""
    limit = int(target_words * 1.5)
    units: list[tuple[int, int]] = []
    for start, end in _paragraph_spans(text):
        if _count_words(text[start:end]) <= limit:
            units.append((start, end))
            continue
        for s_start, s_end in _split_span(text, start, end, _SENTENCE_BREAK):
            if _count_words(text[s_start:s_end]) <= limit:
                units.append((s_start, s_end))
            else:
                units.extend(_word_spans(text, s_start, s_end, target_words))
    return units


def _count_words(text: str) -> int:
    return len(_WORD.findall(text))


def _tail_words(text: str, n: int) -> str:
    """The last ``n`` words of ``text`` (empty when ``n`` is 0)."""
    if n <= 0:
        return ""
    words = _WORD.findall(text)
    return " ".join(words[-n:])


def _chunk_text_document(
    doc: Document, text: str, target_words: int, overlap_words: int
) -> list[Chunk]:
    units = _text_units(text, target_words)
    chunks: list[Chunk] = []
    current: list[tuple[int, int]] = []
    current_words = 0
    overlap_text = ""
    overlap_start = 0

    def _flush() -> None:
        nonlocal overlap_text, overlap_start, current, current_words
        if not current:
            return
        body = "\n\n".join(text[s:e] for s, e in current)
        chunk_text = f"{overlap_text}\n\n{body}" if overlap_text else body
        start_char = overlap_start if overlap_text else current[0][0]
        end_char = current[-1][1]
        chunks.append(
            Chunk(
                id=Chunk.make_id(doc.id, len(chunks)),
                doc_id=doc.id,
                index=len(chunks),
                text=chunk_text,
                start_char=start_char,
                end_char=end_char,
            )
        )
        overlap_text = _tail_words(body, overlap_words)
        overlap_start = max(start_char, end_char - len(overlap_text))
        current = []
        current_words = 0

    for start, end in units:
        words = _count_words(text[start:end])
        if current and current_words + words > target_words:
            _flush()
        current.append((start, end))
        current_words += words
    _flush()
    return chunks


# --- audio / video documents -------------------------------------------------------------------


def _chunk_av_document(doc: Document, window_seconds: float) -> list[Chunk]:
    segments = sorted(doc.segments, key=lambda s: (s.start, s.end))
    overlap = min(AV_OVERLAP_SECONDS, window_seconds / 2)
    step = max(window_seconds - overlap, 1e-3)

    # Character offsets of each segment inside the joined transcript text (best effort).
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for seg in segments:
        cleaned = _clean_segment_text(seg.text)
        if offsets or cleaned:
            start = cursor + (1 if cursor else 0)
        else:
            start = cursor
        end = start + len(cleaned)
        offsets.append((start, end))
        if cleaned:
            cursor = end

    first_start = segments[0].start
    last_start = segments[-1].start
    chunks: list[Chunk] = []
    window_start = first_start
    seen_windows: set[tuple[int, int]] = set()
    while window_start <= last_start:
        window_end = window_start + window_seconds
        indices = [i for i, s in enumerate(segments) if window_start <= s.start < window_end]
        if indices:
            key = (indices[0], indices[-1])
            if key not in seen_windows:
                seen_windows.add(key)
                members = [segments[i] for i in indices]
                text = _join_segments(members)
                if text:
                    chunks.append(
                        Chunk(
                            id=Chunk.make_id(doc.id, len(chunks)),
                            doc_id=doc.id,
                            index=len(chunks),
                            text=text,
                            start_char=offsets[indices[0]][0],
                            end_char=offsets[indices[-1]][1],
                            start_time=min(s.start for s in members),
                            end_time=max(max(s.end, s.start) for s in members),
                        )
                    )
        window_start += step
    return chunks


# --- public entry point ------------------------------------------------------------------------


def chunk_document(
    doc: Document,
    target_words: int = 350,
    overlap_words: int = 60,
    av_window_seconds: float = 120.0,
) -> list[Chunk]:
    """Split ``doc`` into chunks. Returns ``[]`` for a document with no usable text."""
    target_words = max(1, int(target_words))
    overlap_words = max(0, min(int(overlap_words), target_words - 1))
    if doc.segments and _has_timing(doc.segments):
        chunks = _chunk_av_document(doc, float(av_window_seconds))
    else:
        text = doc.text if doc.text.strip() else _join_segments(doc.segments)
        chunks = _chunk_text_document(doc, text, target_words, overlap_words)
    logger.debug("chunked document %s into %d chunks", doc.id, len(chunks))
    return [c for c in chunks if c.text.strip()]
