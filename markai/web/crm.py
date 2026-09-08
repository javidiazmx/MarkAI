"""Send a new lead to the CRM, without ever making a landlord wait for it.

A signup is worth nothing sitting in a SQLite file on one machine, so it goes out to
whatever the owner points this at: LeadSimple's own inbound URL, a Zapier or Make catch
hook, an internal endpoint. One generic POST with a stable payload serves all of them.

Two rules shape everything here:

- **A lead is never lost.** It is written to a queue in the same transaction as the account
  and delivered afterwards, retried with backoff, and retried again on the next start. A
  CRM that is down, rate limited, or misconfigured costs nothing but time.
- **Delivery never blocks an answer.** It runs on a worker thread. The landlord's next
  question does not wait for someone else's webhook.

This uses ``httpx``, the ingesters' client, and is deliberately nowhere near the Anthropic
SDK, which runs on ``httpx2``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 6
# 1 minute, 5, 15, 1 hour, 6 hours. A CRM outage outlasts a tight retry loop.
BACKOFF_SECONDS = (60.0, 300.0, 900.0, 3600.0, 21600.0)
DEFAULT_TIMEOUT = 15.0
BATCH = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   TEXT NOT NULL,
    payload      TEXT NOT NULL,
    created_at   REAL NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_try_at  REAL NOT NULL DEFAULT 0,
    delivered_at REAL,
    last_error   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS leads_pending ON leads(delivered_at, next_try_at);
"""


@dataclass
class Lead:
    """One queued delivery, as `mark leads` shows it."""

    id: int
    account_id: str
    payload: dict[str, Any]
    created_at: float
    attempts: int
    delivered_at: float | None
    last_error: str

    @property
    def delivered(self) -> bool:
        return self.delivered_at is not None

    @property
    def gave_up(self) -> bool:
        return not self.delivered and self.attempts >= MAX_ATTEMPTS


def build_payload(account: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """The lead as the CRM receives it.

    Flat and boring on purpose: every CRM and every automation tool can map flat fields,
    and a shape that does not change is one the owner only has to map once. The question
    they asked is included because a lead that says what someone wanted is worth several
    that only say who they are.
    """
    payload = {
        "source": "Jay, Chicagoland landlord advisor",
        "name": account.name,
        "email": account.email,
        "phone": account.phone,
        "neighborhood": account.neighborhood,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "has_password": bool(getattr(account, "has_password", False)),
    }
    payload.update(context or {})
    return payload


def _post(url: str, headers: dict[str, str], timeout: float) -> Callable[[dict], None]:
    """The default sender: one POST, raising on anything that is not a success."""

    def send(payload: dict[str, Any]) -> None:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()

    return send


class Crm:
    """A durable outbound queue for new leads.

    ``sender`` is injectable so tests never touch the network and so an owner with a CRM
    that wants something other than a JSON POST has one function to replace.
    """

    def __init__(
        self,
        path: Path,
        url: str = "",
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        sender: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.url = url.strip()
        self._headers = dict(headers or {})
        self._timeout = timeout
        self._sender = sender or (_post(self.url, self._headers, timeout) if self.url else None)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._worker: threading.Thread | None = None

    @property
    def configured(self) -> bool:
        """False with no URL and no sender: leads still queue, they just wait."""
        return self._sender is not None

    # --- the queue -----------------------------------------------------------------------

    def enqueue(self, account_id: str, payload: dict[str, Any]) -> int:
        """Write the lead down first. Delivery is a separate, retryable problem."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO leads (account_id, payload, created_at, next_try_at)"
                " VALUES (?, ?, ?, ?)",
                (account_id, json.dumps(payload, sort_keys=True), time.time(), 0.0),
            )
        return int(cursor.lastrowid or 0)

    @staticmethod
    def _row_to_lead(row: sqlite3.Row) -> Lead:
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            payload = {}
        return Lead(
            id=int(row["id"]),
            account_id=row["account_id"],
            payload=payload,
            created_at=float(row["created_at"]),
            attempts=int(row["attempts"]),
            delivered_at=row["delivered_at"],
            last_error=row["last_error"],
        )

    def pending(self, limit: int = BATCH, now: float | None = None) -> list[Lead]:
        moment = now if now is not None else time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM leads WHERE delivered_at IS NULL AND attempts < ?"
                " AND next_try_at <= ? ORDER BY id LIMIT ?",
                (MAX_ATTEMPTS, moment, limit),
            ).fetchall()
        return [self._row_to_lead(row) for row in rows]

    def all(self, limit: int = 100) -> list[Lead]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_lead(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(delivered_at IS NOT NULL) AS delivered,"
                f" SUM(delivered_at IS NULL AND attempts >= {MAX_ATTEMPTS}) AS gave_up"
                " FROM leads"
            ).fetchone()
        total = int(row["total"] or 0)
        delivered = int(row["delivered"] or 0)
        gave_up = int(row["gave_up"] or 0)
        return {
            "total": total,
            "delivered": delivered,
            "gave_up": gave_up,
            "waiting": total - delivered - gave_up,
        }

    def reset_attempts(self) -> int:
        """Put everything that gave up back in the queue, for after a URL is fixed."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE leads SET attempts = 0, next_try_at = 0, last_error = ''"
                " WHERE delivered_at IS NULL"
            )
        return cursor.rowcount

    # --- delivery ------------------------------------------------------------------------

    def deliver_pending(self, now: float | None = None) -> tuple[int, int]:
        """Try every lead that is due. Returns (delivered, failed)."""
        if not self.configured:
            return (0, 0)
        delivered = failed = 0
        for lead in self.pending(now=now):
            try:
                self._sender(lead.payload)
            except Exception as exc:  # any transport or status error is a retry
                failed += 1
                self._record_failure(lead, exc)
                continue
            delivered += 1
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE leads SET delivered_at = ?, last_error = '' WHERE id = ?",
                    (time.time(), lead.id),
                )
        if delivered or failed:
            logger.info("crm delivery: %d sent, %d to retry", delivered, failed)
        return (delivered, failed)

    def _record_failure(self, lead: Lead, exc: Exception) -> None:
        attempts = lead.attempts + 1
        wait = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
        # The class and message, never the payload: it is somebody's phone number.
        reason = f"{type(exc).__name__}: {exc}"[:300]
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE leads SET attempts = ?, next_try_at = ?, last_error = ? WHERE id = ?",
                (attempts, time.time() + wait, reason, lead.id),
            )
        if attempts >= MAX_ATTEMPTS:
            logger.warning(
                "gave up sending lead %d to the CRM after %d tries: %s",
                lead.id,
                attempts,
                reason,
            )

    def deliver_soon(self) -> None:
        """Deliver on a worker thread. Never called on the path that answers a question."""
        if not self.configured:
            return
        if self._worker is not None and self._worker.is_alive():
            return

        def run() -> None:
            try:
                self.deliver_pending()
            except Exception:  # a worker that dies loudly is better than one that hangs
                logger.exception("crm delivery thread failed")

        self._worker = threading.Thread(target=run, name="markai-crm", daemon=True)
        self._worker.start()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
