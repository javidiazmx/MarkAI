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

MAX_NOTES_CHARS = 4000

SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_fit (
    owner_id        TEXT PRIMARY KEY,
    signals         TEXT NOT NULL DEFAULT '[]',
    manual_override INTEGER,
    last_ip         TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    hidden          INTEGER NOT NULL DEFAULT 0,
    ai_good_fit     INTEGER,
    ai_reasoning    TEXT NOT NULL DEFAULT '',
    ai_analyzed_at  REAL,
    updated_at      REAL NOT NULL
);
"""


@dataclass
class PmFit:
    """One owner's standing signal, as the admin page shows it."""

    owner_id: str
    signals: list[str]
    manual_override: bool | None
    last_ip: str
    notes: str
    hidden: bool
    ai_good_fit: bool | None
    ai_reasoning: str
    ai_analyzed_at: float | None
    updated_at: float

    @property
    def good_fit(self) -> bool:
        """Staff's own call, else the AI's read of the transcript, else the blunt regex
        signals - each one a fallback for when the one before it has nothing to say."""
        if self.manual_override is not None:
            return self.manual_override
        if self.ai_good_fit is not None:
            return self.ai_good_fit
        return bool(self.signals)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signals": list(self.signals),
            "manual_override": self.manual_override,
            "good_fit": self.good_fit,
            "last_ip": self.last_ip,
            "notes": self.notes,
            "hidden": self.hidden,
            "ai_good_fit": self.ai_good_fit,
            "ai_reasoning": self.ai_reasoning,
            "ai_analyzed_at": self.ai_analyzed_at,
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
            columns.discard("account_id")
            columns.add("owner_id")
        if "last_ip" not in columns:
            self._conn.execute("ALTER TABLE pm_fit ADD COLUMN last_ip TEXT NOT NULL DEFAULT ''")
            self._conn.commit()
        if "notes" not in columns:
            self._conn.execute("ALTER TABLE pm_fit ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
            self._conn.commit()
        if "hidden" not in columns:
            self._conn.execute("ALTER TABLE pm_fit ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()
        if "ai_good_fit" not in columns:
            self._conn.execute("ALTER TABLE pm_fit ADD COLUMN ai_good_fit INTEGER")
            self._conn.execute(
                "ALTER TABLE pm_fit ADD COLUMN ai_reasoning TEXT NOT NULL DEFAULT ''"
            )
            self._conn.execute("ALTER TABLE pm_fit ADD COLUMN ai_analyzed_at REAL")
            self._conn.commit()

    @staticmethod
    def _row_to_fit(row: sqlite3.Row) -> PmFit:
        try:
            signals = json.loads(row["signals"])
        except ValueError:
            signals = []
        override = row["manual_override"]
        ai_fit = row["ai_good_fit"]
        analyzed_at = row["ai_analyzed_at"]
        return PmFit(
            owner_id=row["owner_id"],
            signals=[str(s) for s in signals],
            manual_override=None if override is None else bool(override),
            last_ip=row["last_ip"] or "",
            notes=row["notes"] or "",
            hidden=bool(row["hidden"]),
            ai_good_fit=None if ai_fit is None else bool(ai_fit),
            ai_reasoning=row["ai_reasoning"] or "",
            ai_analyzed_at=None if analyzed_at is None else float(analyzed_at),
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

    def set_notes(self, owner_id: str, notes: str) -> None:
        """Free-text staff notes. Not shown to the landlord, never in an alert email."""
        if not owner_id:
            return
        text = str(notes or "").strip()[:MAX_NOTES_CHARS]
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT owner_id FROM pm_fit WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO pm_fit (owner_id, signals, notes, updated_at)"
                    " VALUES (?, '[]', ?, ?)",
                    (owner_id, text, time.time()),
                )
            else:
                self._conn.execute(
                    "UPDATE pm_fit SET notes = ?, updated_at = ? WHERE owner_id = ?",
                    (text, time.time(), owner_id),
                )

    def set_hidden(self, owner_id: str, value: bool) -> None:
        """Soft-hide a visitor from the default admin view. Never deletes their row."""
        if not owner_id:
            return
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT owner_id FROM pm_fit WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO pm_fit (owner_id, signals, hidden, updated_at)"
                    " VALUES (?, '[]', ?, ?)",
                    (owner_id, int(value), time.time()),
                )
            else:
                self._conn.execute(
                    "UPDATE pm_fit SET hidden = ?, updated_at = ? WHERE owner_id = ?",
                    (int(value), time.time(), owner_id),
                )

    def set_ai_analysis(self, owner_id: str, good_fit: bool, reasoning: str) -> None:
        """Record the AI's own read of the transcript, staff-triggered and on-demand.

        Sits between `manual_override` and the regex `signals` in `good_fit`'s precedence -
        a real judgment call over the full conversation, not just five keyword matches, but
        still overridable by a human who disagrees with it.
        """
        if not owner_id:
            return
        text = str(reasoning or "").strip()[:MAX_NOTES_CHARS]
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT owner_id FROM pm_fit WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO pm_fit"
                    " (owner_id, signals, ai_good_fit, ai_reasoning, ai_analyzed_at, updated_at)"
                    " VALUES (?, '[]', ?, ?, ?, ?)",
                    (owner_id, int(good_fit), text, time.time(), time.time()),
                )
            else:
                self._conn.execute(
                    "UPDATE pm_fit SET ai_good_fit = ?, ai_reasoning = ?, ai_analyzed_at = ?,"
                    " updated_at = ? WHERE owner_id = ?",
                    (int(good_fit), text, time.time(), time.time(), owner_id),
                )

    def note_visit(self, owner_id: str, ip: str) -> None:
        """Remember the most recent IP a request from this owner arrived from.

        Runs on every question, signal or not, so a visitor who never trips a pain-point
        flag is still reachable for the "location by IP" lookup in their detail view. Does
        not touch `signals` or `manual_override` - a plain visit is not a fit signal.
        """
        if not owner_id or not ip:
            return
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT owner_id FROM pm_fit WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO pm_fit (owner_id, signals, last_ip, updated_at)"
                    " VALUES (?, '[]', ?, ?)",
                    (owner_id, ip, time.time()),
                )
            else:
                self._conn.execute(
                    "UPDATE pm_fit SET last_ip = ?, updated_at = ? WHERE owner_id = ?",
                    (ip, time.time(), owner_id),
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
                signals, override, ip = old_fit.signals, old_fit.manual_override, old_fit.last_ip
                notes, hidden = old_fit.notes, old_fit.hidden
                ai_fit = old_fit.ai_good_fit
                ai_reasoning, ai_at = old_fit.ai_reasoning, old_fit.ai_analyzed_at
            else:
                new_fit = self._row_to_fit(new_row)
                # De-duplicated, oldest first: order does not matter, only membership does.
                signals = list(dict.fromkeys(new_fit.signals + old_fit.signals))
                override = (
                    new_fit.manual_override
                    if new_fit.manual_override is not None
                    else old_fit.manual_override
                )
                ip = new_fit.last_ip or old_fit.last_ip
                notes = new_fit.notes or old_fit.notes
                hidden = new_fit.hidden or old_fit.hidden
                # Whichever transcript was actually analyzed - the account's own if staff
                # already ran it there, otherwise carry the anonymous read forward.
                if new_fit.ai_analyzed_at is not None:
                    ai_fit, ai_reasoning, ai_at = (
                        new_fit.ai_good_fit,
                        new_fit.ai_reasoning,
                        new_fit.ai_analyzed_at,
                    )
                else:
                    ai_fit, ai_reasoning, ai_at = (
                        old_fit.ai_good_fit,
                        old_fit.ai_reasoning,
                        old_fit.ai_analyzed_at,
                    )
            self._conn.execute(
                "INSERT INTO pm_fit (owner_id, signals, manual_override, last_ip, notes,"
                " hidden, ai_good_fit, ai_reasoning, ai_analyzed_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(owner_id) DO UPDATE SET"
                " signals = excluded.signals, manual_override = excluded.manual_override,"
                " last_ip = excluded.last_ip, notes = excluded.notes,"
                " hidden = excluded.hidden, ai_good_fit = excluded.ai_good_fit,"
                " ai_reasoning = excluded.ai_reasoning,"
                " ai_analyzed_at = excluded.ai_analyzed_at, updated_at = excluded.updated_at",
                (
                    new_owner_id,
                    json.dumps(signals),
                    None if override is None else int(override),
                    ip,
                    notes,
                    int(hidden),
                    None if ai_fit is None else int(ai_fit),
                    ai_reasoning,
                    ai_at,
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


CLASSIFY_MODEL = "claude-haiku-4-5-20251001"
MAX_TRANSCRIPT_CHARS = 12000


class ClassificationError(RuntimeError):
    """The AI could not produce a usable verdict - no key, a bad response, network trouble."""


def _transcript_text(threads: list[Any]) -> str:
    """Every message across every thread, oldest first, capped so the call stays cheap.

    Cut from the front, not the back: the most recent exchange is the one most likely to
    hold whatever made staff want to check in the first place.
    """
    lines: list[str] = []
    for thread in threads:
        for message in getattr(thread, "messages", []) or []:
            role = "Landlord" if message.get("role") == "user" else "Jay"
            content = str(message.get("content", "")).strip()
            if content:
                lines.append(f"{role}: {content}")
    text = "\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = "…" + text[-MAX_TRANSCRIPT_CHARS:]
    return text


CLASSIFY_SYSTEM_PROMPT = """You are helping a Chicagoland property management company \
(GC Realty) decide whether a landlord who has been chatting with their AI advisor, Jay, \
looks like a good candidate to reach out to about hiring a property manager.

Read the conversation below and answer only with a single JSON object, no other text:
{"good_fit": true or false, "confidence": "high", "medium", or "low", \
"reasoning": "one or two sentences, specific to what they actually said"}

A good fit shows real signs of being worth a call: burnout, a tenant problem they are \
struggling with, a vacancy they cannot fill, interest in hiring a PM, or scaling a \
portfolio. A landlord who is just asking a quick factual question (deposit rules, a \
notice period) with no sign of a deeper problem is not a good fit on that alone. Judge \
the substance of what they said, not just whether pain-point words appear."""


def classify_fit(settings: Any, threads: list[Any], client: Any = None) -> tuple[bool, str]:
    """Ask a cheap model for a real read of the transcript, not five regex patterns.

    Staff-triggered only (never called from the chat path), so the cost is one small call
    per click, not one per question. Raises `ClassificationError` on anything that keeps a
    usable verdict from coming back - no key, an empty transcript, a bad response shape -
    so the caller can tell staff plainly rather than silently storing a guess.
    """
    transcript = _transcript_text(threads)
    if not transcript:
        raise ClassificationError("There is no conversation to analyze yet.")
    if client is None:
        import anthropic

        key = settings.anthropic_key()
        if not key:
            raise ClassificationError("ANTHROPIC_API_KEY is not set.")
        client = anthropic.Anthropic(api_key=key)
    try:
        response = client.messages.create(
            model=CLASSIFY_MODEL,
            max_tokens=300,
            system=CLASSIFY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": transcript}],
        )
    except Exception as exc:
        raise ClassificationError(f"Could not reach the model: {exc}") from exc
    text = "".join(
        block.text
        for block in getattr(response, "content", [])
        if getattr(block, "type", "") == "text"
    ).strip()
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        parsed = json.loads(text[start:end])
        good_fit = bool(parsed["good_fit"])
        reasoning = str(parsed.get("reasoning", "")).strip()
    except (ValueError, KeyError) as exc:
        raise ClassificationError(f"The model's answer was not usable: {text[:200]}") from exc
    return good_fit, reasoning or "No reasoning given."
