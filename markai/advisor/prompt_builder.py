"""Assembles what Claude sees: a frozen system prompt plus a per-question user turn.

The system blocks never change during a process, which is what makes prompt caching work.
Everything volatile (retrieved sources, flags, tool links, the question) goes into the user
turn, after the cache breakpoint.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from markai.knowledge.episodes import deep_link, format_timestamp
from markai.models import Citation, RetrievedChunk
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
    if business.escalation_url:
        who = business.escalation_name or "a property manager"
        lines.append(
            f"When a situation has outgrown what a chat can settle - the money at stake is "
            f"real, the facts are tangled, a deadline is close, or it is already in court - "
            f"stop advising and hand it to {who}: {business.escalation_url}. Offer it once, "
            f"in one sentence, and only when it is genuinely warranted. On a routine "
            f"question it reads as a brush-off."
        )
    if business.extra_instructions:
        lines.append(business.extra_instructions)
    lines.append("</owner_context>")
    return "\n".join(lines)


def build_system_blocks(
    system_prompt: str, business_block: str | None = None, ttl: str = "5m"
) -> list[dict[str, object]]:
    """System content blocks with the cache breakpoint on the last one.

    This prefix is the only part of a request that repeats between questions, so it is the
    only part worth caching. ``ttl`` decides how long it survives an idle gap.
    """
    blocks: list[dict[str, object]] = [{"type": "text", "text": system_prompt}]
    if business_block:
        blocks.append({"type": "text", "text": business_block})
    cache: dict[str, object] = {"type": "ephemeral"}
    if ttl != "5m":
        cache["ttl"] = ttl
    blocks[-1]["cache_control"] = cache
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


def build_facts_block(
    ordinances: list[Any],
    costs: list[Any],
    as_of: date,
) -> str:
    """The owner's own rules and prices for this question, stamped with the day.

    The date belongs here rather than in the system prompt: interpolating it up there would
    change the cached prefix on every calendar day and silently stop the cache from paying.
    """
    if not ordinances and not costs:
        return ""
    parts = [f'<authoritative_facts as_of="{as_of.isoformat()}">']
    for rule in ordinances:
        attrs = [
            f'jurisdiction="{escape_attr(rule.jurisdiction, 60)}"',
            f'topic="{escape_attr(rule.topic, 80)}"',
            f'citation="{escape_attr(rule.citation, 120)}"',
        ]
        if rule.effective_from:
            attrs.append(f'effective_from="{rule.effective_from.isoformat()}"')
        if rule.url:
            attrs.append(f'url="{escape_attr(rule.url, 300)}"')
        parts.append("<ordinance " + " ".join(attrs) + ">")
        parts.append(escape_text(rule.rule.strip()))
        if rule.notes:
            parts.append(escape_text(rule.notes.strip()))
        parts.append("</ordinance>")
    for cost in costs:
        attrs = [
            f'item="{escape_attr(cost.item, 80)}"',
            f'range="{escape_attr(cost.money(), 40)}"',
            f'unit="{escape_attr(cost.unit, 40)}"',
            f'market="{escape_attr(cost.market, 60)}"',
        ]
        if cost.as_of:
            attrs.append(f'priced="{escape_attr(cost.as_of, 20)}"')
        if cost.source:
            attrs.append(f'source="{escape_attr(cost.source, 80)}"')
        parts.append("<cost " + " ".join(attrs) + " />")
        if cost.notes:
            parts.append(escape_text(cost.notes.strip()))
    parts.append("</authoritative_facts>")
    return "\n".join(parts)


def build_portfolio_block(properties: list[Any], neighborhood: str | None = None) -> str:
    """What the landlord owns, so the answer can be about their building.

    Short by design: it rides along with every question, so the store caps the list rather
    than letting a portfolio quietly become the biggest part of each request. The
    neighborhood comes from the signup form and is worth carrying on its own: it is often
    the only thing known about someone who has not added a property yet, and half the
    ordinances in Chicagoland turn on which side of a line the building is.
    """
    if not properties and not neighborhood:
        return ""
    attrs = [f'count="{len(properties)}"']
    if neighborhood:
        attrs.append(f'neighborhood="{escape_attr(neighborhood, 80)}"')
    parts = ["<portfolio " + " ".join(attrs) + ">"]
    for item in properties:
        attrs = [f'label="{escape_attr(item.label, 80)}"']
        if item.units:
            attrs.append(f'units="{int(item.units)}"')
        if item.city:
            attrs.append(f'city="{escape_attr(item.city, 60)}"')
        parts.append("<property " + " ".join(attrs) + ">")
        if item.notes:
            parts.append(escape_text(item.notes.strip()))
        parts.append("</property>")
    parts.append("</portfolio>")
    return "\n".join(parts)


def build_user_message(
    question: str,
    retrieval,  # RetrievalResult (imported lazily to keep this module light)
    tools: list[ToolLink],
    flags: list[str],
    carried: list[RetrievedChunk] | None = None,
    facts: str = "",
    portfolio: str = "",
    today: date | None = None,
) -> str:
    """The complete user turn: the date, the owner's facts, the passages, the question."""
    chunks = _ordered_chunks(list(retrieval.chunks), carried)
    parts: list[str] = []
    # A landlord asking whether they are inside the heat season, or how many days are left
    # on a notice, needs Jay to know what day it is. It goes in the user turn and never in
    # the system prompt: a date up there would change the cached prefix every midnight.
    parts.append(f"<today>{(today or date.today()).isoformat()}</today>")
    # First after it on purpose: the facts outrank everything that follows.
    if facts:
        parts.append(facts)
    if portfolio:
        parts.append(portfolio)
    parts.append(f'<knowledge_base retrieval_status="{retrieval.coverage}" chunks="{len(chunks)}">')
    for index, rc in enumerate(chunks, start=1):
        parts.append(_source_tag(f"S{index}", rc))
        parts.append(escape_text(rc.chunk.text.strip()))
        parts.append("</source>")
    parts.append("</knowledge_base>")

    if tools:
        parts.append("<recommended_tools>")
        for tool in tools:
            line = f"- {escape_text(tool.name)} - {escape_text(tool.description)}"
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
    return deep_link(rc.document, rc.chunk.start_time)


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
