"""The tool that lets Jay keep the landlord's property log, and read it back later.

The point of this is a conversation months apart. In March they mention the plumber came
out for the second time about the same stack; in September they ask "how many times have I
had somebody out for that drain?" and the answer is theirs, not a guess.

Four actions, and the fourth one is deliberately not "delete":

- ``add`` writes down something they said happened.
- ``find`` reads it back, by words, by kind, by building, by how long ago.
- ``total`` adds the money up in code, because arithmetic is not what a model is for.
- ``close`` marks an open item done.

Deleting a landlord's records is a button on their page, not something a model does on a
sentence it may have misread. The same reasoning keeps ``add`` narrow: it records what they
told Jay, in their words, and Jay confirms it in the answer so a mistake is visible
immediately rather than six months later.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

MAX_RESULTS = 20

LOG_TOOL: dict[str, Any] = {
    "name": "property_log",
    "description": (
        "The user's own running log of what is happening at their properties: expenses, "
        "bills, rent received, maintenance issues, visits and notes. Use action 'add' when "
        "they tell you something happened or was paid or is broken, and confirm in your "
        "answer what you wrote down. Use 'find' when they ask about anything they told you "
        "before - what was paid, when somebody came out, what is still open, what happened "
        "at a building. Use 'total' for how much they have spent or collected; never add "
        "the money up yourself. Use 'close' when they say something is fixed or paid. "
        "Never invent an entry they did not describe, and never state a figure from this "
        "log that a call did not return."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "find", "total", "close"],
                "description": "What to do with the log.",
            },
            "kind": {
                "type": "string",
                "enum": ["expense", "bill", "income", "maintenance", "visit", "note"],
                "description": (
                    "For 'add', what sort of thing it is; for 'find', narrow to one sort. "
                    "'income' is rent or money in. Empty string when it does not apply."
                ),
            },
            "what": {
                "type": "string",
                "description": (
                    "For 'add': what happened, in the user's own words, a few words long. "
                    "Empty string for the other actions."
                ),
            },
            "amount": {
                "type": "number",
                "description": "Dollars, if a figure was named. 0 when there was none.",
            },
            "vendor": {
                "type": "string",
                "description": (
                    "Who did it or who was paid, in their words. An empty string when they "
                    "did not name anyone - never a placeholder and never anything else."
                ),
            },
            "date": {
                "type": "string",
                "description": (
                    "The day it happened as YYYY-MM-DD, or 'today'. Empty string means "
                    "today. Never guess a date they did not give."
                ),
            },
            "property": {
                "type": "string",
                "description": (
                    "Which building, in the words they use for it. Empty string when they "
                    "did not say or own only one."
                ),
            },
            "status": {
                "type": "string",
                "enum": ["open", "done", ""],
                "description": (
                    "For 'add', whether it is still outstanding; maintenance defaults to "
                    "open. For 'find', 'open' returns only what is outstanding."
                ),
            },
            "search": {
                "type": "string",
                "description": (
                    "For 'find': words to look for in what was logged, like 'boiler' or "
                    "the vendor's name. Empty string to list by kind or building instead."
                ),
            },
            "since_days": {
                "type": "integer",
                "description": (
                    "For 'find' and 'total': only the last N days. 0 means all of it. Use "
                    "365 for a year, 30 for a month."
                ),
            },
            "entry_id": {
                "type": "string",
                "description": (
                    "For 'close': the id of the entry, as a previous 'find' returned it. "
                    "Empty string otherwise."
                ),
            },
        },
        "required": [
            "action",
            "kind",
            "what",
            "amount",
            "vendor",
            "date",
            "property",
            "status",
            "search",
            "since_days",
            "entry_id",
        ],
        "additionalProperties": False,
    },
}

NO_LOG = "There is no property log on this surface, so nothing can be written down or read back."


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def add_dedupe_key(tool_input: dict[str, Any]) -> tuple[str, ...] | None:
    """The fields that make two ``add`` calls the same entry, or ``None`` for other actions.

    Parallel tool use lets one turn carry two ``tool_use`` blocks. If Claude calls ``add``
    twice for the same thing the landlord said once, both would otherwise land as separate
    rows - a duplicate the model created, not one the landlord asked for. This key lets the
    caller in ``mark.py`` recognize the second call within the same turn and answer it from
    the first save instead of writing again. It is deliberately scoped to one turn: a
    landlord logging the same-sounding expense in a different month later is not a duplicate.
    """
    args = tool_input or {}
    if str(args.get("action") or "").strip().lower() != "add":
        return None
    return (
        str(args.get("kind") or "").strip().lower(),
        str(args.get("what") or "").strip().lower(),
        str(args.get("amount") or "").strip(),
        str(args.get("vendor") or "").strip().lower(),
        str(args.get("date") or "").strip().lower(),
        str(args.get("property") or "").strip().lower(),
    )


def run_log_tool(log: Any, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Serve ``property_log``. Always returns a JSON-serializable dict, never raises."""
    from markai.web.ledger import LogError

    if log is None:
        return {"error": NO_LOG}
    args = tool_input or {}
    action = str(args.get("action") or "").strip().lower()

    if action == "add":
        try:
            saved = log.add(
                {
                    "kind": args.get("kind"),
                    "what": args.get("what"),
                    "amount": args.get("amount") or None,
                    "vendor": args.get("vendor"),
                    "date": args.get("date"),
                    "status": args.get("status"),
                    "property": args.get("property"),
                }
            )
        except LogError as exc:
            # The message says which field and why, so the model can ask for that one thing.
            return {"error": str(exc)}
        except Exception as exc:
            logger.warning("could not save a log entry: %s", type(exc).__name__)
            return {"error": "That could not be written down. Nothing was saved."}
        return {"saved": saved.to_dict(), "reads_back_as": saved.one_line()}

    if action == "find":
        try:
            found = log.find(
                search=str(args.get("search") or ""),
                kind=str(args.get("kind") or ""),
                property_name=str(args.get("property") or ""),
                since_days=max(0, _int(args.get("since_days"))),
                status=str(args.get("status") or "").strip().lower(),
                limit=MAX_RESULTS,
            )
        except Exception as exc:
            logger.warning("could not read the log: %s", type(exc).__name__)
            return {"error": "The log could not be read."}
        return {
            "entries": [entry.to_dict() for entry in found],
            "found": len(found),
            # Said explicitly so an empty result is not narrated as a gap in the sources.
            "note": "" if found else "Nothing in their log matches that.",
        }

    if action == "total":
        try:
            sums = log.totals(
                property_name=str(args.get("property") or ""),
                since_days=max(0, _int(args.get("since_days"))),
            )
        except Exception as exc:
            logger.warning("could not total the log: %s", type(exc).__name__)
            return {"error": "The log could not be totalled."}
        return {"totals": sums, "currency": "USD", "counted": "only entries with an amount"}

    if action == "close":
        entry_id = str(args.get("entry_id") or "").strip()
        if not entry_id:
            return {"error": "Which entry? Find it first, then close it by its id."}
        done = str(args.get("status") or "done").strip().lower() != "open"
        try:
            changed = log.close(entry_id, done)
        except Exception as exc:
            logger.warning("could not close a log entry: %s", type(exc).__name__)
            return {"error": "That entry could not be updated."}
        return {"closed": changed} if changed else {"error": "No entry of theirs has that id."}

    return {"error": "action must be add, find, total or close."}
