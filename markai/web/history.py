"""Past conversations, kept so a landlord can pick one back up.

Titles are made from the first question, not by asking Claude for one: a title should cost
nothing and appear the instant a conversation starts.

Stored per browser, keyed by the session id the page generates. There is no login, so this
is not a security boundary - it is the same trust model as the chat itself, and the id is
unguessable rather than protected. Question text is what the landlord typed, so the same
rule as the question log applies: it lives on the operator's own disk and goes nowhere else.

Reopening a conversation restores the transcript on screen. Whether Jay still *remembers* it
depends on the in-memory ``Conversation`` for that id, which the LRU keeps until the process
restarts: within a session it does, after a restart the thread reads back but the follow-up
starts fresh. Rehydrating the model's side from this text is deliberately not done - the
stored answer has been through the guardrails, so it is not what the model emitted, and
rebuilding turns from it would invalidate the thinking blocks and the prompt cache.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS = 60
MAX_THREADS_PER_BROWSER = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id          TEXT PRIMARY KEY,
    browser_id  TEXT NOT NULL,
    title       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    turns       INTEGER NOT NULL DEFAULT 0,
    messages    TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS threads_by_browser ON threads(browser_id, updated_at DESC);
"""

_FILLER = re.compile(
    r"^(hola|hi|hey|hello|oye|por favor|please|quiero saber|i want to know|"
    r"tengo una pregunta|quick question|question|una duda|help|ayuda)[\s,:-]+",
    re.IGNORECASE,
)


def title_for(question: str) -> str:
    """A short label from the question itself, with the throat-clearing removed."""
    text = re.sub(r"\s+", " ", (question or "").strip())
    text = _FILLER.sub("", text).strip(" ?¿!¡.,:;")
    if not text:
        return "New conversation"
    if len(text) <= MAX_TITLE_CHARS:
        return text[:1].upper() + text[1:]
    cut = text[:MAX_TITLE_CHARS]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return (cut[:1].upper() + cut[1:]).rstrip(" ,;:") + "…"


@dataclass
class Thread:
    """One saved conversation as the sidebar needs it."""

    id: str
    title: str
    updated_at: float
    turns: int
    messages: list[dict] = field(default_factory=list)

    def to_dict(self, with_messages: bool = False) -> dict:
        data = {
            "id": self.id,
            "title": self.title,
            "updated_at": self.updated_at,
            "turns": self.turns,
        }
        if with_messages:
            data["messages"] = self.messages
        return data


class History:
    """SQLite-backed conversation list. Separate from the knowledge base on purpose."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def record(self, browser_id: str, thread_id: str, question: str, answer: str) -> None:
        """Append one exchange, creating the thread and its title on the first question."""
        if not browser_id or not thread_id:
            return
        now = time.time()
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT messages, turns FROM threads WHERE id = ? AND browser_id = ?",
                    (thread_id, browser_id),
                ).fetchone()
                messages = json.loads(row["messages"]) if row else []
                messages.append({"role": "user", "content": question})
                messages.append({"role": "assistant", "content": answer})
                if row:
                    self._conn.execute(
                        "UPDATE threads SET messages = ?, turns = ?, updated_at = ?"
                        " WHERE id = ? AND browser_id = ?",
                        (json.dumps(messages), row["turns"] + 1, now, thread_id, browser_id),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO threads (id, browser_id, title, created_at, updated_at,"
                        " turns, messages) VALUES (?, ?, ?, ?, ?, 1, ?)",
                        (
                            thread_id,
                            browser_id,
                            title_for(question),
                            now,
                            now,
                            json.dumps(messages),
                        ),
                    )
                self._prune(browser_id)
        except sqlite3.Error as exc:
            # A conversation that cannot be filed is not a reason to lose the answer.
            logger.warning("could not save conversation %s: %s", thread_id, exc)

    def _prune(self, browser_id: str) -> None:
        self._conn.execute(
            "DELETE FROM threads WHERE browser_id = ? AND id NOT IN ("
            "  SELECT id FROM threads WHERE browser_id = ?"
            "  ORDER BY updated_at DESC LIMIT ?)",
            (browser_id, browser_id, MAX_THREADS_PER_BROWSER),
        )

    def list(self, browser_id: str, limit: int = 50) -> list[Thread]:
        if not browser_id:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, title, updated_at, turns FROM threads WHERE browser_id = ?"
                " ORDER BY updated_at DESC LIMIT ?",
                (browser_id, max(1, min(limit, MAX_THREADS_PER_BROWSER))),
            ).fetchall()
        return [
            Thread(id=r["id"], title=r["title"], updated_at=r["updated_at"], turns=r["turns"])
            for r in rows
        ]

    def get(self, browser_id: str, thread_id: str) -> Thread | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, title, updated_at, turns, messages FROM threads"
                " WHERE id = ? AND browser_id = ?",
                (thread_id, browser_id),
            ).fetchone()
        if row is None:
            return None
        return Thread(
            id=row["id"],
            title=row["title"],
            updated_at=row["updated_at"],
            turns=row["turns"],
            messages=json.loads(row["messages"]),
        )

    def delete(self, browser_id: str, thread_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM threads WHERE id = ? AND browser_id = ?", (thread_id, browser_id)
            )
        return cursor.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
