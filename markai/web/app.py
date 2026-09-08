"""FastAPI app behind ``mark serve``: a small chat UI plus a streaming JSON API.

Importing this module never needs an API key or a populated knowledge base. The advisor is
built on the first chat request so ``uvicorn markai.web.app:app`` always starts.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from markai.advisor.guardrails import IDENTITY_NOTICE

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


class Attachment(BaseModel):
    """One file on its way into a single question. Never stored."""

    name: str = Field(default="file", max_length=200)
    media_type: str = Field(default="", max_length=100)
    data: str = Field(default="", max_length=30_000_000)  # base64, checked in the decoder


class ChatRequest(BaseModel):
    session_id: str = Field(default="default", max_length=128)
    message: str = Field(default="")
    attachments: list[Attachment] = Field(default_factory=list, max_length=10)


class ResetRequest(BaseModel):
    session_id: str = Field(default="default", max_length=128)


class _Sessions:
    """LRU map of session id to (conversation, lock, question count)."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: OrderedDict[str, list[Any]] = OrderedDict()
        self._guard = threading.Lock()

    def get(self, session_id: str) -> list[Any]:
        from markai.advisor.mark import Conversation

        with self._guard:
            entry = self._items.get(session_id)
            if entry is None:
                entry = [Conversation(session_id=session_id), threading.Lock(), 0]
                self._items[session_id] = entry
            self._items.move_to_end(session_id)
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
            return entry

    def reset(self, session_id: str) -> None:
        from markai.advisor.mark import Conversation

        with self._guard:
            entry = self._items.get(session_id)
            if entry is not None:
                entry[0] = Conversation(session_id=session_id)
                entry[2] = 0


class _DailyCounter:
    """Global question counter that resets at UTC midnight."""

    def __init__(self) -> None:
        self._day = ""
        self._count = 0
        self._guard = threading.Lock()

    def bump(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        with self._guard:
            if today != self._day:
                self._day, self._count = today, 0
            self._count += 1
            return self._count


def create_app(
    settings: Any | None = None,
    advisor: Any | None = None,
    store: Any | None = None,
) -> FastAPI:
    """Build the app. Pass ``advisor`` to inject a fake in tests."""
    from markai.config import get_settings

    settings = settings or get_settings()
    app = FastAPI(title="Mark", docs_url=None, redoc_url=None)

    state: dict[str, Any] = {
        "advisor": advisor,
        "store": store,
        "advisor_error": None,
        "history": None,
        "retriever": None,
    }
    sessions = _Sessions(settings.max_sessions)
    daily = _DailyCounter()

    def require_access(x_access_code: str | None = Header(default=None)) -> None:
        expected = settings.access_code()
        if not expected:
            return
        if not x_access_code or not hmac.compare_digest(x_access_code, expected):
            raise HTTPException(status_code=401, detail="Access code required.")

    def browser_of(x_browser_id: str | None = Header(default=None)) -> str:
        """Which browser is asking. Sent as a header so ids stay out of the request log."""
        return (x_browser_id or "").strip()[:128]

    def get_history() -> Any:
        if state["history"] is None:
            from markai.web.history import History

            settings.ensure_dirs()
            state["history"] = History(settings.data_dir / "conversations.db")
        return state["history"]

    def get_store() -> Any:
        if state["store"] is None:
            from markai.knowledge.store import KnowledgeStore

            settings.ensure_dirs()
            state["store"] = KnowledgeStore(settings.db_path)
        return state["store"]

    def get_retriever() -> Any:
        """Shared with the advisor: loading the corpus twice would double the memory."""
        if state["retriever"] is None:
            from markai.knowledge.embeddings import build_embedder
            from markai.knowledge.retriever import Retriever

            state["retriever"] = Retriever(get_store(), build_embedder(settings), settings)
        return state["retriever"]

    def get_advisor() -> Any:
        """Build the advisor on first use so import never needs credentials."""
        if state["advisor"] is not None:
            return state["advisor"]
        from markai.advisor.mark import MarkAdvisor
        from markai.advisor.prompt_builder import load_system_prompt
        from markai.sources.manifest import load_manifest

        manifest = load_manifest(settings.sources_file)
        current_store = get_store()
        state["advisor"] = MarkAdvisor(
            settings,
            get_retriever(),
            manifest.tools,
            load_system_prompt(settings.system_prompt_path, settings.show_citations),
            business=manifest.business,
            store=current_store,
        )
        return state["advisor"]

    # -- routes -------------------------------------------------------------------------

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health(_: None = Depends(require_access)) -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/api/status")
    def status(_: None = Depends(require_access)) -> dict[str, Any]:
        stats = get_store().stats()
        return {
            "model": settings.model,
            "effort": settings.effort,
            "embedding_model": stats.embedding_model,
            "embeddings_enabled": bool(settings.voyage_key()),
            "api_key_set": bool(settings.anthropic_key()),
            "documents_by_kind": stats.documents_by_kind,
            "chunks": stats.chunks,
            "embedded_chunks": stats.embedded_chunks,
            "last_ingest_at": stats.last_ingest_at,
            "questions_total": stats.questions_total,
            "questions_not_covered": stats.questions_not_covered,
            "identity_notice": IDENTITY_NOTICE,
            "access_code_required": bool(settings.access_code()),
        }

    @app.get("/api/sources")
    def sources(_: None = Depends(require_access)) -> dict[str, Any]:
        documents = get_store().list_documents()
        return {
            "sources": [
                {
                    "kind": doc.kind.value,
                    "title": doc.title,
                    "link": doc.link,
                    "episode": doc.episode,
                    "published_at": doc.published_at,
                }
                for doc in documents
            ]
        }

    @app.get("/api/episodes")
    def episodes(
        q: str = "",
        guest: str = "",
        limit: int = 5,
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        """A topic search over the transcripts, or the catalog when there is no topic."""
        from markai.knowledge.episodes import catalog, find_moments

        limit = min(max(limit, 1), 50)
        if q.strip():
            found = find_moments(get_retriever(), q, limit=limit)
            return {"query": q, "episodes": [m.to_dict() for m in found]}
        listed = catalog(get_store(), guest=guest or None, limit=limit)
        return {"query": "", "episodes": [e.to_dict() for e in listed]}

    @app.get("/api/gaps")
    def gaps(limit: int = 20, _: None = Depends(require_access)) -> dict[str, Any]:
        return {"gaps": get_store().list_gaps(min(max(limit, 1), 200))}

    @app.get("/api/threads")
    def threads(
        browser: str = Depends(browser_of), _: None = Depends(require_access)
    ) -> dict[str, Any]:
        return {"threads": [t.to_dict() for t in get_history().list(browser)]}

    @app.get("/api/threads/{thread_id}")
    def thread(
        thread_id: str,
        browser: str = Depends(browser_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        found = get_history().get(browser, thread_id)
        if found is None:
            raise HTTPException(status_code=404, detail="No such conversation.")
        return found.to_dict(with_messages=True)

    @app.delete("/api/threads/{thread_id}")
    def forget_thread(
        thread_id: str,
        browser: str = Depends(browser_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        return {"deleted": get_history().delete(browser, thread_id)}

    @app.post("/api/reset")
    def reset(payload: ResetRequest, _: None = Depends(require_access)) -> dict[str, Any]:
        sessions.reset(payload.session_id)
        return {"status": "reset"}

    @app.post("/api/chat")
    def chat(
        payload: ChatRequest,
        browser: str = Depends(browser_of),
        _: None = Depends(require_access),
    ) -> EventSourceResponse:
        from markai.advisor.attachments import AttachmentError, decode_all

        message = (payload.message or "").strip()
        try:
            files = decode_all([a.model_dump() for a in payload.attachments])
        except AttachmentError as exc:
            # The message names the file and the limit, so it is safe and useful to show.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not message and not files:
            raise HTTPException(status_code=400, detail="Ask a question first.")
        if not message:
            message = "Take a look at this and tell me what you see."
        if len(message) > settings.max_question_chars:
            raise HTTPException(
                status_code=413,
                detail=f"Questions are limited to {settings.max_question_chars} characters.",
            )
        if daily.bump() > settings.daily_question_limit:
            raise HTTPException(
                status_code=429, detail="Mark has hit today's question limit. Try again tomorrow."
            )

        entry = sessions.get(payload.session_id)
        conversation, lock, asked = entry
        if asked >= settings.per_session_question_limit:
            raise HTTPException(
                status_code=429,
                detail="This conversation has hit its question limit. Start a new one.",
            )
        if not lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="This conversation is still answering.")
        entry[2] = asked + 1

        record: Callable[[str, str], None] | None = None
        if browser:
            history = get_history()
            thread_id = payload.session_id

            def record(question: str, answer: str) -> None:
                history.record(browser, thread_id, question, answer)

        return EventSourceResponse(_events(get_advisor, conversation, message, lock, files, record))

    return app


def _events(
    get_advisor,
    conversation: Any,
    message: str,
    lock: threading.Lock,
    attachments: list[Any] | None = None,
    record: Callable[[str, str], None] | None = None,
) -> Iterator[dict]:
    from markai.advisor.mark import MissingApiKeyError

    try:
        try:
            advisor = get_advisor()
        except MissingApiKeyError as exc:
            yield {"event": "error", "data": json.dumps({"message": str(exc)})}
            return
        except FileNotFoundError as exc:
            yield {"event": "error", "data": json.dumps({"message": str(exc)})}
            return

        response = None
        for event in advisor.stream(message, conversation, attachments):
            if event.type == "text":
                yield {"event": "text", "data": json.dumps({"text": event.text})}
            elif event.type == "tool_call":
                yield {"event": "tool", "data": json.dumps({"name": event.text})}
            elif event.type == "error":
                yield {"event": "error", "data": json.dumps({"message": event.text})}
                return
            elif event.type == "final":
                response = event.response

        if response is None:
            yield {"event": "error", "data": json.dumps({"message": "No answer was produced."})}
            return

        if record is not None:
            record(message, response.text)

        yield {
            "event": "citations",
            "data": json.dumps({"citations": [c.to_dict() for c in response.citations]}),
        }
        yield {
            "event": "done",
            "data": json.dumps(
                {
                    "text": response.text,
                    "coverage": response.coverage,
                    "flags": response.flags,
                    "usage": response.usage,
                    "model": response.model,
                    "stop_reason": response.stop_reason,
                }
            ),
        }
    except Exception as exc:  # a crash here must not hang the browser
        logger.exception("chat stream failed")
        yield {"event": "error", "data": json.dumps({"message": f"Something went wrong: {exc}"})}
    finally:
        lock.release()


app = create_app()
