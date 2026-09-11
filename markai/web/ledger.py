"""What is actually going on at the property: bills, expenses, maintenance, visits.

A landlord's real question is rarely general. It is "did I already pay that water bill?",
"how much have I put into 2145 this year?", "when did the plumber last come out?" - and the
answer is not in a blog post, it is in what they did. So Jay keeps their log for them: they
mention something in passing, it gets written down, and months later they can ask about it
in plain language instead of digging through a shoebox.

The shape of an entry is deliberately small - kind, what, how much, who, when, open or done
- because anything wider becomes property management software, and this is a landlord
talking to an advisor. The six kinds cover what people actually say out loud.

Rules this holds to:

- **Their disk, their data.** Same store, same owner id as the conversation list: the
  account when they are signed in, the browser when they are not. It goes into their own
  questions and nowhere else, and deleting an entry deletes the row.
- **Jay writes only what they said.** The tool that reaches this can add, search, total and
  close. It cannot delete: a model quietly dropping a landlord's records is not a risk
  worth taking for the convenience, so deleting stays a button on the page.
- **Nothing is logged about the log.** An address, a vendor, a repair and an amount are
  their business. The module records that an entry was saved and its kind, never a value.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# What a landlord says out loud. "Expense" is money out with nothing owed, "bill" is money
# owed or paid on a recurring account, "income" is rent in, "maintenance" is something
# wrong with the building, "visit" is somebody who came out, "note" is everything else.
KINDS = ("expense", "bill", "income", "maintenance", "visit", "note")

# Nobody says "log an expense". They say "the plumber invoiced me". The model picks a word
# and it should not fail because it picked a synonym.
SYNONYMS = {
    "repair": "maintenance",
    "repairs": "maintenance",
    "issue": "maintenance",
    "problem": "maintenance",
    "work": "maintenance",
    "workorder": "maintenance",
    "work_order": "maintenance",
    "invoice": "bill",
    "payment": "bill",
    "tax": "bill",
    "taxes": "bill",
    "insurance": "bill",
    "utility": "bill",
    "mortgage": "bill",
    "rent": "income",
    "deposit": "income",
    "showing": "visit",
    "inspection": "visit",
    "walkthrough": "visit",
    "vendor": "visit",
    "cost": "expense",
    "capex": "expense",
    "purchase": "expense",
}

OPEN = "open"
DONE = "done"

MAX_WHAT_CHARS = 240
MAX_VENDOR_CHARS = 80
MAX_AMOUNT = 10_000_000
MAX_ENTRIES_PER_OWNER = 5000
DEFAULT_LIMIT = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS log_entries (
    id           TEXT PRIMARY KEY,
    owner_id     TEXT NOT NULL,
    property_id  TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL,
    what         TEXT NOT NULL,
    amount       REAL,
    vendor       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT '',
    happened_on  TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS log_by_owner
    ON log_entries(owner_id, happened_on DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS log_by_property ON log_entries(owner_id, property_id);
"""


class LogError(ValueError):
    """The entry as given cannot be saved, with a reason worth showing."""


# A model's own tool-call syntax, arriving inside a tool argument. "Log $200 on locks"
# came back with the vendor set to `</parameter> <parameter name="date">today`, and it went
# into the landlord's records looking like that. Whatever produced it, a value carrying
# machinery is not something a person said, so it is dropped rather than tidied: cleaning
# the tags off that example would have left the word "today" sitting in the vendor field,
# which is a quieter kind of wrong.
_MACHINERY = re.compile(
    r"(antml:|</?\s*(?:parameter|invoke|function_calls?|tool_use|tool_result)\b)",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^<>]{0,300}>")


def _clean(text: Any, limit: int) -> str:
    """One line of what somebody actually wrote, or nothing."""
    raw = str(text or "")
    if _MACHINERY.search(raw):
        logger.warning("dropped a log field that arrived carrying tool-call syntax")
        return ""
    flat = _TAG.sub(" ", raw)
    flat = re.sub(r"[\x00-\x1f\x7f]", " ", flat)
    return re.sub(r"\s+", " ", flat).strip()[:limit]


def normalise_kind(raw: Any) -> str:
    """Map whatever word arrived onto one of the five, defaulting to a note."""
    word = re.sub(r"[^a-z_]", "", str(raw or "").strip().lower())
    if word in KINDS:
        return word
    return SYNONYMS.get(word, "note")


_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_US = re.compile(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$")
_TODAY = {"today", "hoy", "now", "ahora"}
_YESTERDAY = {"yesterday", "ayer"}


def parse_day(raw: Any, today: date | None = None) -> date:
    """The day something happened, from what a person or a model would write.

    ISO first because that is what the tool asks for, then the American order the owner
    would type by hand, then "today" and "yesterday", which is how anyone talks about a
    repair that happened this morning. Anything unreadable is today: refusing to file an
    expense over the date format would lose the expense.
    """
    now = today or date.today()
    text = str(raw or "").strip().lower()
    if not text or text in _TODAY:
        return now
    if text in _YESTERDAY:
        return now - timedelta(days=1)
    iso = _ISO.match(text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return now
    us = _US.match(text)
    if us:
        year = us.group(3)
        full = int(year) if year and len(year) == 4 else (2000 + int(year) if year else now.year)
        try:
            return date(full, int(us.group(1)), int(us.group(2)))
        except ValueError:
            return now
    return now


def parse_amount(raw: Any) -> float | None:
    """A dollar figure from "$8,000", "8000" or "8k". None when there is no number."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).strip().lower().replace(",", "").replace("$", "")
        multiplier = 1000.0 if text.endswith("k") else 1.0
        text = text[:-1] if text.endswith("k") else text
        try:
            value = float(text) * multiplier
        except ValueError:
            return None
    if value < 0:
        raise LogError("An amount cannot be negative. Log it as income if money came in.")
    if value > MAX_AMOUNT:
        raise LogError(f"${value:,.0f} looks like a typo. The limit is ${MAX_AMOUNT:,.0f}.")
    return round(value, 2)


@dataclass
class Entry:
    """One thing that happened, as the landlord described it."""

    id: str
    kind: str
    what: str
    amount: float | None = None
    vendor: str = ""
    status: str = ""
    happened_on: str = ""
    property_id: str = ""
    property_label: str = ""
    created_at: float = 0.0

    def money(self) -> str:
        return f"${self.amount:,.0f}" if self.amount else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "what": self.what,
            "amount": self.amount,
            "vendor": self.vendor,
            "status": self.status,
            "happened_on": self.happened_on,
            "property_id": self.property_id,
            "property_label": self.property_label,
        }

    def one_line(self) -> str:
        """How it reads back: "Aug 30 · expense · New boiler · $8,000 · ABC Heating"."""
        bits = [self.happened_on or "", self.kind, self.what]
        if self.amount:
            bits.append(self.money())
        if self.vendor:
            bits.append(self.vendor)
        if self.property_label:
            bits.append(self.property_label)
        if self.status == OPEN:
            bits.append("still open")
        return " · ".join(bit for bit in bits if bit)


def parse(raw: dict[str, Any], today: date | None = None) -> Entry:
    """Validate one entry, however it arrived. Raises ``LogError``."""
    what = _clean(raw.get("what") or raw.get("note") or raw.get("description"), MAX_WHAT_CHARS)
    if not what:
        raise LogError("Say what happened, in a few words.")
    kind = normalise_kind(raw.get("kind"))
    status = str(raw.get("status", "") or "").strip().lower()
    if status not in (OPEN, DONE, ""):
        status = ""
    if not status and kind == "maintenance":
        # An issue is open until somebody says it is not. That default is the whole reason
        # a landlord can ask "what is still outstanding at the Berwyn place?".
        status = OPEN
    return Entry(
        id=_clean(raw.get("id"), 64) or uuid.uuid4().hex,
        kind=kind,
        what=what,
        amount=parse_amount(raw.get("amount")),
        vendor=_clean(raw.get("vendor") or raw.get("who"), MAX_VENDOR_CHARS),
        status=status,
        happened_on=parse_day(raw.get("date") or raw.get("happened_on"), today).isoformat(),
        property_id=_clean(raw.get("property_id"), 64),
    )


def resolve_property(properties: list[Any], text: Any) -> Any | None:
    """Which of their buildings a phrase means, or None.

    A landlord says "the Berwyn six flat" or "2145", never a row id. Matching is a plain
    substring both ways, because their label is already the words they use for it: an exact
    id first, then the label containing what they said or the other way round, then the
    city. No fuzzy scoring - guessing the wrong building would file an expense against the
    wrong property, which is worse than filing it against none.
    """
    wanted = _clean(text, 120).lower()
    if not wanted or not properties:
        return None
    for item in properties:
        if wanted == str(item.id).lower():
            return item
    for item in properties:
        label = str(item.label).lower()
        if wanted == label or wanted in label or (len(wanted) > 3 and label in wanted):
            return item
    for item in properties:
        if str(item.city).lower() and wanted == str(item.city).lower():
            return item
    return None


class Ledger:
    """SQLite-backed log of everything happening at an owner's properties."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- writing -------------------------------------------------------------------------

    def add(self, owner_id: str, raw: dict[str, Any], today: date | None = None) -> Entry:
        """Save one entry. Raises ``LogError`` when it cannot be saved."""
        if not owner_id:
            raise LogError("There is nobody to save this for.")
        item = parse(raw, today)
        with self._lock, self._conn:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM log_entries WHERE owner_id = ?", (owner_id,)
            ).fetchone()[0]
            if count >= MAX_ENTRIES_PER_OWNER:
                raise LogError(
                    f"That is {MAX_ENTRIES_PER_OWNER} entries, which is the limit here. "
                    f"A portfolio this active has outgrown a chat window and wants real "
                    f"bookkeeping."
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO log_entries (id, owner_id, property_id, kind, what,"
                " amount, vendor, status, happened_on, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    owner_id,
                    item.property_id,
                    item.kind,
                    item.what,
                    item.amount,
                    item.vendor,
                    item.status,
                    item.happened_on,
                    time.time(),
                ),
            )
        # Kind only. What it was, what it cost and where is theirs.
        logger.info("log entry saved: kind=%s", item.kind)
        return item

    def close(self, owner_id: str, entry_id: str, done: bool = True) -> bool:
        """Mark an open item done, or reopen it."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE log_entries SET status = ? WHERE id = ? AND owner_id = ?",
                (DONE if done else OPEN, entry_id, owner_id),
            )
        return cursor.rowcount > 0

    def delete(self, owner_id: str, entry_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM log_entries WHERE id = ? AND owner_id = ?", (entry_id, owner_id)
            )
        return cursor.rowcount > 0

    def reassign(self, old_owner: str, new_owner: str) -> int:
        """Move an anonymous log onto the account made from the same browser."""
        if not old_owner or not new_owner or old_owner == new_owner:
            return 0
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE log_entries SET owner_id = ? WHERE owner_id = ?", (new_owner, old_owner)
            )
        return cursor.rowcount

    # -- reading -------------------------------------------------------------------------

    def _rows(self, sql: str, params: tuple) -> list[Entry]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            Entry(
                id=row["id"],
                kind=row["kind"],
                # Cleaned on the way out as well, so a row written before this stopped
                # showing the landlord markup and stopped reaching the prompt.
                what=_clean(row["what"], MAX_WHAT_CHARS),
                amount=row["amount"],
                vendor=_clean(row["vendor"], MAX_VENDOR_CHARS),
                status=row["status"],
                happened_on=row["happened_on"],
                property_id=row["property_id"],
                created_at=float(row["created_at"]),
            )
            for row in rows
        ]

    def list(
        self,
        owner_id: str,
        property_id: str = "",
        kinds: tuple[str, ...] = (),
        status: str = "",
        since: date | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[Entry]:
        """Their log, most recent first, narrowed however the caller needs it."""
        if not owner_id:
            return []
        where = ["owner_id = ?"]
        params: list[Any] = [owner_id]
        if property_id:
            where.append("property_id = ?")
            params.append(property_id)
        if kinds:
            where.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if status:
            where.append("status = ?")
            params.append(status)
        if since:
            where.append("happened_on >= ?")
            params.append(since.isoformat())
        params.append(max(1, min(limit, MAX_ENTRIES_PER_OWNER)))
        return self._rows(
            "SELECT * FROM log_entries WHERE "
            + " AND ".join(where)
            + " ORDER BY happened_on DESC, created_at DESC LIMIT ?",
            tuple(params),
        )

    def find(self, owner_id: str, text: str, limit: int = DEFAULT_LIMIT) -> list[Entry]:
        """Search what they wrote. Plain substring, over what happened and who did it."""
        needle = _clean(text, 120)
        if not owner_id or not needle:
            return []
        like = f"%{needle.lower()}%"
        return self._rows(
            "SELECT * FROM log_entries WHERE owner_id = ? AND (LOWER(what) LIKE ?"
            " OR LOWER(vendor) LIKE ?) ORDER BY happened_on DESC, created_at DESC LIMIT ?",
            (owner_id, like, like, max(1, min(limit, MAX_ENTRIES_PER_OWNER))),
        )

    def open_items(self, owner_id: str, limit: int = DEFAULT_LIMIT) -> list[Entry]:
        """What is still outstanding, oldest first: the ones that have been waiting."""
        if not owner_id:
            return []
        return self._rows(
            "SELECT * FROM log_entries WHERE owner_id = ? AND status = ?"
            " ORDER BY happened_on ASC, created_at ASC LIMIT ?",
            (owner_id, OPEN, max(1, min(limit, MAX_ENTRIES_PER_OWNER))),
        )

    def totals(
        self,
        owner_id: str,
        property_id: str = "",
        since: date | None = None,
    ) -> dict[str, float]:
        """Money by kind over a window, plus ``out``, ``in`` and ``net``.

        Added up here rather than by the model: a landlord asking what they have spent on a
        building wants the number to be right, and arithmetic is not what a language model
        is for.
        """
        if not owner_id:
            return {}
        where = ["owner_id = ?", "amount IS NOT NULL"]
        params: list[Any] = [owner_id]
        if property_id:
            where.append("property_id = ?")
            params.append(property_id)
        if since:
            where.append("happened_on >= ?")
            params.append(since.isoformat())
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, SUM(amount) AS total FROM log_entries WHERE "
                + " AND ".join(where)
                + " GROUP BY kind",
                tuple(params),
            ).fetchall()
        by_kind = {row["kind"]: round(float(row["total"] or 0), 2) for row in rows}
        money_in = by_kind.get("income", 0.0)
        money_out = sum(value for kind, value in by_kind.items() if kind != "income")
        by_kind["in"] = round(money_in, 2)
        by_kind["out"] = round(money_out, 2)
        by_kind["net"] = round(money_in - money_out, 2)
        return by_kind

    def count(self, owner_id: str) -> int:
        if not owner_id:
            return 0
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM log_entries WHERE owner_id = ?", (owner_id,)
                ).fetchone()[0]
            )

    def close_db(self) -> None:
        with self._lock:
            self._conn.close()


class OwnerLog:
    """One landlord's log, already bound to them and to their buildings.

    The advisor is handed this rather than the store and an id, so nothing in the answer
    path can read or write another owner's rows even by mistake, and so a property can be
    named the way the landlord names it instead of by row id.
    """

    def __init__(self, ledger: Ledger, owner_id: str, properties: list[Any] | None = None) -> None:
        self._ledger = ledger
        self._owner = owner_id
        self._properties = list(properties or [])

    def _label(self, entry: Entry) -> Entry:
        for item in self._properties:
            if item.id == entry.property_id:
                entry.property_label = item.label
                break
        return entry

    def _labelled(self, entries: list[Entry]) -> list[Entry]:
        return [self._label(entry) for entry in entries]

    def property_id_for(self, text: Any) -> tuple[str, str]:
        """Resolve a phrase to (id, label). Empty when it matches nothing they own."""
        found = resolve_property(self._properties, text)
        return (found.id, found.label) if found else ("", "")

    def add(self, raw: dict[str, Any]) -> Entry:
        data = dict(raw)
        named = data.pop("property", "") or data.get("property_id", "")
        property_id, label = self.property_id_for(named)
        data["property_id"] = property_id
        saved = self._ledger.add(self._owner, data)
        saved.property_label = label
        return saved

    def find(
        self,
        search: str = "",
        kind: str = "",
        property_name: str = "",
        since_days: int = 0,
        status: str = "",
        limit: int = 12,
    ) -> list[Entry]:
        if search:
            return self._labelled(self._ledger.find(self._owner, search, limit))
        property_id, _ = self.property_id_for(property_name)
        kinds = (normalise_kind(kind),) if kind else ()
        since = date.today() - timedelta(days=since_days) if since_days > 0 else None
        return self._labelled(
            self._ledger.list(
                self._owner,
                property_id=property_id,
                kinds=kinds,
                status=status,
                since=since,
                limit=limit,
            )
        )

    def recent(self, limit: int = 8) -> list[Entry]:
        return self._labelled(self._ledger.list(self._owner, limit=limit))

    def open_items(self, limit: int = 6) -> list[Entry]:
        return self._labelled(self._ledger.open_items(self._owner, limit=limit))

    def totals(self, property_name: str = "", since_days: int = 0) -> dict[str, float]:
        property_id, _ = self.property_id_for(property_name)
        since = date.today() - timedelta(days=since_days) if since_days > 0 else None
        return self._ledger.totals(self._owner, property_id=property_id, since=since)

    def close(self, entry_id: str, done: bool = True) -> bool:
        return self._ledger.close(self._owner, entry_id, done)

    def count(self) -> int:
        return self._ledger.count(self._owner)
