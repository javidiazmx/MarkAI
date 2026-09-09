"""Who a landlord is, and how many free questions they have left.

Two questions get answered, then the page asks for a name, an email and a phone. That form
is the lead, and it is the only thing standing between a stranger and the rest of Jay.

There is no password, on purpose. A password does not verify an email, so it buys no lead
quality; what it buys is a second device, and it costs conversion at the exact moment
somebody decides. So identity here is **the device**: filling the form issues an HttpOnly
cookie, that device is remembered, and the wall does not come back. A landlord on a second
device fills the short form again, which takes fifteen seconds and is not worth a password
and its support burden.

That also settles a question the password version had to be careful about: a device never
inherits another device's conversations, because nothing here claims to prove who anyone
is. What one person typed stays where they typed it.

The fields are somebody's personal data. They live in ``data/accounts.db`` on the
operator's own machine, they never reach the log, and the only place they go on purpose is
the CRM, through the queue in ``crm.py``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
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

SESSION_DAYS = 30

# Deliberately loose. Whether an address is theirs is not a regex's business; this catches
# a missing @ and a trailing comma.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
_DIGITS = re.compile(r"\d")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id           TEXT PRIMARY KEY,
    email        TEXT NOT NULL,
    name         TEXT NOT NULL,
    phone        TEXT NOT NULL,
    neighborhood TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS accounts_by_email ON accounts(email);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_by_account ON sessions(account_id);
CREATE TABLE IF NOT EXISTS usage (
    owner_id  TEXT PRIMARY KEY,
    questions INTEGER NOT NULL DEFAULT 0
);
"""


class SignupError(ValueError):
    """The form as filled in cannot be used, with a reason worth showing."""


@dataclass
class Account:
    """One landlord's contact details, as they typed them."""

    id: str
    email: str
    name: str
    phone: str
    neighborhood: str = ""

    @property
    def owner_id(self) -> str:
        """What the conversation and property stores key on for this device."""
        return f"account:{self.id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "name": self.name,
            "phone": self.phone,
            "neighborhood": self.neighborhood,
        }


def anonymous_owner(browser_id: str) -> str:
    """The owner id for someone who has not filled the form yet."""
    return f"browser:{browser_id}" if browser_id else ""


def _clean(value: Any, limit: int) -> str:
    flat = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    return re.sub(r"\s+", " ", flat).strip()[:limit]


def normalize_email(value: Any) -> str:
    """Lowercased and trimmed, so the same address is the same address."""
    return _clean(value, MAX_EMAIL_CHARS).lower()


def parse(raw: dict[str, Any]) -> Account:
    """Validate the form. Every message names its field, so it can be shown as it is."""
    name = _clean(raw.get("name"), MAX_NAME_CHARS)
    if len(name) < 2:
        raise SignupError("Tell us your name.")
    email = normalize_email(raw.get("email"))
    if not _EMAIL.match(email):
        raise SignupError("That email address does not look right.")
    phone = _clean(raw.get("phone"), MAX_PHONE_CHARS)
    if len(_DIGITS.findall(phone)) < MIN_PHONE_DIGITS:
        raise SignupError("A phone number with the area code, please.")
    return Account(
        id=secrets.token_hex(16),
        email=email,
        name=name,
        phone=phone,
        neighborhood=_clean(raw.get("neighborhood"), MAX_NEIGHBORHOOD_CHARS),
    )


class Accounts:
    """Contact details, one remembered device each, and the free-question count."""

    def __init__(
        self, path: Path, free_questions: int = 2, session_days: int = SESSION_DAYS
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.free_questions = max(int(free_questions), 0)
        self.session_days = max(int(session_days), 1)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _migrate(self) -> None:
        """Carry two older shapes forward rather than dropping what they hold.

        The first version keyed a signup by browser id; the second added a password. Both
        hold real contact details, so an old table is moved to ``signups_v1`` where the
        owner can still read it and `mark accounts` says how many are there.
        """
        listing = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in listing}
        if "accounts" in tables and "signups_v1" not in tables:
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(accounts)")}
            # Either the browser-keyed first version, or the password one whose UNIQUE
            # email and password column no longer fit.
            if "browser_id" in columns or "password_hash" in columns:
                self._conn.execute("ALTER TABLE accounts RENAME TO signups_v1")
                self._conn.execute("DROP TABLE IF EXISTS sessions")
                self._conn.commit()
                logger.info("kept the earlier signups as signups_v1")
        if "usage" in tables:
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(usage)")}
            if "owner_id" not in columns and "browser_id" in columns:
                self._conn.execute("ALTER TABLE usage RENAME COLUMN browser_id TO owner_id")
                # The old rows counted a bare browser id; the new key is prefixed.
                self._conn.execute(
                    "UPDATE usage SET owner_id = 'browser:' || owner_id"
                    " WHERE owner_id NOT LIKE 'browser:%' AND owner_id NOT LIKE 'account:%'"
                )
                self._conn.commit()

    # --- the form ------------------------------------------------------------------------

    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> Account:
        return Account(
            id=row["id"],
            email=row["email"],
            name=row["name"],
            phone=row["phone"],
            neighborhood=row["neighborhood"],
        )

    def create(self, raw: dict[str, Any]) -> tuple[Account, str]:
        """Save the contact details and remember this device. Returns it and the token.

        A repeat email is not refused. Without a password there is nothing to check, so a
        landlord on a second device fills the form again and gets their own row rather than
        being handed whatever the first device had.
        """
        account = parse(raw)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO accounts (id, email, name, phone, neighborhood, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    account.id,
                    account.email,
                    account.name,
                    account.phone,
                    account.neighborhood,
                    time.time(),
                ),
            )
        # The id, never the fields.
        logger.info("signup recorded (%s…)", account.id[:6])
        return account, self.start_session(account.id)

    def seen_before(self, email: str) -> bool:
        """Whether this address has already been through the form.

        Used to keep one person filling the form on their phone and their laptop from
        landing in the CRM twice.
        """
        needle = normalize_email(email)
        if not needle:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM accounts WHERE email = ? LIMIT 1", (needle,)
            ).fetchone()
        return row is not None

    def all(self, limit: int | None = None) -> list[tuple[Account, float]]:
        """Every signup, newest first, with when it happened. For `mark accounts`."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM accounts ORDER BY created_at DESC" + (" LIMIT ?" if limit else ""),
                (limit,) if limit else (),
            ).fetchall()
        return [(self._row_to_account(row), float(row["created_at"])) for row in rows]

    def legacy_signups(self) -> int:
        """How many signups from an earlier shape are sitting in signups_v1."""
        try:
            with self._lock:
                row = self._conn.execute("SELECT COUNT(*) FROM signups_v1").fetchone()
            return int(row[0])
        except sqlite3.Error:
            return 0

    # --- the remembered device -----------------------------------------------------------

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def start_session(self, account_id: str) -> str:
        """A fresh token. Only its hash is stored, so the database holds no usable cookie."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sessions (token_hash, account_id, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (self._token_hash(token), account_id, now, now + self.session_days * 86400),
            )
            self._conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        return token

    def account_for_token(self, token: str | None) -> Account | None:
        """Whose device this is, or None. An expired session is deleted as it is found."""
        if not token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT a.*, s.expires_at FROM sessions s JOIN accounts a ON a.id = s.account_id"
                " WHERE s.token_hash = ?",
                (self._token_hash(token),),
            ).fetchone()
        if row is None:
            return None
        if float(row["expires_at"]) < time.time():
            self.end_session(token)
            return None
        return self._row_to_account(row)

    def end_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (self._token_hash(token),)
            )

    # --- free questions ------------------------------------------------------------------

    def questions_used(self, owner_id: str) -> int:
        if not owner_id:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT questions FROM usage WHERE owner_id = ?", (owner_id,)
            ).fetchone()
        return int(row["questions"]) if row else 0

    def count_question(self, owner_id: str) -> int:
        """Count one answered question. Kept apart from the saved conversations on purpose:
        deleting a conversation must not hand back a free question."""
        if not owner_id:
            return 0
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO usage (owner_id, questions) VALUES (?, 1)"
                    " ON CONFLICT(owner_id) DO UPDATE SET questions = questions + 1",
                    (owner_id,),
                )
                row = self._conn.execute(
                    "SELECT questions FROM usage WHERE owner_id = ?", (owner_id,)
                ).fetchone()
            return int(row["questions"]) if row else 0
        except sqlite3.Error as exc:
            # A counter that cannot be written is not a reason to lose the answer.
            logger.warning("could not count a question: %s", exc)
            return 0

    def needs_signup(self, owner_id: str) -> bool:
        """True when the next question needs the form first. A known device, never."""
        if not owner_id or owner_id.startswith("account:"):
            return False
        return self.questions_used(owner_id) >= self.free_questions

    def free_left(self, owner_id: str) -> int:
        if not owner_id or owner_id.startswith("account:"):
            return 0
        return max(self.free_questions - self.questions_used(owner_id), 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
