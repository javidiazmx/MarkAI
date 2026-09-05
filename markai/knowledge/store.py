"""SQLite-backed knowledge store (documents, chunks, embeddings, ingest runs, question log).

One file (``data/markai.db``), stdlib ``sqlite3`` in WAL mode. Embeddings are stored as raw
float32 bytes next to the chunk text so a full matrix can be pulled in one query.
Everything the advisor needs at answer time is loaded into memory by the retriever; the store
is only hit per query to log the question.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from markai.models import Chunk, Document, Segment, SourceKind

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    locator       TEXT NOT NULL,
    text          TEXT NOT NULL,
    segments_json TEXT NOT NULL DEFAULT '[]',
    link          TEXT,
    published_at  TEXT,
    episode       TEXT,
    channel       TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    content_hash  TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_kind ON documents(kind);
CREATE INDEX IF NOT EXISTS idx_documents_locator ON documents(locator);

CREATE TABLE IF NOT EXISTS chunks (
    id              TEXT PRIMARY KEY,
    doc_id          TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,
    text            TEXT NOT NULL,
    start_char      INTEGER NOT NULL DEFAULT 0,
    end_char        INTEGER NOT NULL DEFAULT 0,
    start_time      REAL,
    end_time        REAL,
    heading         TEXT,
    embedding       BLOB,
    embedding_model TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, idx);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL,
    summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asked_at      TEXT NOT NULL,
    session_id    TEXT,
    question      TEXT NOT NULL,
    coverage      TEXT NOT NULL,
    flags_json    TEXT NOT NULL DEFAULT '[]',
    not_covered   INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_questions_not_covered ON questions_log(not_covered, asked_at);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class StoreStats:
    """Whitelisted numbers for ``mark status`` and ``/api/status`` (no secrets, no text)."""

    documents_by_kind: dict[str, int]
    chunks: int
    embedded_chunks: int
    last_ingest_at: str | None
    embedding_model: str | None
    questions_total: int
    questions_not_covered: int
    tokens_total: int

    def to_dict(self) -> dict:
        return asdict(self)


class KnowledgeStore:
    """Thread-safe (one connection, one lock) SQLite store."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(SCHEMA)
        logger.debug("knowledge store opened at %s", self.db_path)

    # --- serialization helpers -------------------------------------------------------------

    @staticmethod
    def _doc_row(doc: Document) -> tuple:
        return (
            doc.id,
            doc.kind.value,
            doc.title,
            doc.locator,
            doc.text,
            json.dumps([asdict(s) for s in doc.segments]),
            doc.link,
            doc.published_at,
            doc.episode,
            doc.channel,
            json.dumps(doc.metadata, default=str),
            doc.ensure_hash(),
            _now_iso(),
        )

    @staticmethod
    def _row_to_document(row: sqlite3.Row) -> Document:
        segments = [Segment(**s) for s in json.loads(row["segments_json"] or "[]")]
        metadata = json.loads(row["metadata_json"] or "{}")
        return Document(
            id=row["id"],
            kind=SourceKind(row["kind"]),
            title=row["title"],
            locator=row["locator"],
            text=row["text"],
            segments=segments,
            link=row["link"],
            published_at=row["published_at"],
            episode=row["episode"],
            channel=row["channel"],
            metadata=metadata if isinstance(metadata, dict) else {},
            content_hash=row["content_hash"] or "",
        )

    @staticmethod
    def _row_to_chunk(row: sqlite3.Row) -> Chunk:
        return Chunk(
            id=row["id"],
            doc_id=row["doc_id"],
            index=row["idx"],
            text=row["text"],
            start_char=row["start_char"] or 0,
            end_char=row["end_char"] or 0,
            start_time=row["start_time"],
            end_time=row["end_time"],
            heading=row["heading"],
        )

    # --- documents -------------------------------------------------------------------------

    def upsert_document(
        self,
        doc: Document,
        chunks: list[Chunk],
        embeddings: list[list[float]] | None = None,
        embedding_model: str | None = None,
    ) -> None:
        """Replace ``doc`` and all of its chunks in a single transaction."""
        rows = []
        for i, chunk in enumerate(chunks):
            blob = None
            model = None
            if embeddings is not None and i < len(embeddings) and embeddings[i] is not None:
                blob = np.asarray(embeddings[i], dtype=np.float32).tobytes()
                model = embedding_model
            rows.append(
                (
                    chunk.id,
                    doc.id,
                    chunk.index,
                    chunk.text,
                    chunk.start_char,
                    chunk.end_char,
                    chunk.start_time,
                    chunk.end_time,
                    chunk.heading,
                    blob,
                    model,
                )
            )
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc.id,))
            self._conn.execute(
                "INSERT OR REPLACE INTO documents (id, kind, title, locator, text, segments_json,"
                " link, published_at, episode, channel, metadata_json, content_hash, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._doc_row(doc),
            )
            self._conn.executemany(
                "INSERT INTO chunks (id, doc_id, idx, text, start_char, end_char, start_time,"
                " end_time, heading, embedding, embedding_model)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        logger.debug("upserted document %s with %d chunks", doc.id, len(rows))

    def get_document(self, doc_id: str) -> Document | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return self._row_to_document(row) if row else None

    def document_hash(self, doc_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT content_hash FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
        return row["content_hash"] if row else None

    def locator_with_hash(self, content_hash: str, other_than: str) -> str | None:
        """The locator of a *different* document holding exactly this text, if one exists.

        Two domains serving the same site is common (an alias, a staging host, a vanity
        domain). Storing both copies doubles every passage and lets one page crowd real
        variety out of the top-k.
        """
        if not content_hash:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT locator FROM documents WHERE content_hash = ? AND id != ? LIMIT 1",
                (content_hash, other_than),
            ).fetchone()
        return row["locator"] if row else None

    def list_documents(self, kind: SourceKind | None = None) -> list[Document]:
        sql = "SELECT * FROM documents"
        params: tuple = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            params = (kind.value,)
        sql += " ORDER BY kind, published_at, title, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_document(r) for r in rows]

    def list_locators(self, kind: SourceKind | None = None) -> dict[str, str]:
        sql = "SELECT id, locator FROM documents"
        params: tuple = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            params = (kind.value,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {r["id"]: r["locator"] for r in rows}

    def delete_document(self, doc_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            self._conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        logger.debug("deleted document %s", doc_id)

    # --- chunks & embeddings ---------------------------------------------------------------

    def all_chunks(self) -> list[Chunk]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, doc_id, idx, text, start_char, end_char, start_time, end_time, heading"
                " FROM chunks ORDER BY doc_id, idx"
            ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def chunks_for_document(self, doc_id: str) -> list[Chunk]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, doc_id, idx, text, start_char, end_char, start_time, end_time, heading"
                " FROM chunks WHERE doc_id = ? ORDER BY idx",
                (doc_id,),
            ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def chunks_missing_embeddings(self, model: str) -> list[Chunk]:
        """Stored chunks that have no embedding for ``model`` yet.

        The text is already on disk, so embeddings can be backfilled after the fact instead
        of re-downloading every source just to add semantic search.
        """
        rows = self._conn.execute(
            "SELECT * FROM chunks WHERE embedding IS NULL OR embedding_model IS NOT ?"
            " ORDER BY doc_id, idx",
            (model,),
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    def set_embeddings(self, embeddings: dict[str, list[float]], embedding_model: str) -> None:
        """Attach embeddings to chunks that already exist."""
        if not embeddings:
            return
        payload = [
            (np.asarray(vector, dtype=np.float32).tobytes(), embedding_model, chunk_id)
            for chunk_id, vector in embeddings.items()
        ]
        with self._conn:
            self._conn.executemany(
                "UPDATE chunks SET embedding = ?, embedding_model = ? WHERE id = ?", payload
            )

    def embeddings_matrix(self, model: str | None = None) -> tuple[list[str], np.ndarray] | None:
        """All stored embeddings (optionally for one model) as ``(chunk_ids, float32 matrix)``."""
        sql = "SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL"
        params: tuple = ()
        if model is not None:
            sql += " AND embedding_model = ?"
            params = (model,)
        sql += " ORDER BY doc_id, idx"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        if not rows:
            return None
        ids: list[str] = []
        vectors: list[np.ndarray] = []
        dim: int | None = None
        for row in rows:
            vec = np.frombuffer(row["embedding"], dtype=np.float32)
            if vec.size == 0:
                continue
            if dim is None:
                dim = vec.size
            if vec.size != dim:
                logger.warning(
                    "skipping chunk %s: embedding dimension %d != %d", row["id"], vec.size, dim
                )
                continue
            ids.append(row["id"])
            vectors.append(vec)
        if not vectors:
            return None
        return ids, np.vstack(vectors)

    # --- bookkeeping -----------------------------------------------------------------------

    def record_ingest_run(self, summary: dict) -> None:
        started = str(summary.get("started_at") or _now_iso())
        finished = str(summary.get("finished_at") or _now_iso())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO ingest_runs (started_at, finished_at, summary_json) VALUES (?, ?, ?)",
                (started, finished, json.dumps(summary, default=str)),
            )

    def log_question(
        self,
        session_id: str | None,
        question: str,
        coverage: str,
        flags: list[str],
        not_covered: bool,
        usage: dict[str, int],
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO questions_log (asked_at, session_id, question, coverage, flags_json,"
                " not_covered, input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now_iso(),
                    session_id,
                    question,
                    coverage,
                    json.dumps(sorted(set(flags))),
                    1 if not_covered else 0,
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                ),
            )

    def list_gaps(self, limit: int = 20) -> list[dict]:
        """Questions Mark could not answer from the sources, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT asked_at, question, coverage, flags_json FROM questions_log"
                " WHERE not_covered = 1 ORDER BY asked_at DESC, id DESC LIMIT ?",
                (max(0, int(limit)),),
            ).fetchall()
        return [
            {
                "asked_at": r["asked_at"],
                "question": r["question"],
                "coverage": r["coverage"],
                "flags": json.loads(r["flags_json"] or "[]"),
            }
            for r in rows
        ]

    def stats(self) -> StoreStats:
        with self._lock:
            by_kind = {
                r["kind"]: r["n"]
                for r in self._conn.execute(
                    "SELECT kind, COUNT(*) AS n FROM documents GROUP BY kind"
                ).fetchall()
            }
            chunk_row = self._conn.execute(
                "SELECT COUNT(*) AS n,"
                " SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded FROM chunks"
            ).fetchone()
            model_rows = self._conn.execute(
                "SELECT embedding_model, COUNT(*) AS n FROM chunks"
                " WHERE embedding_model IS NOT NULL GROUP BY embedding_model"
            ).fetchall()
            last_run = self._conn.execute(
                "SELECT MAX(finished_at) AS t FROM ingest_runs"
            ).fetchone()
            q_row = self._conn.execute(
                "SELECT COUNT(*) AS total, SUM(not_covered) AS gaps,"
                " SUM(input_tokens + output_tokens) AS tokens FROM questions_log"
            ).fetchone()
        model_counts = Counter({r["embedding_model"]: r["n"] for r in model_rows})
        embedding_model = model_counts.most_common(1)[0][0] if model_counts else None
        return StoreStats(
            documents_by_kind={k.value: int(by_kind.get(k.value, 0)) for k in SourceKind},
            chunks=int(chunk_row["n"] or 0),
            embedded_chunks=int(chunk_row["embedded"] or 0),
            last_ingest_at=last_run["t"] if last_run and last_run["t"] else None,
            embedding_model=embedding_model,
            questions_total=int(q_row["total"] or 0),
            questions_not_covered=int(q_row["gaps"] or 0),
            tokens_total=int(q_row["tokens"] or 0),
        )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - closing twice is harmless
                logger.debug("store already closed", exc_info=True)

    def __enter__(self) -> KnowledgeStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
