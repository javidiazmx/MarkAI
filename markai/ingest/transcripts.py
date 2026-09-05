"""Transcript file parsing and text helpers shared by the YouTube and podcast ingesters.

Supports ``.srt`` (via the ``srt`` package), ``.vtt`` (via ``webvtt-py``), ``.json`` (the
``youtube-transcript-api`` ``to_raw_data()`` shape or ``{"segments": [...]}``), and plain
``.txt`` (paragraphs, no timing). Everything here is pure: no network, no global state.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import srt
import webvtt

from markai.models import IngestError, Segment

logger = logging.getLogger(__name__)

TRANSCRIPT_SUFFIXES: tuple[str, ...] = (".srt", ".vtt", ".txt", ".json")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_BRACKET_NOISE_RE = re.compile(r"\[[^\]\n]{0,60}\]")
_PAREN_NOISE_RE = re.compile(
    r"\((?:laughs?|laughing|laughter|chuckles?|applause|music|inaudible|crosstalk|"
    r"unintelligible|sighs?|coughs?|silence|pause|background noise|cheering|"
    r"indistinct|foreign|speaking [a-z ]+)[^)\n]{0,40}\)",
    re.IGNORECASE,
)
_MUSIC_NOTE_RE = re.compile(r"[♪♫]+")
_HTML_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&nbsp;": " ",
}


def _clean_caption_text(text: str) -> str:
    """Strip markup tags and entities from a caption cue and collapse whitespace."""
    text = _TAG_RE.sub(" ", text)
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    return _WS_RE.sub(" ", text).strip()


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_srt(text: str) -> list[Segment]:
    segments: list[Segment] = []
    for sub in srt.parse(text, ignore_errors=True):
        content = _clean_caption_text(sub.content)
        if not content:
            continue
        segments.append(
            Segment(start=sub.start.total_seconds(), end=sub.end.total_seconds(), text=content)
        )
    return segments


def _parse_vtt(text: str) -> list[Segment]:
    segments: list[Segment] = []
    for caption in webvtt.from_string(text):
        content = _clean_caption_text(caption.text)
        if not content:
            continue
        segments.append(
            Segment(
                start=float(caption.start_in_seconds),
                end=float(caption.end_in_seconds),
                text=content,
            )
        )
    return segments


def _parse_json(text: str, path: Path) -> list[Segment]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IngestError(f"{path.name} is not valid JSON: {exc}") from exc
    if isinstance(data, dict):
        items = data.get("segments")
        if items is None:
            items = data.get("snippets")
        if items is None:
            raise IngestError(
                f"{path.name}: expected a JSON list or an object with a 'segments' list",
                hint="Export the transcript as a list of {text, start, duration} objects.",
            )
    else:
        items = data
    if not isinstance(items, list):
        raise IngestError(f"{path.name}: transcript segments must be a JSON list")
    segments: list[Segment] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = _clean_caption_text(str(item.get("text", "")))
        if not content:
            continue
        start = _as_float(item.get("start"))
        if "end" in item and item["end"] is not None:
            end = _as_float(item.get("end"), start)
        else:
            end = start + _as_float(item.get("duration"))
        segments.append(Segment(start=start, end=max(start, end), text=content))
    return segments


def _parse_txt(text: str) -> list[Segment]:
    paragraphs = re.split(r"\n\s*\n", text)
    segments: list[Segment] = []
    for para in paragraphs:
        content = _WS_RE.sub(" ", para).strip()
        if content:
            segments.append(Segment(start=0.0, end=0.0, text=content))
    return segments


def parse_transcript_file(path: Path) -> list[Segment]:
    """Parse a transcript file into timed segments.

    ``.txt`` transcripts have no timing: every segment gets ``start == end == 0.0``.
    An empty file yields ``[]``. An unsupported suffix raises ``IngestError``.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in TRANSCRIPT_SUFFIXES:
        raise IngestError(
            f"Unsupported transcript format {suffix or '(no extension)'} for {path.name}",
            hint="Supported transcript formats: .srt, .vtt, .txt, .json",
        )
    if not path.exists():
        raise IngestError(f"Transcript file not found: {path}")
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return []
    try:
        if suffix == ".srt":
            return _parse_srt(text)
        if suffix == ".vtt":
            return _parse_vtt(text)
        if suffix == ".json":
            return _parse_json(text, path)
        return _parse_txt(text)
    except IngestError:
        raise
    except Exception as exc:  # malformed caption files
        raise IngestError(f"Could not parse {path.name}: {exc}") from exc


def _strip_noise(text: str) -> str:
    text = _BRACKET_NOISE_RE.sub(" ", text)
    text = _PAREN_NOISE_RE.sub(" ", text)
    text = _MUSIC_NOTE_RE.sub(" ", text)
    return text


def segments_to_text(segments: list[Segment]) -> str:
    """Join segment texts with spaces, dropping noise tags such as ``[Music]`` or ``(laughs)``."""
    joined = " ".join(seg.text for seg in segments if seg.text)
    return _WS_RE.sub(" ", _strip_noise(joined)).strip()


def format_timestamp(seconds: float) -> str:
    """Render seconds as ``m:ss`` under an hour, ``h:mm:ss`` otherwise."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _has_timing(segments: list[Segment]) -> bool:
    return any(seg.start or seg.end for seg in segments)


def merge_segments(segments: list[Segment], window_seconds: float = 30.0) -> list[Segment]:
    """Coalesce tiny caption snippets into roughly ``window_seconds``-long segments.

    Order is preserved and no empty segments are emitted. Untimed segments (all zeros, as
    produced from ``.txt`` files) are returned as-is apart from dropping empties.
    """
    cleaned = [seg for seg in segments if seg.text and seg.text.strip()]
    if not cleaned:
        return []
    if not _has_timing(cleaned):
        return [Segment(start=0.0, end=0.0, text=seg.text.strip()) for seg in cleaned]

    merged: list[Segment] = []
    parts: list[str] = []
    group_start = cleaned[0].start
    group_end = cleaned[0].end

    def flush() -> None:
        text = _WS_RE.sub(" ", " ".join(parts)).strip()
        if text:
            merged.append(Segment(start=group_start, end=max(group_start, group_end), text=text))

    for seg in cleaned:
        if parts and (max(seg.end, seg.start) - group_start) >= window_seconds:
            flush()
            parts = []
            group_start = seg.start
        parts.append(seg.text.strip())
        group_end = max(seg.end, seg.start)
    flush()
    return merged


def slugify(text: str) -> str:
    """Lowercase ``[a-z0-9]+`` runs joined by ``-``."""
    return "-".join(re.findall(r"[a-z0-9]+", (text or "").lower()))
