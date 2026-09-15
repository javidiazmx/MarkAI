"""GC Realty staff's own view: which visitors look like a fit for property management.

Additive and separate from every other store - nothing here touches ``accounts.db``,
``conversations.db``, or ``leads.db``. The five pain-point flags in ``guardrails.py`` already
run on every question; this just remembers, per owner, which of them have ever fired, and
lets a staff member override what the AI decided. "Good fit" is the AI's own call by default:
any signal at all is a yes, unless a human has said otherwise.

Keyed on the owner id (``account:<id>`` once signed in, ``browser:<id>`` before that) rather
than a bare account id, the same identity every other store in ``markai/web/`` uses - so a
visitor who trips a signal before signing up is not invisible to staff, only unreachable by
email until they do.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_fit (
    owner_id        TEXT PRIMARY KEY,
    signals         TEXT NOT NULL DEFAULT '[]',
    manual_override INTEGER,
    updated_at      REAL NOT NULL
);
"""


@dataclass
class PmFit:
    """One owner's standing signal, as the admin page shows it."""

    owner_id: str
    signals: list[str]
    manual_override: bool | None
    updated_at: float

    @property
    def good_fit(self) -> bool:
        """The AI's call, unless staff overrode it."""
        if self.manual_override is not None:
            return self.manual_override
        return bool(self.signals)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signals": list(self.signals),
            "manual_override": self.manual_override,
            "good_fit": self.good_fit,
            "updated_at": self.updated_at,
        }


class AdminStore:
    """Every owner's PM-fit signals, one row each, keyed on the owner id."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _migrate(self) -> None:
        """Carry the first shape forward: it keyed a row on a bare account id.

        The same move `Accounts._migrate` already made for its own ``usage`` table, once
        anonymous browsers started earning signals too and the key had to become an owner
        id like everything else in ``markai/web/``.
        """
        listing = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in listing}
        if "pm_fit" not in tables:
            return
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(pm_fit)")}
        if "owner_id" not in columns and "account_id" in columns:
            self._conn.execute("ALTER TABLE pm_fit RENAME COLUMN account_id TO owner_id")
            self._conn.execute(
                "UPDATE pm_fit SET owner_id = 'account:' || owner_id"
                " WHERE owner_id NOT LIKE 'account:%' AND owner_id NOT LIKE 'browser:%'"
            )
            self._conn.commit()

    @staticmethod
    def _row_to_fit(row: sqlite3.Row) -> PmFit:
        try:
            signals = json.loads(row["signals"])
        except ValueError:
            signals = []
        override = row["manual_override"]
        return PmFit(
            owner_id=row["owner_id"],
            signals=[str(s) for s in signals],
            manual_override=None if override is None else bool(override),
            updated_at=float(row["updated_at"]),
        )

    def get(self, owner_id: str) -> PmFit | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pm_fit WHERE owner_id = ?", (owner_id,)
            ).fetchone()
        return None if row is None else self._row_to_fit(row)

    def record_signal(self, owner_id: str, signal: str) -> bool:
        """Remember that ``signal`` fired for this owner.

        Returns True the first time this exact signal is newly recorded for this owner,
        which is what an alert should fire on - the same "once per signal per account" rule
        `Crm.already_sent` already uses, just local to this table.
        """
        if not owner_id or not signal:
            return False
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT signals FROM pm_fit WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO pm_fit (owner_id, signals, updated_at) VALUES (?, ?, ?)",
                    (owner_id, json.dumps([signal]), time.time()),
                )
                return True
            try:
                signals = json.loads(row["signals"])
            except ValueError:
                signals = []
            if signal in signals:
                return False
            signals.append(signal)
            self._conn.execute(
                "UPDATE pm_fit SET signals = ?, updated_at = ? WHERE owner_id = ?",
                (json.dumps(signals), time.time(), owner_id),
            )
            return True

    def set_override(self, owner_id: str, value: bool | None) -> None:
        """The staff checkbox. ``None`` clears it back to "follow the AI"."""
        if not owner_id:
            return
        stored = None if value is None else int(value)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT owner_id FROM pm_fit WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO pm_fit (owner_id, signals, manual_override, updated_at)"
                    " VALUES (?, '[]', ?, ?)",
                    (owner_id, stored, time.time()),
                )
            else:
                self._conn.execute(
                    "UPDATE pm_fit SET manual_override = ?, updated_at = ? WHERE owner_id = ?",
                    (stored, time.time(), owner_id),
                )

    def reassign(self, old_owner_id: str, new_owner_id: str) -> int:
        """Carry a visitor's signals into their new account the moment they sign up.

        Mirrors `History.reassign` and `Portfolio.reassign`: what they showed anonymously is
        theirs, so a signal earned as `browser:<id>` is merged into `account:<id>` rather than
        left behind under an id nothing will ever look up again. Only called for the browser
        the signup came from. Silent - it does not re-fire the staff alert, the same way a
        pre-signup `pm_interest` question never queued a candidate lead retroactively either.
        """
        if not old_owner_id or not new_owner_id or old_owner_id == new_owner_id:
            return 0
        with self._lock, self._conn:
            old_row = self._conn.execute(
                "SELECT * FROM pm_fit WHERE owner_id = ?", (old_owner_id,)
            ).fetchone()
            if old_row is None:
                return 0
            old_fit = self._row_to_fit(old_row)
            new_row = self._conn.execute(
                "SELECT * FROM pm_fit WHERE owner_id = ?", (new_owner_id,)
            ).fetchone()
            if new_row is None:
                signals, override = old_fit.signals, old_fit.manual_override
            else:
                new_fit = self._row_to_fit(new_row)
                # De-duplicated, oldest first: order does not matter, only membership does.
                signals = list(dict.fromkeys(new_fit.signals + old_fit.signals))
                override = (
                    new_fit.manual_override
                    if new_fit.manual_override is not None
                    else old_fit.manual_override
                )
            self._conn.execute(
                "INSERT INTO pm_fit (owner_id, signals, manual_override, updated_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(owner_id) DO UPDATE SET"
                " signals = excluded.signals, manual_override = excluded.manual_override,"
                " updated_at = excluded.updated_at",
                (
                    new_owner_id,
                    json.dumps(signals),
                    None if override is None else int(override),
                    time.time(),
                ),
            )
            self._conn.execute("DELETE FROM pm_fit WHERE owner_id = ?", (old_owner_id,))
        return 1

    def all(self) -> dict[str, PmFit]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM pm_fit").fetchall()
        return {row["owner_id"]: self._row_to_fit(row) for row in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


PM_FIT_SUBJECT = "Jay flagged a landlord for property management"


def build_alert_email(account: Any, flag: str, reason: str) -> tuple[str, str]:
    """Subject and body for the human staff alert. Plain and readable, not machine-parsed."""
    name = str(getattr(account, "name", "")).strip() or "Someone"
    lines = [
        f"{name} may be a good fit for property management.",
        "",
        f"Signal: {reason} ({flag})",
        "",
        f"Name: {getattr(account, 'name', '')}",
        f"Email: {getattr(account, 'email', '')}",
        f"Phone: {getattr(account, 'phone', '')}",
        f"Neighborhood: {getattr(account, 'neighborhood', '')}",
        "",
        "See the full picture, and every other signal on file, in the admin panel.",
    ]
    return (f"{PM_FIT_SUBJECT}: {name}", "\n".join(lines))


def _email_sender(
    to_address: str,
    host: str,
    port: int,
    username: str,
    password: str,
    from_address: str,
    starttls: bool,
    timeout: float,
):
    """Mirrors ``crm.py``'s ``_email``: one message per alert, raising on anything that fails."""

    def send(account: Any, flag: str, reason: str) -> None:
        import smtplib
        from email.message import EmailMessage

        subject, body = build_alert_email(account, flag, reason)
        message = EmailMessage()
        message["To"] = to_address
        message["From"] = from_address or username
        message["Subject"] = subject
        message.set_content(body)

        opener = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        with opener(host, port, timeout=timeout) as smtp:
            if starttls and port != 465:
                smtp.starttls()
            if username:
                smtp.login(username, password)
            smtp.send_message(message)

    return send


def pm_fit_alert_sender(settings: Any):
    """Build the alert sender from settings, or None if ``pm_fit_alert_email_to`` is unset."""
    to_address = (getattr(settings, "pm_fit_alert_email_to", "") or "").strip()
    host = (getattr(settings, "smtp_host", "") or "").strip()
    if not to_address or not host:
        return None
    return _email_sender(
        to_address,
        host,
        int(settings.smtp_port),
        (settings.smtp_username or "").strip(),
        settings.smtp_secret() or "",
        (settings.smtp_from or "").strip(),
        bool(settings.smtp_starttls),
        15.0,
    )


def send_pm_fit_alert_soon(
    sender: Any,
    account: Any,
    flag: str,
    reason: str,
) -> None:
    """Fire the alert on a background thread. Best-effort: never raises into the request path.

    Same discipline as `crm.py`'s `deliver_soon` - an alert failure must never break the chat
    answer that triggered it.
    """
    if sender is None:
        return

    def run() -> None:
        try:
            sender(account, flag, reason)
        except Exception:
            logger.exception("could not send the PM-fit alert email")

    threading.Thread(target=run, name="markai-pm-fit-alert", daemon=True).start()
