"""Accounts with a password. The email is the username.

Two questions get answered for free, then a landlord creates an account and keeps going.
This module owns everything about that: the password, the sign-in session, and the count of
free questions a browser has spent before signing up.

What is deliberate here:

- **Passwords are never stored, only scrypt hashes.** The cost parameters live inside each
  stored hash, so raising them later leaves existing accounts able to sign in.
- **The session is an HttpOnly cookie** holding a random token, and only the token's sha256
  is stored. A stolen database yields no usable session, and no script on the page can read
  the cookie even if something got injected into it.
- **Sign-in tells an unknown email and a wrong password apart to nobody.** Same message,
  and a real hash is computed either way so the timing does not answer the question the
  message refuses to. Repeated failures for one email are throttled.
- **Identity is the account, not the browser.** Signed in, a landlord's conversations and
  properties follow them to another machine; anonymous, they belong to the browser. Both
  are an *owner id*, which is what the other stores key on.

What this still does not do, and what that means: there is no email delivery, so there is
no verification and no self-service password reset. An address is whatever they typed, and
a landlord who forgets their password needs the owner to run
``mark accounts reset-password``. Anyone building on this should know that before they
treat an address here as proof of anything.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
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

MIN_PASSWORD_CHARS = 8
# Long enough for any passphrase, short enough that nobody can make the server chew on a
# megabyte of input. scrypt's cost is set by the parameters below, not by the length.
MAX_PASSWORD_CHARS = 200

# scrypt at n=2**14 is about 40ms and 16MB per hash on ordinary hardware: slow enough to
# matter to an attacker with the database, fast enough that a sign-in feels instant.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

SESSION_DAYS = 30
FAILED_ATTEMPTS_BEFORE_THROTTLE = 5
THROTTLE_SECONDS = 60.0

# Deliberately loose. Whether an address is theirs is not a regex's business; this catches
# a missing @ and a trailing comma.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
_DIGITS = re.compile(r"\d")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    phone         TEXT NOT NULL,
    neighborhood  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL,
    last_login_at REAL
);
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


class LoginError(ValueError):
    """Sign-in failed. The message is deliberately the same for every cause."""


WRONG_CREDENTIALS = "That email and password do not match an account."


# --- passwords ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """``scrypt$n$r$p$salt$hash``, so the cost used is remembered with the hash."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=132 * SCRYPT_N * SCRYPT_R,
    )
    parts = (
        "scrypt",
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )
    return "$".join(str(part) for part in parts)


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash. A malformed hash is a failure, not a crash."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=132 * int(n) * int(r),
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(digest, expected)


# A hash of nothing anybody knows, used to spend the same time on an unknown email as on a
# real one. Built once, because building it is the expensive part.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


# --- shapes ------------------------------------------------------------------------------


@dataclass
class Account:
    """One landlord's account. The password never leaves the store."""

    id: str
    email: str
    name: str
    phone: str
    neighborhood: str

    @property
    def owner_id(self) -> str:
        """What the conversation and property stores key on while this account is signed in."""
        return f"account:{self.id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "name": self.name,
            "phone": self.phone,
            "neighborhood": self.neighborhood,
        }


def anonymous_owner(browser_id: str) -> str:
    """The owner id for someone who has not signed in."""
    return f"browser:{browser_id}" if browser_id else ""


# --- validation --------------------------------------------------------------------------


def _clean(value: Any, limit: int) -> str:
    flat = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    return re.sub(r"\s+", " ", flat).strip()[:limit]


def normalize_email(value: Any) -> str:
    """Lowercased and trimmed, because a username people type has to match every time."""
    return _clean(value, MAX_EMAIL_CHARS).lower()


def check_password(password: Any, email: str = "") -> str:
    """Validate a new password. Length is the rule that matters; the rest is a trap."""
    raw = str(password or "")
    if "\n" in raw or "\r" in raw:
        raise SignupError("A password cannot contain a line break.")
    if len(raw) < MIN_PASSWORD_CHARS:
        raise SignupError(f"Use at least {MIN_PASSWORD_CHARS} characters for the password.")
    if len(raw) > MAX_PASSWORD_CHARS:
        raise SignupError(f"That password is longer than {MAX_PASSWORD_CHARS} characters.")
    if email and raw.strip().lower() == email:
        raise SignupError("The password cannot be your email address.")
    return raw


def parse(raw: dict[str, Any]) -> tuple[Account, str]:
    """Validate the signup form. Returns the account and the password to hash."""
    name = _clean(raw.get("name"), MAX_NAME_CHARS)
    if len(name) < 2:
        raise SignupError("Tell us your name.")
    email = normalize_email(raw.get("email"))
    if not _EMAIL.match(email):
        raise SignupError("That email address does not look right.")
    phone = _clean(raw.get("phone"), MAX_PHONE_CHARS)
    if len(_DIGITS.findall(phone)) < MIN_PHONE_DIGITS:
        raise SignupError("A phone number with the area code, please.")
    neighborhood = _clean(raw.get("neighborhood"), MAX_NEIGHBORHOOD_CHARS)
    if len(neighborhood) < 2:
        raise SignupError("Which neighborhood is your rental in?")
    password = check_password(raw.get("password"), email)
    return (
        Account(
            id=secrets.token_hex(16),
            email=email,
            name=name,
            phone=phone,
            neighborhood=neighborhood,
        ),
        password,
    )


# --- the store ---------------------------------------------------------------------------


class Accounts:
    """Accounts, sign-in sessions, and the free-question count per owner."""

    def __init__(
        self, path: Path, free_questions: int = 2, session_days: int = SESSION_DAYS
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.free_questions = max(int(free_questions), 0)
        self.session_days = max(int(session_days), 1)
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _migrate(self) -> None:
        """Move a pre-password database aside instead of throwing away what it holds.

        The first version keyed a signup by browser id and had no password, so those rows
        cannot become accounts: there is nothing to sign in with. They are kept as
        ``signups_v1`` so the owner still has the leads, and `mark accounts` says so.
        """
        listing = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in listing}
        if "accounts" in tables:
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(accounts)")}
            if "password_hash" not in columns and "signups_v1" not in tables:
                self._conn.execute("ALTER TABLE accounts RENAME TO signups_v1")
                self._conn.commit()
                logger.info("kept the pre-password signups as signups_v1")
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

    # --- accounts ------------------------------------------------------------------------

    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> Account:
        return Account(
            id=row["id"],
            email=row["email"],
            name=row["name"],
            phone=row["phone"],
            neighborhood=row["neighborhood"],
        )

    def by_email(self, email: str) -> Account | None:
        needle = normalize_email(email)
        if not needle:
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM accounts WHERE email = ?", (needle,)).fetchone()
        return self._row_to_account(row) if row else None

    def create(self, raw: dict[str, Any]) -> tuple[Account, str]:
        """Create an account and a signed-in session. Returns the account and its token."""
        account, password = parse(raw)
        stored = hash_password(password)
        with self._lock, self._conn:
            taken = self._conn.execute(
                "SELECT 1 FROM accounts WHERE email = ?", (account.email,)
            ).fetchone()
            if taken:
                raise SignupError("There is already an account with that email. Sign in instead.")
            self._conn.execute(
                "INSERT INTO accounts (id, email, name, phone, neighborhood, password_hash,"
                " created_at, last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account.id,
                    account.email,
                    account.name,
                    account.phone,
                    account.neighborhood,
                    stored,
                    time.time(),
                    time.time(),
                ),
            )
        # The id, never the fields.
        logger.info("account created (%s…)", account.id[:6])
        return account, self.start_session(account.id)

    def sign_in(self, email: str, password: str) -> tuple[Account, str]:
        """Check a password and open a session. Every failure raises the same message."""
        needle = normalize_email(email)
        self._check_throttle(needle)
        with self._lock:
            row = self._conn.execute("SELECT * FROM accounts WHERE email = ?", (needle,)).fetchone()
        # An unknown email still costs a hash, so the wait does not answer the question the
        # message refuses to.
        stored = row["password_hash"] if row else _DUMMY_HASH
        if not verify_password(str(password or ""), stored) or row is None:
            self._note_failure(needle)
            raise LoginError(WRONG_CREDENTIALS)
        self._clear_failures(needle)
        account = self._row_to_account(row)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE accounts SET last_login_at = ? WHERE id = ?", (time.time(), account.id)
            )
        return account, self.start_session(account.id)

    def set_password(self, email: str, password: str) -> None:
        """Reset a password from the terminal. There is no self-service route to this."""
        account = self.by_email(email)
        if account is None:
            raise SignupError(f"No account with the email {normalize_email(email)!r}.")
        stored = hash_password(check_password(password, account.email))
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE accounts SET password_hash = ? WHERE id = ?", (stored, account.id)
            )
            # Every existing session goes: a reset is also how you throw someone out.
            self._conn.execute("DELETE FROM sessions WHERE account_id = ?", (account.id,))

    def all(self, limit: int | None = None) -> list[tuple[Account, float]]:
        """Every account, newest first, with when it was created. For `mark accounts`."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM accounts ORDER BY created_at DESC" + (" LIMIT ?" if limit else ""),
                (limit,) if limit else (),
            ).fetchall()
        return [(self._row_to_account(row), float(row["created_at"])) for row in rows]

    def legacy_signups(self) -> int:
        """How many pre-password signups are still sitting in signups_v1."""
        try:
            with self._lock:
                row = self._conn.execute("SELECT COUNT(*) FROM signups_v1").fetchone()
            return int(row[0])
        except sqlite3.Error:
            return 0

    # --- sessions ------------------------------------------------------------------------

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def start_session(self, account_id: str) -> str:
        """Return a fresh token. Only its hash is stored, so the database holds no key."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sessions (token_hash, account_id, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    self._token_hash(token),
                    account_id,
                    now,
                    now + self.session_days * 86400,
                ),
            )
            self._conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        return token

    def account_for_token(self, token: str | None) -> Account | None:
        """Who this cookie belongs to, or None. An expired session is deleted as it is found."""
        if not token:
            return None
        digest = self._token_hash(token)
        with self._lock:
            row = self._conn.execute(
                "SELECT a.*, s.expires_at FROM sessions s JOIN accounts a ON a.id = s.account_id"
                " WHERE s.token_hash = ?",
                (digest,),
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

    # --- throttling ----------------------------------------------------------------------

    def _check_throttle(self, email: str) -> None:
        count, until = self._failures.get(email, (0, 0.0))
        if count >= FAILED_ATTEMPTS_BEFORE_THROTTLE and time.time() < until:
            raise LoginError("Too many tries. Wait a minute and try again.")

    def _note_failure(self, email: str) -> None:
        count, _ = self._failures.get(email, (0, 0.0))
        self._failures[email] = (count + 1, time.time() + THROTTLE_SECONDS)

    def _clear_failures(self, email: str) -> None:
        self._failures.pop(email, None)

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
        """True when the next question needs an account first. Signed in, never."""
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
