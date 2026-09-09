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
    support: int = 1  # how many passages quoted this same sentence

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


# Priced off the first full run over the real corpus, which is the only measurement there
# is: 2640 passages, 220 requests, $12.36. The reply was about 115 output tokens per
# passage - the model found roughly one entry every second passage, and an entry carries a
# quote - against a flat 700 per *request* this used to assume, which is why the estimate
# came in at $8.21 and the bill did not. Output scales with passages, not with requests.
CHARS_PER_TOKEN = 3.6  # escaped markup runs denser than prose
INPUT_TOKENS_PER_BATCH = 500  # the system prompt rides on every request
OUTPUT_TOKENS_PER_PASSAGE = 120
OUTPUT_TOKENS_PER_BATCH = 200  # the JSON envelope, even on a batch that finds nothing
CEILING = 1.25  # what to quote as the worst case, since the average is only an average

IN_USD_PER_MTOK = 5.0
OUT_USD_PER_MTOK = 25.0


def estimate(candidates: list[tuple[str, str, str]], batch_size: int = DEFAULT_BATCH) -> dict:
    """What a run will cost, before the owner spends it.

    Two numbers, because one number pretending to be exact is what went wrong the first
    time: ``usd`` is the expected bill and ``usd_max`` the figure to decide against. A run
    that finds more rules than average costs more, and the owner would rather hear the
    ceiling now than read it on the invoice.
    """
    chars = sum(len(text[:MAX_PASSAGE_CHARS]) for _, _, text in candidates)
    batches = max(1, -(-len(candidates) // batch_size)) if candidates else 0
    input_tokens = int(chars / CHARS_PER_TOKEN) + batches * INPUT_TOKENS_PER_BATCH
    output_tokens = len(candidates) * OUTPUT_TOKENS_PER_PASSAGE + batches * OUTPUT_TOKENS_PER_BATCH
    usd = input_tokens / 1_000_000 * IN_USD_PER_MTOK + output_tokens / 1_000_000 * OUT_USD_PER_MTOK
    return {
        "passages": len(candidates),
        "batches": batches,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usd": usd,
        "usd_max": usd * CEILING,
    }


# --- mining without spending anything -----------------------------------------------------

# The model reads a passage and writes the rule out in its own words. That costs money, and
# it is better. But most of the value is in one sentence somebody already wrote - "heat must
# reach 68 degrees during the day" - and pulling that sentence out is pattern matching, not
# language understanding. So there is a free path: no API key, no call, no bill, the sentence
# proposed verbatim as its own rule. The owner still decides, and the quote is the sentence,
# so it cannot be wrong about what the source said.
#
# The filter is stricter here than for the paid path. The model can look at a weak passage
# and correctly return nothing; a regex cannot, so anything it is unsure about it leaves
# alone. Fewer proposals, all of them at least somebody's actual sentence.

_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_BINDING = re.compile(
    r"\b(must|shall|is required|are required|required to|may not|cannot|can not|"
    r"no later than|within \d|has to|have to|is entitled|are entitled|is prohibited|"
    r"are prohibited|is liable|are liable|is due|are due)\b",
    re.IGNORECASE,
)
_MONEY_RANGE = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s?(k\b)?(?:\s*(?:to|-|–|and)\s*\$?\s?([\d,]+(?:\.\d+)?)\s?(k\b)?)?",
    re.IGNORECASE,
)
_JURISDICTIONS = (
    "Chicago",
    "Cook County",
    "Evanston",
    "Oak Park",
    "Berwyn",
    "Cicero",
    "Skokie",
    "Naperville",
    "Aurora",
    "Joliet",
    "Illinois",
)

MIN_SENTENCE_CHARS = 40
MAX_SENTENCE_CHARS = 320
MAX_TOPIC_WORDS = 4

# Words that say nothing about what a rule is about, on top of the shared stop list.
_NOT_A_TOPIC = frozenset(
    """
    landlord landlords tenant tenants property properties unit units building buildings
    day days month months year years time must shall required require within also going
    really thing things lot lots way ways people going make made take taken give given
    """.split()
)


def sentences_worth_proposing(text: str) -> list[str]:
    """The sentences in a passage that state a rule outright, verbatim.

    A number and a binding word have to be in the *sentence*, not merely somewhere in the
    passage: "we paid $8,000" three lines above "you must give notice" is two facts, and
    joining them would invent a third.
    """
    found: list[str] = []
    for raw in _SENTENCE.findall(text or ""):
        sentence = re.sub(r"\s+", " ", raw).strip()
        if not (MIN_SENTENCE_CHARS <= len(sentence) <= MAX_SENTENCE_CHARS):
            continue
        if _HAS_A_NUMBER.search(sentence) and _BINDING.search(sentence):
            found.append(sentence)
    return found


def topic_from(sentence: str) -> str:
    """A few words naming what the sentence is about, in the order they appear."""
    from markai.sources.facts import terms_in

    keep = terms_in(sentence) - _NOT_A_TOPIC
    words = [word for word in re.findall(r"[a-z0-9áéíóúñü]+", sentence.lower()) if word in keep]
    seen: list[str] = []
    for word in words:
        if word not in seen:
            seen.append(word)
    return " ".join(seen[:MAX_TOPIC_WORDS]) or "rule"


def jurisdiction_from(sentence: str, passage: str = "") -> str:
    """Which government the sentence is about, named only when it says so.

    The sentence first, then the passage around it. Nothing is assumed: an unsourced
    "Chicago" on a rule that is actually state law is exactly the mistake that hurts.
    """
    for where in _JURISDICTIONS:
        if re.search(rf"\b{re.escape(where)}\b", sentence, re.IGNORECASE):
            return where
    for where in _JURISDICTIONS:
        if re.search(rf"\b{re.escape(where)}\b", passage, re.IGNORECASE):
            return where
    return ""


def price_in(sentence: str) -> tuple[float | None, float | None]:
    """The dollar figure or range a sentence names, as (low, high)."""

    def value(number: str, thousands: str | None) -> float:
        amount = float(number.replace(",", ""))
        return amount * 1000 if thousands else amount

    match = _MONEY_RANGE.search(sentence)
    if not match:
        return None, None
    low = value(match.group(1), match.group(2))
    high = value(match.group(3), match.group(4)) if match.group(3) else low
    return (low, high) if high >= low else (high, low)


def proposal_from_sentence(sentence: str, label: str, passage: str) -> Proposal:
    """One sentence as a proposal that quotes itself."""
    title, _, url = label.partition("\u241f")
    low, high = price_in(sentence)
    priced = low is not None and "$" in sentence
    return Proposal(
        kind="cost" if priced else "ordinance",
        topic=topic_from(sentence),
        rule=sentence[:600],
        quote=sentence[:600],
        source_title=title,
        source_url=url or None,
        jurisdiction=jurisdiction_from(sentence, passage),
        citation=title,
        low=low if priced else None,
        high=high if priced else None,
        unit="as stated" if priced else "",
    )


def mine_locally(
    store: Any,
    limit: int = 0,
    kinds: tuple[SourceKind, ...] | None = None,
    already_read: set[str] | None = None,
    on_progress: Any = None,
) -> MinerReport:
    """Propose entries out of the sources with no API call and no cost.

    Same output as :func:`mine` - proposals into the same review queue - reached by reading
    rather than by asking. Worth running first on new material: what it finds is free, and
    what it misses is still there for a paid run later.
    """
    report = MinerReport()
    found, seen = candidates_in(store, kinds=kinds, already_read=already_read)
    report.passages_seen = seen
    if limit:
        found = found[:limit]
    report.passages_read = len(found)

    for index, (chunk_id, label, text) in enumerate(found, start=1):
        for sentence in sentences_worth_proposing(text[:MAX_PASSAGE_CHARS]):
            proposal = proposal_from_sentence(sentence, label, text)
            if not quote_is_real(proposal.quote, text):
                # Cannot happen while the quote is a slice of the passage, and checked
                # anyway: this is the one property the whole thing rests on.
                report.dropped_unquoted += 1
                continue
            report.proposals.append(proposal)
        report.read_chunk_ids.append(chunk_id)
        if on_progress and index % 100 == 0:
            on_progress(index, len(found))
    if on_progress:
        on_progress(len(found), len(found))
    return report


# --- reading a big pile of proposals ------------------------------------------------------

# A full run over the corpus came back with 1305 proposals, and a command that shows those
# one at a time is a command nobody finishes. Most of the pile is repetition: the same
# sentence about the heat ordinance sits in four blog posts and gets quoted four times. So
# identical quotes collapse into one entry that names how many sources said it, and what is
# left is ordered by topic, so a landlord's whole "security deposit" pile arrives together
# and can be dealt with in one decision instead of eleven.

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def fingerprint(raw: dict) -> str:
    """What makes two proposals the same proposal.

    The quote, normalised. Two passages that quote the same sentence are one rule, whoever
    republished it; two passages that say the same thing in different words are not, and
    guessing that they are would merge rules that differ in a number.
    """
    quote = _normalise(raw.get("quote", ""))
    return f"{raw.get('kind', 'ordinance')}|{quote or _normalise(raw.get('rule', ''))}"


# A word that turns up in nearly every topic is not a subject, it is the vocabulary of the
# whole corpus. "Tenant" or "chicago" would collect everything into one pile, so a term is
# only allowed to name a subject if it stays under this share of the topics.
MAX_SUBJECT_SHARE = 0.4


def topic_terms(raw: dict) -> set[str]:
    from markai.sources.facts import terms_in

    return terms_in(str(raw.get("topic", "")))


def topic_key(raw: dict, counts: dict[str, int] | None = None, ceiling: int = 0) -> str:
    """The subject a proposal belongs to, for ordering and for "skip this topic".

    The word its topic shares with the most other proposals, which is what makes a pile a
    pile: "deposit interest" and "deposit return" both land under ``deposit``. Without the
    counts to compare against it falls back to the topic's own words.
    """
    words = topic_terms(raw)
    if not words:
        return _normalise(raw.get("topic", "")) or "other"
    if not counts:
        return " ".join(sorted(words))
    usable = [word for word in words if counts.get(word, 0) <= ceiling] or sorted(words)
    return sorted(usable, key=lambda word: (-counts.get(word, 0), word))[0]


@dataclass
class ProposalGroup:
    """One rule, the passages that stated it, and where it sits in the review queue."""

    lead: dict
    raws: list[dict] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)
    topic: str = ""
    known: bool = False

    @property
    def support(self) -> int:
        """How many of the owner's own sources state this."""
        return len(self.raws)

    @property
    def kind(self) -> str:
        return str(self.lead.get("kind", "ordinance"))

    def sources(self) -> list[str]:
        seen: list[str] = []
        for raw in self.raws:
            title = str(raw.get("source", ""))
            if title and title not in seen:
                seen.append(title)
        return seen


def _lead_of(raws: list[dict]) -> dict:
    """The one to show. A cited, fully written entry beats a terse one, ties broken by name."""
    return max(
        raws,
        key=lambda raw: (
            bool(str(raw.get("citation", "")).strip()),
            min(len(str(raw.get("rule", ""))), 400),
            str(raw.get("source", "")),
        ),
    )


def group_proposals(
    raws: list[dict], known_terms: frozenset[str] = frozenset()
) -> list[ProposalGroup]:
    """Collapse repeats, mark what the owner already has a rule about, and order the queue.

    ``known_terms`` is the vocabulary of ``facts.yaml``. A proposal whose topic is already
    covered there goes to the back and is labelled, because the owner has already made that
    decision and a second rule on the same subject is usually the one they do not want.
    """
    from markai.sources.facts import terms_in

    order: list[str] = []
    by_print: dict[str, ProposalGroup] = {}
    for index, raw in enumerate(raws):
        key = fingerprint(raw)
        group = by_print.get(key)
        if group is None:
            group = ProposalGroup(lead=raw)
            by_print[key] = group
            order.append(key)
        group.raws.append(raw)
        group.indices.append(index)

    groups = [by_print[key] for key in order]
    for group in groups:
        group.lead = _lead_of(group.raws)
        group.known = bool(known_terms and terms_in(str(group.lead.get("topic", ""))) & known_terms)

    counts: dict[str, int] = {}
    for group in groups:
        for word in topic_terms(group.lead):
            counts[word] = counts.get(word, 0) + 1
    ceiling = max(2, int(len(groups) * MAX_SUBJECT_SHARE))
    for group in groups:
        group.topic = topic_key(group.lead, counts, ceiling)

    # Topics first, best-supported topic first, so one subject is reviewed in one sitting.
    weight: dict[tuple[bool, str], int] = {}
    for group in groups:
        cluster = (group.known, group.topic)
        weight[cluster] = weight.get(cluster, 0) + group.support
    groups.sort(
        key=lambda g: (
            g.known,
            -weight[(g.known, g.topic)],
            g.topic,
            -g.support,
            str(g.lead.get("topic", "")),
        )
    )
    return groups


def has_a_price(raw: dict) -> bool:
    """Whether a cost proposal actually carries a price.

    A cost entry with no number becomes "$0 to $0" in the fact block, which is not a
    missing answer but a wrong one, so it never reaches the file.
    """
    if raw.get("kind") != "cost":
        return True
    return bool(raw.get("low") or raw.get("high"))


def as_yaml_entry(proposal: Proposal, entry_id: str) -> str:
    """One proposal as the lines that go into facts.yaml, indentation and all.

    Written as text rather than dumped from a parsed document so the owner's file keeps its
    comments, which are half of what makes it editable by hand.
    """

    def quoted(value: str) -> str:
        return json.dumps(str(value))  # JSON strings are valid YAML and escape themselves

    note = "From: " + proposal.quote
    if proposal.support > 1:
        note += f" (stated in {proposal.support} of your sources)"

    lines = [f"  - id: {entry_id}"]
    if proposal.kind == "ordinance":
        lines.append(f"    jurisdiction: {quoted(proposal.jurisdiction or 'Chicago')}")
        lines.append(f"    topic: {quoted(proposal.topic)}")
        lines.append(f"    rule: {quoted(proposal.rule)}")
        lines.append(f"    citation: {quoted(proposal.citation or proposal.source_title)}")
        if proposal.source_url:
            lines.append(f"    url: {quoted(proposal.source_url)}")
        lines.append(f"    notes: {quoted(note)}")
    else:
        lines.append(f"    item: {quoted(proposal.topic)}")
        lines.append(f"    low: {proposal.low or 0}")
        lines.append(f"    high: {proposal.high or proposal.low or 0}")
        lines.append(f"    unit: {quoted(proposal.unit or 'per job')}")
        lines.append(f"    source: {quoted(proposal.source_title)}")
        lines.append(f"    notes: {quoted(note)}")
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
