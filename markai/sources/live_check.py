"""Checking a rate/fee figure in facts.yaml against the government source it should match.

`mark facts stale` (in `facts.py`) finds ordinances worth a second look from their own
citation alone - no network, nothing touched. This module is the next, opt-in step for the
handful of jurisdictions with a known official page: fetch it and look for the exact
dollar or percent figures the ordinance states.

There is deliberately no AI judgment call in the middle. A figure found verbatim on the
government page is real, checkable evidence the rule is still current; one that is not
found is *not* evidence it is wrong - only that this page, as fetched today, does not
confirm it (the figure could be phrased differently, live further down a page this
extractor trimmed, or the page could simply not be the section that covers it). Reporting
that distinction honestly, rather than a confident yes/no, is the whole point: the same
discipline `facts_miner` already holds every mined fact to (quote a passage, verify the
quote is really there) applies here too.

Every jurisdiction has an authoritative source in principle, but only a few are registered
here - "when possible" is an honest limit, not a shortcut. `AUTHORITATIVE_SOURCES` is keyed
by the Notice Wizard's own closed jurisdiction list (`markai.advisor.notice_wizard
.JURISDICTIONS`) rather than facts.yaml's many spellings of the same place, since that
module already solves - and tests - telling one Cook County label from another.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import httpx

    from markai.sources.facts import Ordinance

# Verified by hand against the live page before being added here, not guessed:
# - Chicago: Municipal Code Chapter 5-12 (Residential Landlords and Tenants), as published
#   by American Legal Publishing, the city's own code-hosting vendor.
# - Suburban Cook County: the county's own RTLO page, which states dollar/percent figures
#   (like the late fee cap) directly in its own text, not just a linked PDF.
# - Evanston: Title 5, Chapter 3 (the Residential Landlord and Tenant Ordinance) via
#   Municode, the city's own code-hosting vendor.
# - Illinois state law: the Security Deposit Return Act on the General Assembly's own site.
#   State law is the widest of the four and spans many acts depending on topic; this is the
#   one most of the state-level dollar-figure facts in this corpus actually cite.
AUTHORITATIVE_SOURCES: dict[str, str] = {
    "Chicago": "https://codelibrary.amlegal.com/codes/chicago/latest/chicago_il/0-0-0-2639041",
    "Suburban Cook County": "https://www.cookcountyil.gov/rtlo",
    "Evanston": (
        "https://library.municode.com/il/evanston/codes/code_of_ordinances?nodeId=TIT5HORE"
    ),
    "Illinois (no local ordinance)": (
        "https://www.ilga.gov/Legislation/ILCS/Articles?ActID=2202&ChapterID=62"
    ),
}

_MONEY_OR_PERCENT = re.compile(r"\$\s?[\d,]+(?:\.\d+)?|\b\d+(?:\.\d+)?\s?%")


@dataclass(frozen=True)
class LiveCheckResult:
    status: Literal["confirmed", "not_found", "no_source", "fetch_failed"]
    source_url: str | None
    detail: str


def _figures_in(text: str) -> set[str]:
    """Dollar amounts and percentages, normalized to their numeric value so "$1,000.00"
    and "$1000" - or "5.0%" and "5%" - compare equal.

    Day-counts are deliberately left out - "60 days", "60-day", and "sixty days" all mean
    the same thing but rarely match textually, which would make every real match look like
    a miss and defeat the point of a literal check.
    """
    figures = set()
    for raw in _MONEY_OR_PERCENT.findall(text):
        is_percent = raw.strip().endswith("%")
        digits = re.sub(r"[^\d.]", "", raw)
        if not digits:
            continue
        value = f"{float(digits):g}"
        figures.add(f"{value}%" if is_percent else f"${value}")
    return figures


def wizard_jurisdiction_for(raw_jurisdiction: str) -> str | None:
    """Which of the Notice Wizard's four jurisdictions `raw_jurisdiction` (a facts.yaml
    spelling like "Cook County (suburban)") belongs to, or ``None`` if it matches none of
    them - reusing the wizard's own, already-tested matching rather than re-solving it."""
    from markai.advisor.notice_wizard import JURISDICTIONS, _jurisdiction_matches

    for wizard_jurisdiction in JURISDICTIONS:
        if _jurisdiction_matches(raw_jurisdiction, wizard_jurisdiction):
            return wizard_jurisdiction
    return None


def check_live(ordinance: Ordinance, client: httpx.Client) -> LiveCheckResult:
    """Fetch the registered source for `ordinance.jurisdiction` and look for every dollar
    or percent figure `ordinance.rule` states."""
    from markai.ingest.websites import extract_main_text, fetch_page
    from markai.models import IngestError

    wizard_jurisdiction = wizard_jurisdiction_for(ordinance.jurisdiction)
    url = AUTHORITATIVE_SOURCES.get(wizard_jurisdiction or "")
    if not url:
        return LiveCheckResult("no_source", None, "no authoritative source registered yet")

    wanted = _figures_in(ordinance.rule)
    if not wanted:
        return LiveCheckResult(
            "no_source", url, "nothing to check - the rule states no dollar or percent figure"
        )

    try:
        page = fetch_page(url, client)
    except IngestError as exc:
        return LiveCheckResult("fetch_failed", url, str(exc))

    _, text = extract_main_text(page.html, page.final_url)
    found = _figures_in(text)
    missing = wanted - found
    if not missing:
        return LiveCheckResult("confirmed", url, "every figure in the rule appears on this page")
    return LiveCheckResult(
        "not_found",
        url,
        f"{', '.join(sorted(missing))} not found on this page as fetched - not proof it's "
        "wrong, just unconfirmed here",
    )
