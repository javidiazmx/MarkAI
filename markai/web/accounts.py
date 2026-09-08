"""Two questions free, then a free account. The account is the point.

A landlord who asked two real questions and got two real answers has seen the thing work,
which is the moment they will trade a name for the third. So the wall goes there and not at
the door.

What this is, said plainly: a signup form, not a login. There is no password, so nothing
here proves anyone is who they say. Someone who clears their browser storage gets a new id
and two more free questions. That is the normal shape of a lead wall and it is fine for
what it does, but it is not access control and must never be treated as any.

The fields are personal data: a name, an email, a phone, a neighborhood. They live in
``data/accounts.db`` on the operator's own machine, they are never logged, and they go
nowhere except into that file. Anyone running this owes their landlords the truth about
that, which is why the page says it above the form.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_NAME_CHARS = 80
MAX_EMAIL_CHARS = 200
MAX_PHONE_CHARS = 30
MAX_NEIGHBORHOOD_CHARS = 80
MIN_PHONE_DIGITS = 10

# Deliberately loose. An address is either theirs or it is not, and a regex is not the
# thing that finds out; this only catches a typo like a missing @ or a trailing comma.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
_DIGITS = re.compile(r"\d")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    browser_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    email        TEXT NOT NULL,
    phone        TEXT NOT NULL,
    neighborhood TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    browser_id TEXT PRIMARY KEY,
    questions  INTEGER NOT NULL DEFAULT 0
);
"""


class SignupError(ValueError):
    """The form as filled in cannot be accepted, with a reason to show the landlord."""


@dataclass
class Account:
    """One landlord who signed up."""

    name: str
    email: str
    phone: str
    neighborhood: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "neighborhood": self.neighborhood,
        }


def _clean(value: Any, limit: int) -> str:
    flat = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    return re.sub(r"\s+", " ", flat).strip()[:limit]


def parse(raw: dict[str, Any]) -> Account:
    """Validate the form. Every message names the field, so it can be shown as it is."""
    name = _clean(raw.get("name"), MAX_NAME_CHARS)
    if len(name) < 2:
        raise SignupError("Tell us your name.")
    email = _clean(raw.get("email"), MAX_EMAIL_CHARS)
    if not _EMAIL.match(email):
        raise SignupError("That email address does not look right.")
    phone = _clean(raw.get("phone"), MAX_PHONE_CHARS)
    if len(_DIGITS.findall(phone)) < MIN_PHONE_DIGITS:
        raise SignupError("A phone number with the area code, please.")
    neighborhood = _clean(raw.get("neighborhood"), MAX_NEIGHBORHOOD_CHARS)
    if len(neighborhood) < 2:
        raise SignupError("Which neighborhood is your rental in?")
    return Account(name=name, email=email, phone=phone, neighborhood=neighborhood)


class Accounts:
    """Who signed up, and how many free questions each browser has spent."""

    def __init__(self, path: Path, free_questions: int = 2) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.free_questions = max(int(free_questions), 0)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def get(self, browser_id: str) -> Account | None:
        if not browser_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT name, email, phone, neighborhood FROM accounts WHERE browser_id = ?",
                (browser_id,),
            ).fetchone()
        if row is None:
            return None
        return Account(
            name=row["name"],
            email=row["email"],
            phone=row["phone"],
            neighborhood=row["neighborhood"],
        )

    def create(self, browser_id: str, raw: dict[str, Any]) -> Account:
        """Save the signup. Raises ``SignupError`` when the form is not usable."""
        if not browser_id:
            raise SignupError("This browser has no id, so there is nowhere to save it.")
        account = parse(raw)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO accounts (browser_id, name, email, phone,"
                " neighborhood, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    browser_id,
                    account.name,
                    account.email,
                    account.phone,
                    account.neighborhood,
                    time.time(),
                ),
            )
        # Never the fields themselves, here or anywhere else.
        logger.info("account created (browser %s…)", browser_id[:6])
        return account

    def all(self, limit: int | None = None) -> list[tuple[Account, float]]:
        """Every signup, newest first, with when it happened. For `mark accounts`."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, email, phone, neighborhood, created_at FROM accounts"
                " ORDER BY created_at DESC" + (" LIMIT ?" if limit else ""),
                (limit,) if limit else (),
            ).fetchall()
        return [
            (
                Account(
                    name=r["name"],
                    email=r["email"],
                    phone=r["phone"],
                    neighborhood=r["neighborhood"],
                ),
                float(r["created_at"]),
            )
            for r in rows
        ]

    def questions_used(self, browser_id: str) -> int:
        if not browser_id:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT questions FROM usage WHERE browser_id = ?", (browser_id,)
            ).fetchone()
        return int(row["questions"]) if row else 0

    def count_question(self, browser_id: str) -> int:
        """Count one answered question. Kept apart from the saved conversations on purpose:
        deleting a conversation must not hand back a free question."""
        if not browser_id:
            return 0
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO usage (browser_id, questions) VALUES (?, 1)"
                    " ON CONFLICT(browser_id) DO UPDATE SET questions = questions + 1",
                    (browser_id,),
                )
                row = self._conn.execute(
                    "SELECT questions FROM usage WHERE browser_id = ?", (browser_id,)
                ).fetchone()
            return int(row["questions"]) if row else 0
        except sqlite3.Error as exc:
            # A counter that cannot be written is not a reason to lose the answer.
            logger.warning("could not count a question: %s", exc)
            return 0

    def needs_signup(self, browser_id: str) -> bool:
        """True when the next question needs an account first."""
        if self.get(browser_id) is not None:
            return False
        return self.questions_used(browser_id) >= self.free_questions

    def free_left(self, browser_id: str) -> int:
        if self.get(browser_id) is not None:
            return 0
        return max(self.free_questions - self.questions_used(browser_id), 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
