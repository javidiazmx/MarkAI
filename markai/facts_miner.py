"""Propose ordinance and cost entries out of the material already indexed.

The owner's line, and it is the right one: Jay must not learn local facts on his own, but
the facts are already sitting in the sources, said out loud in a blog post or an episode.
Pulling them out is not invention, it is reading what is there and writing it down in a
shape the answer path can use.

Three rules make that safe:

- **Nothing is invented.** Every proposal quotes the passage it came from and names the
  document. A rule with no quote is a bug, not a fact.
- **Nothing is applied.** Proposals are written to their own file. `facts.yaml` is the
  owner's, and only the owner edits it.
- **Nothing is trusted.** A passage is untrusted text from a crawl, so it is escaped before
  it reaches the model and the model is told to quote rather than paraphrase.

This costs money to run, which is why it is a command and not a background job.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from markai.advisor.prompt_builder import escape_text
from markai.models import SourceKind

logger = logging.getLogger(__name__)

# A rule worth proposing has a number in it - a deadline, a temperature, a dollar amount, a
# section number - and a word that makes it binding. Deliberately loose on the number:
# trying to enumerate the shapes missed "68 degrees", which is the heat ordinance. The
# filter is here to keep the bill down, not to decide what is true, so a passage that slips
# through costs a few tokens and comes back with nothing.
_HAS_A_NUMBER = re.compile(r"\d")
_SOUNDS_LIKE_A_RULE = re.compile(
    r"\b(must|shall|required|require|no later than|within|deadline|entitled|"
    r"prohibited|may not|cannot|has to|obligated|penalty|fine|ordinance|"
    r"illegal|liable|owes?|due)\b",
    re.IGNORECASE,
)

MAX_PASSAGE_CHARS = 1200
DEFAULT_BATCH = 12

SYSTEM = """You read passages from a Chicagoland landlording knowledge base and pull out
the rules and prices stated in them.

You are not writing rules. You are finding ones already written down and copying them out.

For each passage, return zero or more entries. Zero is the normal answer: most passages are
commentary, not rules. Only return an entry when the passage states something checkable a
landlord would act on.

Every entry must carry `quote`: the exact sentence from the passage that says it, copied
character for character. If you cannot quote it, you cannot propose it.

Never merge two passages, never add a number that is not in the text, never convert or
round one, and never state a rule you know from elsewhere. If the passage says "45 days",
the entry says 45 days; if it says "about a month and a half", the entry says that."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["ordinance", "cost"]},
                    "jurisdiction": {"type": "string"},
                    "topic": {"type": "string"},
                    "rule": {"type": "string"},
                    "citation": {"type": "string"},
                    "low": {"type": "number"},
                    "high": {"type": "number"},
                    "unit": {"type": "string"},
                    "quote": {"type": "string"},
                    "passage": {"type": "integer"},
                },
                "required": ["kind", "topic", "rule", "quote", "passage"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["entries"],
    "additionalProperties": False,
}


@dataclass
class Proposal:
    """One candidate entry, with the passage it came out of."""

    kind: str
    topic: str
    rule: str
    quote: str
    source_title: str
    source_url: str | None = None
    jurisdiction: str = ""
    citation: str = ""
    low: float | None = None
    high: float | None = None
    unit: str = ""
    verified: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = {
            "kind": self.kind,
            "topic": self.topic,
            "rule": self.rule,
            "quote": self.quote,
            "source": self.source_title,
            "url": self.source_url,
            "verified_quote": self.verified,
        }
        if self.kind == "ordinance":
            data["jurisdiction"] = self.jurisdiction
            data["citation"] = self.citation
        else:
            data["low"] = self.low
            data["high"] = self.high
            data["unit"] = self.unit
        return data


@dataclass
class MinerReport:
    """What one run looked at and what it found."""

    passages_seen: int = 0
    passages_read: int = 0
    proposals: list[Proposal] = field(default_factory=list)
    dropped_unquoted: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    read_chunk_ids: list[str] = field(default_factory=list)
    batches_failed: int = 0

    @property
    def cost_usd(self) -> float:
        """Opus 5 list price. Close enough to tell the owner what a run cost."""
        return (
            self.usage.get("input_tokens", 0) / 1_000_000 * 5.0
            + self.usage.get("output_tokens", 0) / 1_000_000 * 25.0
        )


def worth_reading(text: str) -> bool:
    """Whether a passage could hold a rule, before spending a token on it."""
    return bool(_HAS_A_NUMBER.search(text) and _SOUNDS_LIKE_A_RULE.search(text))


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def quote_is_real(quote: str, passage: str) -> bool:
    """The quote has to be in the passage. This is the whole safety property.

    Whitespace is normalised because a model reflows lines, but nothing else is: a
    paraphrase, a merged sentence, or a number that drifted all fail here and the entry is
    thrown away rather than shown to the owner as something their sources said.
    """
    needle = _normalise(quote)
    return len(needle) > 15 and needle in _normalise(passage)


def build_batch_prompt(passages: list[tuple[int, str, str]]) -> str:
    """One user turn holding several passages, each numbered so entries can point back."""
    parts = ["<passages>"]
    for index, title, text in passages:
        parts.append(f'<passage index="{index}" source="{escape_text(title)}">')
        parts.append(escape_text(text[:MAX_PASSAGE_CHARS]))
        parts.append("</passage>")
    parts.append("</passages>")
    parts.append("Return the entries these passages state. Most passages yield none; that is fine.")
    return "\n".join(parts)


def _proposals_from(payload: dict, batch: list[tuple[int, str, str]], report: MinerReport) -> None:
    by_index = {index: (title, text) for index, title, text in batch}
    for raw in payload.get("entries", []):
        passage_index = raw.get("passage")
        found = by_index.get(passage_index)
        if found is None:
            report.dropped_unquoted += 1
            continue
        label, text = found
        title, _, url = label.partition("\u241f")
        quote = str(raw.get("quote", ""))
        if not quote_is_real(quote, text):
            # The one check that matters. A rule the sources do not actually say is worse
            # than no rule, and it would arrive wearing their name.
            report.dropped_unquoted += 1
            continue
        kind = "cost" if raw.get("kind") == "cost" else "ordinance"
        report.proposals.append(
            Proposal(
                kind=kind,
                topic=str(raw.get("topic", ""))[:120],
                rule=str(raw.get("rule", ""))[:600],
                quote=quote[:600],
                source_title=title,
                source_url=url or None,
                jurisdiction=str(raw.get("jurisdiction", ""))[:80],
                citation=str(raw.get("citation", ""))[:160],
                low=raw.get("low"),
                high=raw.get("high"),
                unit=str(raw.get("unit", ""))[:40],
            )
        )


def candidates_in(
    store: Any,
    kinds: tuple[SourceKind, ...] | None = None,
    already_read: set[str] | None = None,
) -> tuple[list[tuple[str, str, str]], int]:
    """Every passage worth a model call, and how many were looked at to find them.

    Returned as (chunk id, document title, text). The whole corpus is walked - the owner
    asked for all of it - but only the passages that could hold a rule are spent on: a
    number and a word like "must" or "within". The rest is commentary about a
    neighbourhood, and reading it costs money for nothing.
    """
    documents = {doc.id: doc for doc in store.list_documents()}
    done = already_read or set()
    seen = 0
    found: list[tuple[str, str, str]] = []
    for chunk in store.all_chunks():
        doc = documents.get(chunk.doc_id)
        if doc is None or (kinds and doc.kind not in kinds):
            continue
        seen += 1
        if chunk.id in done or not worth_reading(chunk.text):
            continue
        # The title carries the link so an accepted rule can point at the page it came
        # from, which is the difference between a citation and a claim.
        label = f"{doc.title}\u241f{doc.link or ''}"
        found.append((chunk.id, label, chunk.text))
    return found, seen


def estimate(candidates: list[tuple[str, str, str]], batch_size: int = DEFAULT_BATCH) -> dict:
    """Roughly what a run will cost, before the owner spends it.

    Deliberately generous: a surprise on the bill is worse than a surprise that it came in
    under. Characters over four for tokens, plus a fixed allowance for the reply.
    """
    chars = sum(len(text[:MAX_PASSAGE_CHARS]) for _, _, text in candidates)
    batches = max(1, -(-len(candidates) // batch_size)) if candidates else 0
    input_tokens = int(chars / 4) + batches * 400  # the system prompt rides on each batch
    output_tokens = batches * 700
    return {
        "passages": len(candidates),
        "batches": batches,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usd": input_tokens / 1_000_000 * 5.0 + output_tokens / 1_000_000 * 25.0,
    }


def as_yaml_entry(proposal: Proposal, entry_id: str) -> str:
    """One proposal as the lines that go into facts.yaml, indentation and all.

    Written as text rather than dumped from a parsed document so the owner's file keeps its
    comments, which are half of what makes it editable by hand.
    """

    def quoted(value: str) -> str:
        return json.dumps(str(value))  # JSON strings are valid YAML and escape themselves

    lines = [f"  - id: {entry_id}"]
    if proposal.kind == "ordinance":
        lines.append(f"    jurisdiction: {quoted(proposal.jurisdiction or 'Chicago')}")
        lines.append(f"    topic: {quoted(proposal.topic)}")
        lines.append(f"    rule: {quoted(proposal.rule)}")
        lines.append(f"    citation: {quoted(proposal.citation or proposal.source_title)}")
        if proposal.source_url:
            lines.append(f"    url: {quoted(proposal.source_url)}")
        lines.append(f"    notes: {quoted('From: ' + proposal.quote)}")
    else:
        lines.append(f"    item: {quoted(proposal.topic)}")
        lines.append(f"    low: {proposal.low or 0}")
        lines.append(f"    high: {proposal.high or proposal.low or 0}")
        lines.append(f"    unit: {quoted(proposal.unit or 'per job')}")
        lines.append(f"    source: {quoted(proposal.source_title)}")
        lines.append(f"    notes: {quoted('From: ' + proposal.quote)}")
    return "\n".join(lines)


class CannotInsert(ValueError):
    """The file is shaped in a way this cannot edit safely, with a reason to show."""


def insert_into_facts(body: str, section: str, entry: str) -> str:
    """Put an entry under ``ordinances:`` or ``costs:``, creating the key if it is missing.

    Textual on purpose: a round trip through the YAML parser would drop every comment in
    the owner's file, and those comments are the instructions they wrote for themselves.

    The care here is about not writing a second copy of the key. YAML keeps the last of two
    identical keys and discards the first without complaining, so an append that looks fine
    can quietly delete every rule the owner already wrote.
    """
    lines = body.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(f"{section}:"):
            continue
        rest = stripped[len(section) + 1 :].strip()
        if not rest:  # "ordinances:" with entries under it, or nothing yet
            lines[index + 1 : index + 1] = entry.splitlines()
            return "\n".join(lines) + "\n"
        if rest == "[]":  # "ordinances: []" - make it a block and put this inside
            lines[index] = f"{section}:"
            lines[index + 1 : index + 1] = entry.splitlines()
            return "\n".join(lines) + "\n"
        raise CannotInsert(
            f"{section} is written inline as `{stripped[:40]}`. Put it on its own lines "
            f"first, then this can add to it without losing what is there."
        )
    tail = "" if body.endswith("\n") or not body else "\n"
    return f"{body}{tail}{section}:\n{entry}\n"


def mine(
    store: Any,
    client: Any,
    model: str,
    limit: int = 0,
    batch_size: int = DEFAULT_BATCH,
    kinds: tuple[SourceKind, ...] | None = None,
    on_progress: Any = None,
    already_read: set[str] | None = None,
) -> MinerReport:
    """Read the corpus and propose entries. ``client`` is the Anthropic client.

    ``limit`` of 0 means everything, which is the point: a fact the sources state is no use
    if the passage holding it was never read. ``already_read`` lets a second run pick up
    where an interrupted one stopped, so a corpus this size does not have to survive a run
    in one piece.
    """
    report = MinerReport()
    found, seen = candidates_in(store, kinds=kinds, already_read=already_read)
    report.passages_seen = seen
    if limit:
        found = found[:limit]
    report.passages_read = len(found)

    for start in range(0, len(found), batch_size):
        window = found[start : start + batch_size]
        batch = [(index, title, text) for index, (_, title, text) in enumerate(window)]
        if on_progress:
            on_progress(start + len(window), len(found))
        try:
            response = client.messages.create(
                model=model,
                max_tokens=8000,
                system=SYSTEM,
                messages=[{"role": "user", "content": build_batch_prompt(batch)}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": SCHEMA},
                },
            )
        except Exception as exc:  # one bad batch must not lose the rest of the run
            logger.warning("a batch failed: %s", exc)
            report.batches_failed += 1
            continue
        for key in ("input_tokens", "output_tokens"):
            report.usage[key] = report.usage.get(key, 0) + getattr(response.usage, key, 0)
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        try:
            payload = json.loads(text)
        except ValueError:
            logger.warning("a batch came back as something other than JSON")
            report.batches_failed += 1
            continue
        _proposals_from(payload, batch, report)
        # Only after the batch came back: an interrupted run re-reads its last batch
        # rather than skipping passages it never actually looked at.
        report.read_chunk_ids.extend(chunk_id for chunk_id, _, _ in window)

    return report
