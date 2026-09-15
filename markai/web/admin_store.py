"""GC Realty staff's own view: which landlords look like a fit for property management.

Additive and separate from every other store - nothing here touches ``accounts.db``,
``conversations.db``, or ``leads.db``. The five pain-point flags in ``guardrails.py`` already
run on every question; this just remembers, per account, which of them have ever fired, and
lets a staff member override what the AI decided. "Good fit" is the AI's own call by default:
any signal at all is a yes, unless a human has said otherwise.
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
    account_id      TEXT PRIMARY KEY,
    signals         TEXT NOT NULL DEFAULT '[]',
    manual_override INTEGER,
    updated_at      REAL NOT NULL
);
"""


@dataclass
class PmFit:
    """One account's standing signal, as the admin page shows it."""

    account_id: str
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
    """Every account's PM-fit signals, one row each, keyed on ``account.id``."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @staticmethod
    def _row_to_fit(row: sqlite3.Row) -> PmFit:
        try:
            signals = json.loads(row["signals"])
        except ValueError:
            signals = []
        override = row["manual_override"]
        return PmFit(
            account_id=row["account_id"],
            signals=[str(s) for s in signals],
            manual_override=None if override is None else bool(override),
            updated_at=float(row["updated_at"]),
        )

    def get(self, account_id: str) -> PmFit | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pm_fit WHERE account_id = ?", (account_id,)
            ).fetchone()
        return None if row is None else self._row_to_fit(row)

    def record_signal(self, account_id: str, signal: str) -> bool:
        """Remember that ``signal`` fired for this account.

        Returns True the first time this exact signal is newly recorded for this account,
        which is what an alert should fire on - the same "once per signal per account" rule
        `Crm.already_sent` already uses, just local to this table.
        """
        if not account_id or not signal:
            return False
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT signals FROM pm_fit WHERE account_id = ?", (account_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO pm_fit (account_id, signals, updated_at) VALUES (?, ?, ?)",
                    (account_id, json.dumps([signal]), time.time()),
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
                "UPDATE pm_fit SET signals = ?, updated_at = ? WHERE account_id = ?",
                (json.dumps(signals), time.time(), account_id),
            )
            return True

    def set_override(self, account_id: str, value: bool | None) -> None:
        """The staff checkbox. ``None`` clears it back to "follow the AI"."""
        if not account_id:
            return
        stored = None if value is None else int(value)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT account_id FROM pm_fit WHERE account_id = ?", (account_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO pm_fit (account_id, signals, manual_override, updated_at)"
                    " VALUES (?, '[]', ?, ?)",
                    (account_id, stored, time.time()),
                )
            else:
                self._conn.execute(
                    "UPDATE pm_fit SET manual_override = ?, updated_at = ? WHERE account_id = ?",
                    (stored, time.time(), account_id),
                )

    def all(self) -> dict[str, PmFit]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM pm_fit").fetchall()
        return {row["account_id"]: self._row_to_fit(row) for row in rows}

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
