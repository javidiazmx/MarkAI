"""A guided form over the same sourced facts Jay already cites in chat - no free text to
get wrong, no AI call, instant.

The #1 landlord complaint this exists for, found across eviction-attorney writeups and
landlord forums: serving the wrong notice type resets the clock, and sometimes gets the
whole case thrown out. Chicago, suburban Cook County, and Evanston each escalate the
no-cause notice period by how long the tenant has lived there in a different way; a
landlord guessing from memory is exactly how that mistake happens.

This is a query over `facts.yaml`, not a parallel legal ruleset it could drift from - it
turns four structured picks (jurisdiction, reason, tenure) into a search, and shows
whatever the reviewed, cited facts already say back, ranked by relevance. Jurisdiction is
filtered first rather than left to keyword ranking alone: `facts.select()`'s plain
term-overlap scoring is exactly what let Chicago's much more heavily-mined Fair Notice
Ordinance facts crowd out a correct suburban or Illinois-general match earlier in this
project (see markai/sources/facts.py's MAX_SELECTED note) - a wizard that already knows
the jurisdiction the landlord picked has no reason to risk that again.
"""

from __future__ import annotations

from datetime import date
from typing import Any

JURISDICTIONS = [
    "Chicago",
    "Suburban Cook County",
    "Evanston",
    "Illinois (no local ordinance)",
]

REASONS = {
    "nonpayment": "Tenant hasn't paid rent",
    "lease_violation": "Tenant broke a lease term (unauthorized pet, guest, noise, damage)",
    "no_cause_nonrenewal": "Ending the tenancy or not renewing, no fault of the tenant's",
}

_REASON_TERMS = {
    "nonpayment": "5-day notice nonpayment rent pay",
    "lease_violation": "10-day notice lease violation comply",
    "no_cause_nonrenewal": "notice non-renewal terminate tenancy renewing month-to-month",
}


def _excludes_chicago(jurisdiction_lower: str) -> bool:
    """A jurisdiction label can name Chicago while meaning the opposite of "in Chicago" -
    "Cook County (suburbs outside Chicago)", "Chicago suburbs", "Illinois (outside Chicago)"
    all mention the word but describe somewhere the city ordinance does not reach. Plain
    substring matching would let a label like that answer for Chicago, which is exactly the
    backwards mistake this module exists to prevent - so this is checked before "chicago" in
    a label is ever treated as a Chicago match."""
    return "outside" in jurisdiction_lower or "suburb" in jurisdiction_lower


def _jurisdiction_matches(ordinance_jurisdiction: str, wizard_jurisdiction: str) -> bool:
    """Loosely, since the source material spells the same place several ways
    ("Cook County", "Suburban Cook County", "Cook County, IL" all mean the same RTLO rule
    here) - but never lets a Chicago-specific rule answer for anywhere else, which is the
    actual mistake this tool exists to prevent.

    A label that names Chicago and Cook County together ("Chicago/Cook County", "Cook
    County (including Chicago)") states one rule for both, not a Chicago-only rule that
    happens to mention the county - unlike the Chicago Fair Notice Ordinance's own
    tenure-escalated notice periods, which are always filed under "Chicago" alone, never
    paired with "Cook" in the same label. So any label mentioning Cook County answers for
    Suburban Cook County; only a Chicago-only label (no "cook" in it at all) does not.
    """
    j = ordinance_jurisdiction.lower()
    excludes_chicago = _excludes_chicago(j)
    if wizard_jurisdiction == "Chicago":
        return "chicago" in j and not excludes_chicago
    if wizard_jurisdiction == "Suburban Cook County":
        return "cook" in j
    if wizard_jurisdiction == "Evanston":
        return "evanston" in j
    if wizard_jurisdiction == "Illinois (no local ordinance)":
        return (
            "illinois" in j
            and ("chicago" not in j or excludes_chicago)
            and "cook" not in j
            and "evanston" not in j
        )
    return False


def _tenure_terms(tenure_years: float | None) -> str:
    """The tenure tiers are described in words in the sources ("more than three years",
    "less than six months"), not numbers - matching the same words is what actually finds
    them; a bare "3.5" shares no term with any of that."""
    if tenure_years is None:
        return ""
    if tenure_years >= 3:
        return "three years or longer lived"
    if tenure_years >= 0.5:
        return "six months but less than three years"
    return "less than six months"


def find_notice_rules(
    book: Any, jurisdiction: str, reason: str, tenure_years: float | None, as_of: date | None = None
) -> list[Any]:
    """Every in-force ordinance for this jurisdiction, ranked by how well it answers this
    specific reason/tenure - the same `select()` ranking chat uses, just scoped to a
    jurisdiction already filtered instead of run over the whole book.
    """
    from markai.sources.facts import _terms

    today = as_of or date.today()
    candidates = [
        o for o in book.in_force(today) if _jurisdiction_matches(o.jurisdiction, jurisdiction)
    ]
    if not candidates:
        return []
    # Tenure only means anything for a non-renewal notice - the tenure tiers describe how
    # long the escalated notice period is, which nonpayment and lease-violation notices
    # don't have. Folding it into their query anyway would let a stray tenure word outrank
    # the fact that actually answers the reason asked.
    tenure = tenure_years if reason == "no_cause_nonrenewal" else None
    query = " ".join([_REASON_TERMS.get(reason, reason), _tenure_terms(tenure)])
    asked = _terms(query)
    if not asked:
        return candidates[:5]
    scored = [(len(asked & o.terms()), o) for o in candidates]
    scored = [(score, o) for score, o in scored if score]
    if not scored:
        # Nothing in this jurisdiction mentions the reason/tenure words at all - better to
        # show everything this jurisdiction does have than to show nothing.
        return candidates[:5]
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [o for _, o in scored[:5]]
