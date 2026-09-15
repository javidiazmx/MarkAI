"""Proactive, time-relevant reminders shown on the chat page, not just answers to questions.

A landlord opens a reference tool when something is already wrong. A tool that only reacts
stays a reference doc, not a habit - the research behind this feature is explicit that
well-timed proactive value is the lever that turns a tool into something people return to.

Computed live from today's date against a fact already carried in ``facts.yaml`` (Chicago's
Heat Ordinance season), not a scheduler - nothing to configure, nothing that can silently
stop firing because a background job never ran. Each nudge is dismissible on the page and
never blocks the input box, on purpose: a proactive nudge that cannot be waved away is a
dark pattern, not a feature.
"""

from __future__ import annotations

from datetime import date
from typing import Any

# Chicago Heat Ordinance: enforced September 15 through June 1 each year (see
# sources/facts.yaml, "Heat ordinance season" - the citation this nudge relies on).
HEAT_SEASON_START = (9, 15)
HEAT_SEASON_END = (6, 1)


def _in_heat_season(today: date) -> bool:
    start = (today.month, today.day) >= HEAT_SEASON_START
    end = (today.month, today.day) <= HEAT_SEASON_END
    # The window wraps the new year (Sep 15 -> Jun 1), so it's an OR, not an AND.
    return start or end


def active_nudges(today: date | None = None) -> list[dict[str, Any]]:
    """Every nudge worth showing right now, each with a stable ``id`` so the page can
    remember a dismissal per nudge rather than per visit."""
    today = today or date.today()
    nudges: list[dict[str, Any]] = []
    if _in_heat_season(today):
        nudges.append(
            {
                "id": "heat-season",
                "title": "Heat season is on",
                "text": (
                    "Chicago's Heat Ordinance runs September 15 through June 1: 68°F "
                    "daytime, 66°F overnight, citywide. Worth a check on any building "
                    "with an older boiler before a cold snap makes it urgent."
                ),
            }
        )
    return nudges
