"""The monthly summary landlords never asked for but come back to see.

Reactive tools - a chat window, a calculator - only get opened once something already needs
attention. This is the other half: a short "here is what changed" note, built entirely from
numbers already on file (`Ledger.totals`, `Ledger.open_items`), so it costs nothing to
produce and says nothing that isn't already true.

Deliberately a CLI command (`mark digest send`), not a silent background thread inside
`mark serve`. A digest is only as good as its schedule, and a process that gets killed by
the OS under memory pressure - which happens - is not a schedule. Wiring an actual monthly
cadence is an operating-system decision (Windows Task Scheduler, cron, a hosted job runner),
left to whoever runs this, the same way `mark leads send` is triggered from outside rather
than assumed to run itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass
class Digest:
    """One landlord's monthly numbers, plain enough to read in an email on a phone."""

    name: str
    email: str
    money_in: float
    money_out: float
    net: float
    open_items: list[str]
    property_count: int

    @property
    def has_anything_to_say(self) -> bool:
        """Nothing moved and nothing is open: a digest that says so is still spam."""
        return bool(self.money_in or self.money_out or self.open_items)


def build_digest(account: Any, ledger: Any, portfolio: Any, since: date) -> Digest:
    """One account's digest for the window starting at ``since``."""
    totals = ledger.totals(account.owner_id, since=since)
    open_entries = ledger.open_items(account.owner_id, limit=5)
    properties = portfolio.list(account.owner_id)
    return Digest(
        name=account.name,
        email=account.email,
        money_in=totals.get("in", 0.0),
        money_out=totals.get("out", 0.0),
        net=totals.get("net", totals.get("in", 0.0) - totals.get("out", 0.0)),
        open_items=[entry.what for entry in open_entries],
        property_count=len(properties),
    )


def digest_email(digest: Digest) -> tuple[str, str]:
    """Subject and plain-text body. Short enough to read without scrolling on a phone."""
    subject = f"Your Jay update: ${digest.net:,.0f} net this month"
    lines = [
        f"Hi {digest.name.split()[0] if digest.name else 'there'},",
        "",
        f"Rent and other income this month: ${digest.money_in:,.2f}",
        f"Expenses this month: ${digest.money_out:,.2f}",
        f"Net: ${digest.net:,.2f}",
        "",
    ]
    if digest.open_items:
        lines.append("Still open on your log:")
        lines.extend(f"- {item}" for item in digest.open_items)
        lines.append("")
    lines.append("Ask Jay anything, any time - that's what he's there for.")
    return subject, "\n".join(lines)
