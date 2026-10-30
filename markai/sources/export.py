"""Package the fact layer for a chatbot that is not Jay.

The owner wants to hand GC Realty's knowledge to another assistant - their Grok bot - so it
can answer landlord questions with the same material. That is a reasonable thing to want and
a dangerous thing to do naively, because everything that makes Jay safe lives outside
``facts.yaml``:

- Jay never sees the whole fact layer. ``facts.select()`` puts at most ``MAX_SELECTED``
  entries in front of one question, chosen by term overlap. A pack hands over all of them at
  once, so the failure mode changes from "no fact" to "a fuzzy match picked the wrong city's
  fact". The file genuinely holds four different security-deposit deadlines for four
  overlapping jurisdictions, so that is not a theoretical risk.
- Jay's legal disclaimer is appended by ``guardrails``, not written by the model. A third
  party has no appender, so the pack has to ask for the sentence instead of forbidding it -
  the opposite of what the system prompt says, which is why it is spelled out here.
- "Jay never narrates a gap" is safe only because ``retrieval.coverage`` exists to catch the
  gap elsewhere. Exported verbatim into a file with no coverage signal, the same words mean
  "answer anyway". The pack instructs the opposite on purpose.

So the export is two files, not one, and the split is not cosmetic. Grok's custom
instructions field holds a few thousand characters and persists across every conversation;
an uploaded file is large but applies only where it is attached. The rules have to be in the
part that always loads, or the first chat opened without the attachment is a landlord being
told Evanston's notice period for a Chicago building.

Nothing here reads a store. The pack is built from ``facts.yaml`` and the ``business`` and
``tools`` blocks of the manifest, and from nothing else - no settings, no accounts, no
property log, no questions anyone asked. ``mark report`` already decided what is safe to
hand to a human helping debug; this goes to a model whose retention the owner does not
control, so it is strictly narrower and has no ``--questions`` escape hatch.
"""

from __future__ import annotations

import re
from datetime import date

from markai.advisor.guardrails import HIGH_RISK_RESPONSE, LEGAL_DISCLAIMER
from markai.sources.facts import CostRange, FactBook, Ordinance
from markai.sources.manifest import BusinessProfile, SourceManifest

# Pages on GC Realty's own site that publish how *this company* operates. A rule mined from
# one of them is a house policy, however much it reads like an ordinance: "guests may stay up
# to 14 days" is a lease term, not the municipal code. They arrive labelled "Chicago" or
# "Illinois" like everything else, and 18 of the 22 carry no hint of whose rule they are, so
# matching on the source URL is the only reliable way to catch them.
POLICY_PAGES = (
    "/resident-handbook",
    "/owner-handbook",
    "/tenant-faq",
    "/leasing-faq",
    "/gc-property-standards",
    "/pay-your-rent",
    "/faq",
)

POLICY_LABEL = "GC Realty policy, not law"

# Entries that are not exported at any label, with the reason each one is here. Relabelling
# is enough for a house rule about light bulbs; it is not enough for these.
WITHHELD: dict[str, str] = {
    # One company's screening thresholds, published to a stranger as though they were the
    # standard, is how a disparate-impact complaint starts. HIGH_RISK_RESPONSE exists in
    # guardrails for exactly this topic, and a third-party bot has no equivalent.
    "mined-459": "screening thresholds - one company's criteria must not read as a legal standard",
    "mined-400": "screening rule of thumb - same reason",
    # True for nonpayment, wrong as stated: a lease violation and a termination run on
    # different clocks. A relabel does not fix a sentence that is wrong as law.
    "mined-337": "states one notice period as the prerequisite for every eviction reason",
}

# The jurisdiction strings in facts.yaml are free text and 90 distinct spellings have
# accumulated. The pack groups by jurisdiction, so the spellings have to collapse - but only
# ever into a label that says the same thing. Order is the tier the group prints in: the
# baselines first, because they apply to every building in the pack, then the places.
_TIERS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (0, "Applies anywhere (federal)", ("federal", "united states", "u.s.", "hud", "fha")),
    (1, "Illinois (statewide)", ("illinois",)),
    (2, "Chicago", ("chicago",)),
    (3, "Cook County (suburban / unincorporated)", ("cook",)),
    (4, "Evanston", ("evanston",)),
    (5, "Oak Park", ("oak park",)),
    (6, "Mount Prospect", ("mount prospect", "mt. prospect", "mt prospect")),
)

# "Berkeley" and "Berkeley, IL" are one town with two spellings in the file, and left alone
# they print as two headings with two entries each - which reads as two different places
# whose rules might differ. Only the state suffix is removed: the town's own name is never
# rewritten, because a heading nobody typed is a jurisdiction nobody checked.
_STATE_SUFFIX = re.compile(r"[,\s]+(?:il|ill|illinois)\.?$", re.IGNORECASE)


def canonical_jurisdiction(raw: str) -> tuple[int, str]:
    """The tier and heading one jurisdiction label belongs under.

    A label naming both Chicago and Cook County states a rule for both and files under
    Chicago, the narrower of the two. A Chicago-only label never answers for the suburbs, so
    nothing is ever widened: an unrecognised municipality keeps its own name and its own
    heading rather than being swept into a county it may not be in.
    """
    text = " ".join((raw or "").split())
    low = text.lower()
    if not low:
        return 9, "Jurisdiction not stated in the source"
    if POLICY_LABEL.lower() in low or "gc realty" in low:
        return 8, f"{POLICY_LABEL} (GC Realty's own rules)"
    for tier, heading, needles in _TIERS:
        if any(needle in low for needle in needles):
            return tier, heading
    return 7, _STATE_SUFFIX.sub("", text).strip(" ,")


def is_policy(rule: Ordinance) -> bool:
    """True when this rule came off a page describing how GC Realty itself operates."""
    url = (rule.url or "").lower()
    return any(page in url for page in POLICY_PAGES)


def _flat(text: str | None) -> str:
    """One line, whatever the source did with newlines. Never truncated.

    A quote is the only thing that makes an exported claim checkable, so it is reflowed and
    never shortened: an ellipsis in the middle of an ordinance is worse than no quote at all.
    """
    return " ".join((text or "").split())


# The miner's marker, and the only one. 544 of the 547 notes carry it; the three that do
# not are the owner writing about an entry rather than quoting it. Matching anything looser -
# a bare "From the City of Evanston's own page:" opening a correction - turns the owner's
# own commentary into something the pack presents as a quoted source sentence, which is the
# one thing a quote line must never be.
_QUOTE_MARKER = "From: "


def split_note(notes: str | None) -> tuple[str, str]:
    """A mined ``notes`` field into (quoted passage, the owner's own commentary).

    The miner writes ``From: <the sentence it read>``, sometimes after a correction the
    owner typed first. Both matter and they are not the same thing, so the pack prints them
    on separate lines; a note with no marker is commentary in full and yields no quote.
    """
    text = _flat(notes)
    if not text:
        return "", ""
    marker = text.find(_QUOTE_MARKER)
    if marker < 0:
        return "", text
    return _flat(text[marker + len(_QUOTE_MARKER) :]), _flat(text[:marker])


def exportable(book: FactBook, as_of: date) -> tuple[list[Ordinance], list[str]]:
    """The in-force rules the pack may carry, and the ids held back, with reasons.

    Nothing is dropped silently. The caller prints the second list, the same way
    ``mark facts drop`` keeps what it dropped visible rather than making it disappear.
    """
    kept, held = [], []
    for rule in book.in_force(as_of):
        if rule.id in WITHHELD:
            held.append(f"{rule.id} - {WITHHELD[rule.id]}")
            continue
        kept.append(rule)
    return kept, held


def _business_lines(business: BusinessProfile) -> list[str]:
    out: list[str] = []
    if business.name:
        out.append(f"- **Who this is about:** {_flat(business.name)}")
    if business.services:
        out.append(f"- **What they do:** {_flat(business.services)}")
    if business.service_area:
        out.append(f"- **Where they work:** {_flat(business.service_area)}")
    if business.contact_url:
        out.append(f"- **Site:** <{_flat(business.contact_url)}>")
    if business.escalation_url:
        who = _flat(business.escalation_name) or "someone at GC Realty"
        out.append(
            f"- **When a question has outgrown a chat:** book {who}, {business.escalation_url}"
        )
    return out


def instructions(
    manifest: SourceManifest,
    kept: int,
    costs: int,
    today: date,
    pack_name: str,
) -> str:
    """The short block that goes in the other bot's standing instructions.

    Kept deliberately small. Grok's custom-instructions field has been reported at 12,000
    characters and at 4,000 within the same year, so this has to survive the smaller of the
    two - and a rule nobody can fit in the box is a rule that does not run.
    """
    business = manifest.business
    name = _flat(business.name) or "GC Realty & Development"
    lines = [
        f"You help landlords in the Chicago area using a file called {pack_name}, exported "
        f"from {name}'s reviewed materials on {today.isoformat()}. Follow these rules in "
        "every answer, whether or not that file is attached to this conversation.",
        "",
        f"1. YOU ARE NOT JAY. Jay is {name}'s own advisor and has protections you do not. "
        "Never claim to be Jay, Mark Ainley, or anyone at the company, and never say your "
        "answers are reviewed by them or by a lawyer.",
        "",
        "2. THE FILE IS THE WHOLE WORLD. If the pack does not cover something, say so "
        "plainly and stop. Do not fill the gap from your own knowledge of landlord law - "
        "these rules change town by town and a confident wrong answer about a notice period "
        "gets a case thrown out. If the pack is not attached to this conversation, say that "
        "instead of answering from memory.",
        "",
        "3. CITE THE ENTRY ID. End every factual sentence with the id it came from, like "
        "`mined-77`. A sentence with no id is not supported by the pack and you should not "
        "be writing it.",
        "",
        "4. ASK WHICH TOWN FIRST. Almost every rule turns on which municipal line the "
        "building sits behind. Chicago, suburban Cook County, Evanston, Oak Park and Mount "
        "Prospect each have their own ordinance and they disagree with each other. Never "
        "answer one town's question with another town's rule, and never widen a city rule "
        "into a county one. If you do not know the town, ask.",
        "",
        "5. NOTICE PERIODS FORK ON THE REASON. Nonpayment, a lease violation and a "
        "non-renewal run on different clocks, and some depend on how long the tenant has "
        "lived there. Ask which one before you answer.",
        "",
        "6. NOTHING IN THE PACK CARRIES AN EFFECTIVE DATE. It was reviewed as of the export "
        "date above. Ordinances usually change on January 1. Say so when a figure or a "
        "deadline is doing real work in your answer.",
        "",
        '7. ENTRIES MARKED "' + POLICY_LABEL.upper() + "\" ARE THIS COMPANY'S OWN RULES, "
        "not anybody's ordinance. Never restate one as law.",
        "",
        "8. END ANY ANSWER THAT TOUCHES LAW WITH THIS SENTENCE, WORD FOR WORD:",
        f'   "{LEGAL_DISCLAIMER}"',
        "",
        "9. REFUSE TWO THINGS OUTRIGHT: screening a tenant on a protected characteristic, "
        "and removing a tenant outside the court process. Reply with this, word for word:",
    ]
    for line in HIGH_RISK_RESPONSE.split("\n"):
        lines.append(f"   {line}" if line else "")
    if business.never_say:
        lines += ["", "10. NEVER SAY:"] + [f"   - {_flat(item)}" for item in business.never_say]
    if business.extra_instructions:
        lines += ["", _flat(business.extra_instructions)]
    lines += [
        "",
        f"The pack holds {kept} reviewed rules and {costs} cost ranges, each with the "
        "sentence it came from and a link. When someone needs more than the pack has, point "
        "them at the company rather than guessing.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _rule_block(rule: Ordinance, policy: bool) -> list[str]:
    quote, owner_note = split_note(rule.notes)
    label = f" · **{POLICY_LABEL}**" if policy else ""
    out = [
        f"- **{_flat(rule.topic)}** · {_flat(rule.jurisdiction)}{label} · `{rule.id}`",
        f"  {_flat(rule.rule)}",
    ]
    if quote:
        out.append(f"  > {quote}")
    source = _flat(rule.citation)
    if rule.url:
        source = f"{source} · <{_flat(rule.url)}>" if source else f"<{_flat(rule.url)}>"
    if source:
        out.append(f"  — {source}")
    if owner_note:
        out.append(f"  NOTE FROM THE OWNER: {owner_note}")
    if rule.effective_from:
        out.append(f"  In force since {rule.effective_from.isoformat()}.")
    return out


def _cost_block(cost: CostRange) -> list[str]:
    # `money()` and never an f-string: it drops the dollar sign on units that are not money,
    # because "$17 to $20" on a days-on-market figure is a wrong answer, not a typo.
    head = f"- **{_flat(cost.item)}** · {cost.money()} {_flat(cost.unit)} · `{cost.id}`"
    out = [head]
    if cost.market:
        out.append(f"  Where: {_flat(cost.market)}.")
    quote, owner_note = split_note(cost.notes)
    if quote:
        out.append(f"  > {quote}")
    tail = _flat(cost.source)
    if cost.as_of:
        tail = f"{tail} · as of {_flat(cost.as_of)}" if tail else f"as of {_flat(cost.as_of)}"
    if tail:
        out.append(f"  — {tail}")
    if owner_note:
        out.append(f"  NOTE FROM THE OWNER: {owner_note}")
    return out


def context_pack(
    book: FactBook,
    manifest: SourceManifest,
    today: date,
    include_costs: bool = True,
) -> tuple[str, list[str]]:
    """The uploadable file, and the list of entries held back from it."""
    kept, held = exportable(book, today)
    business = manifest.business
    name = _flat(business.name) or "GC Realty & Development"

    groups: dict[tuple[int, str], list[Ordinance]] = {}
    for rule in kept:
        key = (
            (8, f"{POLICY_LABEL} (GC Realty's own rules)")
            if is_policy(rule)
            else canonical_jurisdiction(rule.jurisdiction)
        )
        groups.setdefault(key, []).append(rule)

    say: list[str] = [
        f"# {name} — landlord context pack",
        "",
        f"Exported {today.isoformat()} · {len(kept)} rules"
        + (f" · {len(book.costs)} cost ranges" if include_costs and book.costs else ""),
        "",
        "Every line below was reviewed and approved by hand by the owner, and carries the "
        "sentence it was taken from so any claim can be checked against its source. Nothing "
        "here was generated.",
        "",
        "## Read this before answering anything",
        "",
        "- **This file is the whole world.** If something is not in it, say so and stop. Do "
        "not answer landlord questions from your own training: these rules differ town by "
        "town and change on their own schedule.",
        "- **Cite the entry id** — the `mined-NNN` in backticks — at the end of every "
        "factual sentence you write. No id, no claim.",
        "- **Ask which town the building is in before you answer.** The sections below are "
        "grouped by jurisdiction because they disagree with each other. A rule under "
        "*Chicago* is wrong for Evanston, and a rule under *Cook County* does not cover "
        "Chicago, which has its own ordinance.",
        "- **The first two sections apply everywhere** in the pack. Federal and Illinois "
        "rules stack underneath whatever the municipality says; the most specific one wins.",
        "- **Notice periods fork on the reason** (nonpayment, lease violation, "
        "non-renewal) and sometimes on how long the tenant has lived there.",
        "- **No entry carries an effective date.** They were reviewed as of the export date "
        f"above. Re-export this file before {today.year + 1}-03-01.",
        f"- **Entries marked “{POLICY_LABEL}”** are how this company runs its own "
        "buildings. They are not ordinances and must never be restated as law.",
        f"- **End any answer touching law with:** “{LEGAL_DISCLAIMER}”",
        "",
    ]

    business_lines = _business_lines(business)
    if business_lines:
        say += ["## Who this is about", ""] + business_lines + [""]

    if manifest.tools:
        say += ["## Tools worth pointing people at", ""]
        for tool in manifest.tools:
            line = f"- **{_flat(tool.name)}** — {_flat(tool.description)}"
            if tool.url:
                line += f" <{_flat(tool.url)}>"
            say.append(line)
            if tool.when_to_recommend:
                say.append(f"  When: {_flat(tool.when_to_recommend)}")
        say.append("")

    say += ["## Coverage", ""]
    for (_, heading), rules in sorted(groups.items()):
        say.append(f"- {heading}: {len(rules)}")
    say += [
        "",
        "A town that does not appear above has no entries here at all. Say that rather than "
        "reaching for a neighbouring town's rule.",
        "",
    ]

    for (_, heading), rules in sorted(groups.items()):
        say += [f"## {heading}", ""]
        for rule in sorted(rules, key=lambda r: (r.topic.lower(), r.id)):
            say += _rule_block(rule, policy=is_policy(rule))
            say.append("")

    if include_costs and book.costs:
        say += [
            "## What things cost",
            "",
            "Ranges the owner recorded by hand. A figure with no dollar sign is not money — "
            "read the unit. Several of these are per-municipality fees that happen to share "
            "a name, so check the *Where* line before quoting one.",
            "",
        ]
        for cost in sorted(book.costs, key=lambda c: (c.item.lower(), c.id)):
            say += _cost_block(cost)
            say.append("")

    say += [
        "---",
        "",
        "Deliberately not in this file: no API key, no landlord's name, email or phone, "
        "nothing from anyone's property log, and no question anyone has ever asked. It is "
        "the reviewed fact layer and the owner's business details, and nothing else.",
    ]
    return "\n".join(say).rstrip() + "\n", held
