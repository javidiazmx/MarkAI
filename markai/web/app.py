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
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from markai.advisor.guardrails import FLAG_PM_INTEREST, IDENTITY_NOTICE

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE = "mark_auth"


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


class SignupRequest(BaseModel):
    """The lead form. Validated again in the store, which owns the rules."""

    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=400)
    phone: str = Field(default="", max_length=60)
    neighborhood: str = Field(default="", max_length=200)


class FeedbackRequest(BaseModel):
    """What a landlord thought of the last answer in a thread."""

    session_id: str = Field(default="default", max_length=128)
    rating: str = Field(default="", max_length=8)
    note: str = Field(default="", max_length=1000)


class LogRequest(BaseModel):
    """One thing that happened, as typed into the page. The store owns the rules."""

    id: str = Field(default="", max_length=64)
    kind: str = Field(default="note", max_length=40)
    what: str = Field(default="", max_length=1000)
    amount: str = Field(default="", max_length=20)
    vendor: str = Field(default="", max_length=200)
    date: str = Field(default="", max_length=20)
    status: str = Field(default="", max_length=10)
    property_id: str = Field(default="", max_length=64)


class PropertyRequest(BaseModel):
    """One building as typed into the page. Lengths are trimmed again in the store."""

    id: str = Field(default="", max_length=64)
    label: str = Field(default="", max_length=200)
    units: str = Field(default="", max_length=10)
    city: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=1000)


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

    # Filled in below, once the loaders it needs exist. Lifespan has to be handed to the
    # constructor, and the constructor comes first.
    warm: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        start = warm.get("start")
        if start:
            start()
        yield

    app = FastAPI(title="Mark", docs_url=None, redoc_url=None, lifespan=lifespan)

    state: dict[str, Any] = {
        "advisor": advisor,
        "store": store,
        "advisor_error": None,
        "history": None,
        "retriever": None,
        "portfolio": None,
        "ledger": None,
        "accounts": None,
        "crm": None,
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

    def signed_in_of(mark_auth: str | None = Cookie(default=None)) -> Any:
        """The account behind the sign-in cookie, or None. HttpOnly: no script reads it."""
        if not settings.account_required:
            return None
        return get_accounts().account_for_token(mark_auth)

    def owner_of(browser: str = Depends(browser_of), account: Any = Depends(signed_in_of)) -> str:
        """Who this request belongs to.

        The account once someone is signed in, so their conversations and properties follow
        them to another machine; the browser while they are anonymous. Everything that
        stores anything keys on this and never on the browser directly.
        """
        from markai.web.accounts import anonymous_owner

        return account.owner_id if account is not None else anonymous_owner(browser)

    def get_history() -> Any:
        if state["history"] is None:
            from markai.web.history import History

            settings.ensure_dirs()
            state["history"] = History(settings.data_dir / "conversations.db")
        return state["history"]

    def get_crm() -> Any:
        if state["crm"] is None:
            from markai.web.crm import Crm, sender_from_settings

            settings.ensure_dirs()
            sender, describe = sender_from_settings(settings)
            state["crm"] = Crm(settings.data_dir / "leads.db", sender=sender, describe=describe)
        return state["crm"]

    def get_accounts() -> Any:
        if state["accounts"] is None:
            from markai.web.accounts import Accounts

            settings.ensure_dirs()
            state["accounts"] = Accounts(
                settings.data_dir / "accounts.db",
                free_questions=settings.free_questions_before_signup,
                session_days=settings.session_days,
            )
        return state["accounts"]

    def get_portfolio() -> Any:
        if state["portfolio"] is None:
            from markai.web.portfolio import Portfolio

            settings.ensure_dirs()
            state["portfolio"] = Portfolio(settings.data_dir / "portfolio.db")
        return state["portfolio"]

    def get_ledger() -> Any:
        if state["ledger"] is None:
            from markai.web.ledger import Ledger

            settings.ensure_dirs()
            state["ledger"] = Ledger(settings.data_dir / "ledger.db")
        return state["ledger"]

    def owner_log(owner: str) -> Any:
        """The log, bound to one owner and their buildings, or None for a stranger."""
        if not owner:
            return None
        from markai.web.ledger import OwnerLog

        return OwnerLog(get_ledger(), owner, get_portfolio().list(owner))

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
        from markai.sources.facts import facts_path, load_facts
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
            facts=load_facts(facts_path(settings.sources_file)),
        )
        return state["advisor"]

    def warm_up() -> None:
        """Load the corpus before anyone asks, not during their first question.

        Building the BM25 index over every passage and normalising the embedding matrix
        takes seconds on a real knowledge base, and paying that inside the first question
        is the worst possible moment: it is the one where somebody is deciding whether this
        thing works. On a background thread so the server still answers immediately, and
        failures are logged and left alone: the lazy path still runs, so a warm-up problem
        delays an answer rather than preventing one.
        """
        if advisor is not None:  # a test injected one; there is nothing to load
            return

        def load() -> None:
            try:
                get_retriever()
                get_advisor()
                logger.info("warm: the knowledge base is loaded and the advisor is built")
            except Exception as exc:  # missing key, empty store: the first ask will say so
                logger.info("warm-up skipped: %s", exc)

        threading.Thread(target=load, name="markai-warmup", daemon=True).start()

    warm["start"] = warm_up

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
        owner: str = Depends(owner_of), _: None = Depends(require_access)
    ) -> dict[str, Any]:
        return {"threads": [t.to_dict() for t in get_history().list(owner)]}

    @app.get("/api/threads/{thread_id}")
    def thread(
        thread_id: str,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        found = get_history().get(owner, thread_id)
        if found is None:
            raise HTTPException(status_code=404, detail="No such conversation.")
        return found.to_dict(with_messages=True)

    @app.delete("/api/threads/{thread_id}")
    def forget_thread(
        thread_id: str,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        return {"deleted": get_history().delete(owner, thread_id)}

    def _set_session(response: Response, token: str) -> None:
        """HttpOnly so no script can read it, Lax so no other site can post with it."""
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.session_days * 86400,
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
            path="/",
        )

    @app.get("/api/account")
    def account(
        owner: str = Depends(owner_of),
        signed_in: Any = Depends(signed_in_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        """Who is signed in, and how many free questions are left if nobody is."""
        if not settings.account_required:
            return {"required": False, "signed_in": True, "free_left": None, "name": None}
        accounts = get_accounts()
        return {
            "required": True,
            "signed_in": signed_in is not None,
            "free_left": accounts.free_left(owner),
            "free_questions": accounts.free_questions,
            # The name to greet them with and the username they signed in as. No more.
            "name": signed_in.name if signed_in else None,
            "email": signed_in.email if signed_in else None,
        }

    @app.post("/api/account")
    def create_account(
        payload: SignupRequest,
        response: Response,
        browser: str = Depends(browser_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        from markai.web.accounts import SignupError, anonymous_owner

        accounts = get_accounts()
        # Checked before the row exists, so one person filling the form on their phone and
        # their laptop reaches the CRM once.
        already_a_lead = accounts.seen_before(payload.email)
        try:
            saved, token = accounts.create(payload.model_dump())
        except SignupError as exc:
            # The message names the field, so it is meant to be shown.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # The two questions they asked before signing up are theirs. Claimed here and only
        # here: on a later sign-in the anonymous rows could be whoever used this machine.
        anonymous = anonymous_owner(browser)
        get_history().reassign(anonymous, saved.owner_id)
        get_portfolio().reassign(anonymous, saved.owner_id)
        get_ledger().reassign(anonymous, saved.owner_id)
        if not already_a_lead:
            # After the reassign, so the lead can carry what they already asked about.
            _queue_lead(saved)
        _set_session(response, token)
        return {"signed_in": True, "name": saved.name, "email": saved.email}

    def _queue_lead(account: Any) -> None:
        """Write the lead down, then push it in the background. Never blocks the signup."""
        from markai.web.crm import build_payload

        context: dict[str, Any] = {}
        threads = get_history().list(account.owner_id, limit=3)
        if threads:
            # What they wanted, not just who they are. Titles come from their own words.
            context["asked_about"] = threads[-1].title
            context["questions"] = sum(t.turns for t in threads)
        properties = get_portfolio().list(account.owner_id)
        if properties:
            context["properties"] = "; ".join(p.one_line() for p in properties)
        try:
            crm = get_crm()
            crm.enqueue(account.id, build_payload(account, context))
            crm.deliver_soon()
        except Exception:  # a CRM problem is never a failed signup
            logger.exception("could not queue the lead")

    def _queue_candidate_lead(account: Any, signal: str, reason: str) -> None:
        """A landlord showed a sign they might be worth a call about a property manager.

        Same queue as a signup lead, one alert per signal per account (`already_sent`), and
        the same rule: a CRM problem is never allowed to break the chat or the property they
        were adding.
        """
        from markai.web.crm import build_payload

        try:
            crm = get_crm()
            if crm.already_sent(account.id, signal):
                return
            crm.enqueue(account.id, build_payload(account, {"signal": signal, "reason": reason}))
            crm.deliver_soon()
        except Exception:
            logger.exception("could not queue a candidate lead")

    @app.get("/api/log")
    def read_log(
        property_id: str = "",
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        """Their log for the panel: what happened, what is still open, what it adds up to."""
        log = get_ledger()
        labels = {p.id: p.label for p in get_portfolio().list(owner)}

        def rendered(entry: Any) -> dict[str, Any]:
            data = entry.to_dict()
            data["property_label"] = labels.get(entry.property_id, "")
            return data

        return {
            "entries": [rendered(e) for e in log.list(owner, property_id=property_id, limit=60)],
            "open": [rendered(e) for e in log.open_items(owner, limit=20)],
            "totals": log.totals(owner, property_id=property_id),
            "count": log.count(owner),
        }

    @app.post("/api/log")
    def add_log(
        payload: LogRequest,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        from markai.web.ledger import LogError

        try:
            saved = get_ledger().add(owner, payload.model_dump())
        except LogError as exc:
            # The message names the field and the limit, so it is useful to show.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"entry": saved.to_dict()}

    @app.post("/api/log/{entry_id}/done")
    def close_log(
        entry_id: str,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        return {"closed": get_ledger().close(owner, entry_id)}

    @app.delete("/api/log/{entry_id}")
    def forget_log(
        entry_id: str,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        """Deleting is a person's decision, which is why it is here and not a tool."""
        return {"deleted": get_ledger().delete(owner, entry_id)}

    @app.get("/api/properties")
    def properties(
        owner: str = Depends(owner_of), _: None = Depends(require_access)
    ) -> dict[str, Any]:
        return {"properties": [p.to_dict() for p in get_portfolio().list(owner)]}

    @app.post("/api/properties")
    def add_property(
        payload: PropertyRequest,
        owner: str = Depends(owner_of),
        signed_in: Any = Depends(signed_in_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        from markai.web.portfolio import PropertyError

        try:
            saved = get_portfolio().add(owner, payload.model_dump())
        except PropertyError as exc:
            # The message names the field and the limit, so it is useful to show.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if signed_in is not None and len(get_portfolio().list(owner)) == 2:
            # Exactly 2, not >= 2: this fires once, the turn a single-building landlord
            # becomes a portfolio. A tenth building is not new information.
            _queue_candidate_lead(signed_in, "portfolio_growth", "Now manages 2 properties")
        return {"property": saved.to_dict()}

    @app.delete("/api/properties/{property_id}")
    def forget_property(
        property_id: str,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        return {"deleted": get_portfolio().delete(owner, property_id)}

    @app.post("/api/feedback")
    def feedback(
        payload: FeedbackRequest,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        """A thumb on the last answer. A down is a content gap the owner can act on."""
        saved = get_history().rate(owner, payload.session_id, payload.rating, payload.note)
        return {"saved": saved}

    @app.post("/api/handoff")
    def handoff(
        payload: ResetRequest,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        """Case notes for the property manager, built from what is already stored."""
        from markai.sources.manifest import load_manifest
        from markai.web.handoff import build_handoff

        name, url = None, None
        try:
            business = load_manifest(settings.sources_file).business
            name, url = business.escalation_name, business.escalation_url
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("no escalation contact available: %s", exc)

        return {
            "text": build_handoff(
                get_history().get(owner, payload.session_id),
                get_portfolio().list(owner),
                name=name,
                url=url,
                open_items=owner_log(owner).open_items() if owner else None,
            ),
            "name": name,
            "url": url,
        }

    @app.post("/api/reset")
    def reset(payload: ResetRequest, _: None = Depends(require_access)) -> dict[str, Any]:
        sessions.reset(payload.session_id)
        return {"status": "reset"}

    @app.post("/api/chat")
    def chat(
        payload: ChatRequest,
        owner: str = Depends(owner_of),
        signed_in: Any = Depends(signed_in_of),
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

        if settings.account_required and get_accounts().needs_signup(owner):
            # 403 with a marker rather than 401: the page has to tell a spent free trial
            # apart from a missing access code, and they mean different things.
            raise HTTPException(
                status_code=403,
                detail={
                    "signup_required": True,
                    "message": (
                        "That is your free questions used up. A free account keeps Jay "
                        "going, and it takes a minute."
                    ),
                },
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
        if owner:
            history = get_history()
            accounts = get_accounts()
            thread_id = payload.session_id

            def record(question: str, answer: str) -> None:
                history.record(owner, thread_id, question, answer)
                # Counted on the way out, not on the way in: a question that failed to
                # produce an answer has not spent anything.
                accounts.count_question(owner)

        on_flags: Callable[[list[str]], None] | None = None
        if signed_in is not None:

            def on_flags(flags: list[str]) -> None:
                if FLAG_PM_INTEREST in flags:
                    _queue_candidate_lead(
                        signed_in, "pm_interest", "Asked Jay about hiring a property manager"
                    )

        return EventSourceResponse(
            _events(
                get_advisor,
                conversation,
                message,
                lock,
                files,
                record,
                get_portfolio().list(owner) if owner else None,
                signed_in.neighborhood if signed_in else None,
                # What they asked about before, in their own words. Costs nothing: the
                # titles already exist, and this is what lets Jay say "same Berwyn unit?".
                get_history().recent_topics(owner, exclude=payload.session_id) if owner else None,
                # Their own record of the buildings: what it cost, who came out, what is
                # still open. Jay can add to it and search it while answering.
                owner_log(owner),
                on_flags,
            )
        )

    return app


def _events(
    get_advisor,
    conversation: Any,
    message: str,
    lock: threading.Lock,
    attachments: list[Any] | None = None,
    record: Callable[[str, str], None] | None = None,
    portfolio: list[Any] | None = None,
    neighborhood: str | None = None,
    remembered: list[tuple[str, float]] | None = None,
    log: Any | None = None,
    on_flags: Callable[[list[str]], None] | None = None,
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
        for event in advisor.stream(
            message, conversation, attachments, portfolio, neighborhood, remembered, log
        ):
            if event.type == "text":
                yield {"event": "text", "data": json.dumps({"text": event.text})}
            elif event.type == "thinking":
                yield {"event": "thinking", "data": json.dumps({"text": event.text})}
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

        if on_flags is not None:
            try:
                on_flags(response.flags)
            except Exception:  # a lead-alert problem must not break the answer
                logger.exception("could not check the answer for a candidate signal")

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
