"""What the landlord owns, so answers can be about their buildings instead of buildings.

"Do I have to give 30 days or 60?" depends on how long the tenant has been there. "Is that
boiler worth fixing?" depends on how many units it heats. A landlord who has already told
Jay about their six flat should not have to describe it again in every conversation.

Kept on the operator's own disk, keyed by the same owner as the conversation list - the
account when someone is signed in, the browser when they are not - and never sent anywhere
except into the question the landlord is asking. An address is the kind of thing a landlord
expects to stay on their own machine. Deleting a property deletes the row.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_PROPERTIES = 20
MAX_LABEL_CHARS = 80
MAX_CITY_CHARS = 60
MAX_NOTES_CHARS = 400
MAX_UNITS = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    label       TEXT NOT NULL,
    units       INTEGER,
    city        TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS properties_by_owner ON properties(owner_id, created_at);
"""


class PropertyError(ValueError):
    """The property as typed cannot be saved, with a reason worth showing."""


def _clean(text: Any, limit: int) -> str:
    """One line, no control characters, trimmed to a length the prompt can carry."""
    flat = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))
    return re.sub(r"\s+", " ", flat).strip()[:limit]


@dataclass
class Property:
    """One building, as the landlord described it."""

    id: str
    label: str
    units: int | None = None
    city: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "units": self.units,
            "city": self.city,
            "notes": self.notes,
        }

    def one_line(self) -> str:
        """How it reads in a handoff: "2145 W Division, 6 units, Chicago"."""
        bits = [self.label]
        if self.units:
            bits.append(f"{self.units} unit{'s' if self.units != 1 else ''}")
        if self.city:
            bits.append(self.city)
        line = ", ".join(bits)
        return f"{line} ({self.notes})" if self.notes else line


def parse(raw: dict[str, Any]) -> Property:
    """Validate one property as typed into the page. Raises ``PropertyError``."""
    label = _clean(raw.get("label"), MAX_LABEL_CHARS)
    if not label:
        raise PropertyError("Give the property a name or an address.")
    units_raw = raw.get("units")
    units: int | None = None
    if units_raw not in (None, "", "0"):
        try:
            units = int(float(units_raw))
        except (TypeError, ValueError) as exc:
            raise PropertyError(f"{label}: units has to be a number.") from exc
        if units < 1 or units > MAX_UNITS:
            raise PropertyError(f"{label}: units has to be between 1 and {MAX_UNITS}.")
    return Property(
        id=raw.get("id") or uuid.uuid4().hex,
        label=label,
        units=units,
        city=_clean(raw.get("city"), MAX_CITY_CHARS),
        notes=_clean(raw.get("notes"), MAX_NOTES_CHARS),
    )


class Portfolio:
    """SQLite-backed list of properties per owner."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate("properties")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _migrate(self, table: str) -> None:
        """Rename a pre-account ``browser_id`` column and prefix the rows it holds.

        Identity used to be the browser. It is now an owner, which is a browser while
        nobody is signed in and an account once someone is, so the old rows keep working by
        becoming ``browser:<id>``. Nothing is dropped.
        """
        listing = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if table not in {row[0] for row in listing}:
            return
        columns = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if "owner_id" in columns or "browser_id" not in columns:
            return
        with self._conn:
            self._conn.execute(f"ALTER TABLE {table} RENAME COLUMN browser_id TO owner_id")
            self._conn.execute(
                f"UPDATE {table} SET owner_id = 'browser:' || owner_id"
                " WHERE owner_id NOT LIKE 'browser:%' AND owner_id NOT LIKE 'account:%'"
            )
        logger.info("%s now keys on owner_id", table)

    def list(self, owner_id: str) -> list[Property]:
        if not owner_id:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, label, units, city, notes FROM properties WHERE owner_id = ?"
                " ORDER BY created_at LIMIT ?",
                (owner_id, MAX_PROPERTIES),
            ).fetchall()
        return [
            Property(
                id=r["id"], label=r["label"], units=r["units"], city=r["city"], notes=r["notes"]
            )
            for r in rows
        ]

    def reassign(self, old_owner: str, new_owner: str) -> int:
        """Move rows from one owner to another, for the moment a landlord signs up.

        They asked two questions and then created an account; those two conversations are
        theirs, and a sidebar that empties itself at signup would be a bug. Only called
        for the browser the signup came from, and never on a later sign-in, where the
        anonymous rows could belong to whoever used that machine before.
        """
        if not old_owner or not new_owner or old_owner == new_owner:
            return 0
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE properties SET owner_id = ? WHERE owner_id = ?", (new_owner, old_owner)
            )
        return cursor.rowcount

    def add(self, owner_id: str, raw: dict[str, Any]) -> Property:
        """Save one property. Raises ``PropertyError`` when it cannot be saved."""
        if not owner_id:
            raise PropertyError("There is nobody to save this for.")
        item = parse(raw)
        with self._lock, self._conn:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM properties WHERE owner_id = ?", (owner_id,)
            ).fetchone()[0]
            if count >= MAX_PROPERTIES:
                raise PropertyError(
                    f"{MAX_PROPERTIES} properties is the limit. Every one of them rides along "
                    f"with each question, so the list has to stay short."
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO properties (id, owner_id, label, units, city, notes,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    owner_id,
                    item.label,
                    item.units,
                    item.city,
                    item.notes,
                    time.time(),
                ),
            )
        return item

    def delete(self, owner_id: str, property_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM properties WHERE id = ? AND owner_id = ?",
                (property_id, owner_id),
            )
        return cursor.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
