"""Hand the case to a person without making the landlord retype it.

When a problem has outgrown a chat, the honest move is a property manager. What makes that
handoff worth anything is context: which building, what they already asked, and where the
answer ran out. This assembles that from what is already stored, so it costs nothing and
adds no round trip to Claude. The landlord reads it before anyone else does, and pastes it
into the booking form.
"""

from __future__ import annotations

from datetime import date
from typing import Any

MAX_QUESTIONS = 8
MAX_ANSWER_CHARS = 700
MAX_QUESTION_CHARS = 200


def _trim(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut + "..."


MAX_OPEN = 6


def build_handoff(
    thread: Any | None,
    properties: list[Any] | None = None,
    name: str | None = None,
    url: str | None = None,
    today: date | None = None,
    open_items: list[Any] | None = None,
) -> str:
    """A plain-text case file. Deterministic: the same conversation gives the same notes."""
    who = (name or "").strip() or "a property manager"
    lines = [f"Case notes for {who} - {(today or date.today()).isoformat()}"]

    if properties:
        lines.append("")
        lines.append("Property:" if len(properties) == 1 else "Properties:")
        lines.extend(f"- {item.one_line()}" for item in properties)

    if open_items:
        # The first thing a manager asks is what is already outstanding. It is written
        # down, so nobody should have to remember it out loud on a phone call.
        lines.append("")
        lines.append("Still open:")
        lines.extend(f"- {item.one_line()}" for item in open_items[:MAX_OPEN])

    messages = list(getattr(thread, "messages", None) or [])
    questions = [m.get("content", "") for m in messages if m.get("role") == "user"]
    answers = [m.get("content", "") for m in messages if m.get("role") == "assistant"]

    if questions:
        lines.append("")
        lines.append("What I asked Jay:")
        # The last few, not the first few: a conversation drifts toward the real problem.
        for index, question in enumerate(questions[-MAX_QUESTIONS:], start=1):
            lines.append(f"{index}. {_trim(question, MAX_QUESTION_CHARS)}")

    if answers:
        lines.append("")
        lines.append("Where it stands:")
        lines.append(_trim(answers[-1], MAX_ANSWER_CHARS))

    if not questions and not answers and not open_items:
        lines.append("")
        lines.append("I have not asked Jay anything yet in this conversation.")

    if url:
        lines.append("")
        lines.append(f"Booking: {url}")
    return "\n".join(lines)
