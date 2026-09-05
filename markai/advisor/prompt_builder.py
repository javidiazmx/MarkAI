"""Assembles what Claude sees: a frozen system prompt plus a per-question user turn.

The system blocks never change during a process, which is what makes prompt caching work.
Everything volatile (retrieved sources, flags, tool links, the question) goes into the user
turn, after the cache breakpoint.
"""

from __future__ import annotations

import re
from pathlib import Path

from markai.models import Citation, RetrievedChunk, SourceKind
from markai.sources.manifest import BusinessProfile, ToolLink

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MARKER_RE = re.compile(r"\[S(\d+)\]")


_CITING_BLOCK = re.compile(r"<!-- CITING:START -->.*?<!-- CITING:END -->\n*", re.DOTALL)
_NOCITE_BLOCK = re.compile(r"<!-- NOCITE:START -->.*?<!-- NOCITE:END -->\n*", re.DOTALL)
# The kept half still carries its own comment fences; they are not instructions.
_BLOCK_MARKER = re.compile(r"<!-- (?:CITING|NOCITE):(?:START|END) -->\n*")


def load_system_prompt(path: Path, show_citations: bool = True) -> str:
    """Read Mark's system prompt from disk, keeping the citation half that applies.

    Two variants, one file. Whichever is chosen is fixed for the life of the process, so the
    prompt stays byte-identical between requests and the cache still holds - the thing that
    would break it is interpolating something that changes, like a date or a session id.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"System prompt not found at {path}. It ships with the repo at "
            "prompts/mark_system_prompt.md."
        )
    text = path.read_text(encoding="utf-8")
    drop = _NOCITE_BLOCK if show_citations else _CITING_BLOCK
    text = drop.sub("", text)
    return _BLOCK_MARKER.sub("", text).strip()


def build_business_block(business: BusinessProfile | None) -> str | None:
    """Render the owner's business context, or ``None`` when they supplied none."""
    if business is None or business.is_empty():
        return None
    lines = ["<owner_context>"]
    if business.name:
        lines.append(f"You were built by {business.name}.")
    if business.services:
        lines.append(f"What they do: {business.services}")
    if business.service_area:
        lines.append(f"Service area: {business.service_area}")
    contact = business.contact_url or business.contact_email
    if contact:
        lines.append(
            f"When someone needs hands-on help beyond what you can answer, point them to {contact}."
        )
    if business.never_say:
        lines.append("Never say or promise any of the following:")
        lines.extend(f"- {item}" for item in business.never_say)
    if business.extra_instructions:
        lines.append(business.extra_instructions)
    lines.append("</owner_context>")
    return "\n".join(lines)


def build_system_blocks(
    system_prompt: str, business_block: str | None = None
) -> list[dict[str, object]]:
    """System content blocks with the cache breakpoint on the last one."""
    blocks: list[dict[str, object]] = [{"type": "text", "text": system_prompt}]
    if business_block:
        blocks.append({"type": "text", "text": business_block})
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def escape_text(text: str) -> str:
    """Escape markup so source text can never forge a tag."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(text: str, limit: int = 200) -> str:
    """Escape and flatten a value for use inside a tag attribute."""
    cleaned = _CONTROL_RE.sub("", str(text)).replace("\n", " ").replace("\r", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return escape_text(cleaned).replace('"', "&quot;")


def format_timestamp(seconds: float) -> str:
    """``m:ss`` under an hour, ``h:mm:ss`` above it."""
    total = max(int(seconds), 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _ordered_chunks(
    retrieval_chunks: list[RetrievedChunk], carried: list[RetrievedChunk] | None
) -> list[RetrievedChunk]:
    seen = {rc.chunk.id for rc in retrieval_chunks}
    extra = [rc for rc in (carried or []) if rc.chunk.id not in seen]
    return list(retrieval_chunks) + extra


def _source_tag(marker: str, rc: RetrievedChunk) -> str:
    doc = rc.document
    attrs = [f'id="{marker}"', f'kind="{escape_attr(doc.kind.value)}"']
    attrs.append(f'title="{escape_attr(doc.title)}"')
    if doc.episode:
        attrs.append(f'episode="{escape_attr(doc.episode)}"')
    if doc.channel:
        attrs.append(f'channel="{escape_attr(doc.channel)}"')
    if doc.published_at:
        attrs.append(f'date="{escape_attr(doc.published_at, 32)}"')
    if rc.chunk.start_time is not None:
        attrs.append(f'timestamp="{escape_attr(format_timestamp(rc.chunk.start_time), 16)}"')
    url = doc.link or (doc.locator if doc.locator.startswith(("http://", "https://")) else None)
    if url:
        attrs.append(f'url="{escape_attr(url, 400)}"')
    return "<source " + " ".join(attrs) + ">"


def build_user_message(
    question: str,
    retrieval,  # RetrievalResult (imported lazily to keep this module light)
    tools: list[ToolLink],
    flags: list[str],
    carried: list[RetrievedChunk] | None = None,
) -> str:
    """The complete user turn: knowledge base, tool links, flags, and the question."""
    chunks = _ordered_chunks(list(retrieval.chunks), carried)
    parts: list[str] = [
        f'<knowledge_base retrieval_status="{retrieval.coverage}" chunks="{len(chunks)}">'
    ]
    for index, rc in enumerate(chunks, start=1):
        parts.append(_source_tag(f"S{index}", rc))
        parts.append(escape_text(rc.chunk.text.strip()))
        parts.append("</source>")
    parts.append("</knowledge_base>")

    if tools:
        parts.append("<recommended_tools>")
        for tool in tools:
            line = f"- {escape_text(tool.name)} — {escape_text(tool.description)}"
            if tool.url:
                line += f" ({escape_text(tool.url)})"
            if tool.when_to_recommend:
                line += f" [when: {escape_text(tool.when_to_recommend)}]"
            parts.append(line)
        parts.append("</recommended_tools>")

    if flags:
        parts.append("<context_flags>" + ", ".join(sorted(flags)) + "</context_flags>")

    parts.append("<question>")
    parts.append(escape_text(question.strip()))
    parts.append("</question>")
    return "\n".join(parts)


def _citation_url(rc: RetrievedChunk) -> str | None:
    doc = rc.document
    url = doc.link or doc.locator
    if not url or not url.startswith(("http://", "https://")):
        return None
    if doc.kind == SourceKind.YOUTUBE and rc.chunk.start_time is not None:
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}t={int(rc.chunk.start_time)}s"
    return url


def strip_all_markers(answer_text: str) -> str:
    """Remove every ``[S#]`` marker and the space it leaves in front of punctuation.

    A backstop for when citations are turned off: the prompt already says not to write them,
    but one slipping through would look like a bug to a landlord reading the answer.
    """
    text = _MARKER_RE.sub("", answer_text)
    text = re.sub(r" +([.,;:!?)])", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def build_citations(
    retrieval,
    answer_text: str,
    carried: list[RetrievedChunk] | None = None,
) -> list[Citation]:
    """Footnotes for the ``[S#]`` markers Mark actually used, in order of appearance."""
    chunks = _ordered_chunks(list(retrieval.chunks), carried)
    by_marker = {f"S{i}": rc for i, rc in enumerate(chunks, start=1)}

    citations: list[Citation] = []
    seen: set[str] = set()
    for match in _MARKER_RE.finditer(answer_text):
        marker = f"S{match.group(1)}"
        if marker in seen or marker not in by_marker:
            continue
        seen.add(marker)
        rc = by_marker[marker]
        doc = rc.document
        citations.append(
            Citation(
                marker=marker,
                kind=doc.kind,
                title=doc.title,
                url=_citation_url(rc),
                episode=doc.episode,
                channel=doc.channel,
                timestamp=(
                    format_timestamp(rc.chunk.start_time)
                    if rc.chunk.start_time is not None
                    else None
                ),
                published_at=doc.published_at,
                snippet=rc.chunk.text.strip()[:160],
            )
        )
    return citations


def strip_unused_markers(answer_text: str, valid_markers: set[str]) -> str:
    """Delete ``[S#]`` markers that point at sources which were never supplied."""

    def replace(match: re.Match[str]) -> str:
        marker = f"S{match.group(1)}"
        return match.group(0) if marker in valid_markers else ""

    cleaned = _MARKER_RE.sub(replace, answer_text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return re.sub(r" +([.,;:!?])", r"\1", cleaned)
