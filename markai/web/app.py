"""FastAPI app behind ``mark serve``: a small chat UI plus a streaming JSON API.

Importing this module never needs an API key or a populated knowledge base. The advisor is
built on the first chat request so ``uvicorn markai.web.app:app`` always starts.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from markai.advisor.guardrails import (
    FLAG_BUYING_INTEREST,
    FLAG_PM_INTEREST,
    FLAG_SELF_MANAGE_BURNOUT,
    FLAG_TENANT_TROUBLE,
    FLAG_VACANCY_HELP,
    IDENTITY_NOTICE,
)

PM_FIT_FLAGS = {
    FLAG_PM_INTEREST: "Asked about hiring a property manager",
    FLAG_TENANT_TROUBLE: "Described a problem with a current tenant",
    FLAG_SELF_MANAGE_BURNOUT: "Sounded burned out managing it themselves",
    FLAG_VACANCY_HELP: "Struggling to fill a vacancy",
    FLAG_BUYING_INTEREST: "Actively shopping for a rental to buy",
}

logger = logging.getLogger(__name__)

# A cell that opens with one of these is a formula to Excel/Sheets, not text - and every
# field in the admin export can hold text nobody here wrote: a landlord's own name, or an
# AI-generated reasoning string built from their words. A leading apostrophe is the standard
# defusal; it displays as-is and never evaluates.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _csv_safe(value: Any) -> str:
    text = str(value)
    return f"'{text}" if text.startswith(_CSV_FORMULA_PREFIXES) else text


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
    urgency: str = Field(default="", max_length=10)


class VendorCostRequest(BaseModel):
    """Filling in who is doing a logged job and what it costs, once that is known."""

    vendor: str = Field(default="", max_length=200)
    amount: str = Field(default="", max_length=20)


class LogEditRequest(BaseModel):
    """Correcting an entry already on the log - a typo, the real invoice amount, a vendor
    added after the fact, a wrong date. Never touches kind, status, property, a photo, or
    a completion time - see ``Ledger.update`` for why."""

    what: str = Field(default="", max_length=1000)
    amount: str = Field(default="", max_length=20)
    vendor: str = Field(default="", max_length=200)
    date: str = Field(default="", max_length=20)


class PmFitOverrideRequest(BaseModel):
    """The staff checkbox. ``None`` clears the override back to "follow the AI"."""

    good_fit: bool | None = None


class PmFitNotesRequest(BaseModel):
    """Free-text staff notes on one visitor. Trimmed and length-capped in the store."""

    notes: str = Field(default="", max_length=4000)


class PmFitHiddenRequest(BaseModel):
    """Soft-hide (or restore) a visitor from the default admin view."""

    hidden: bool = False


class BulkHiddenRequest(BaseModel):
    """Hide or restore several visitors at once, from a multi-select in the admin table."""

    owner_ids: list[str] = Field(default_factory=list, max_length=500)
    hidden: bool = False


class DealRequest(BaseModel):
    """The Deal Analyzer form. Field names and defaults mirror ``calculators.analyze_deal``
    exactly, so the web panel, ``mark calc deal``, and the AI tool-call all agree."""

    price: float = Field(gt=0)
    down_payment_pct: float = Field(default=0.25, ge=0, le=1)
    annual_rate: float = Field(default=0.07, ge=0, le=1)
    years: int = Field(default=30, gt=0, le=50)
    monthly_rent: float = Field(ge=0)
    other_income_monthly: float = Field(default=0.0, ge=0)
    vacancy_rate: float = Field(default=0.05, ge=0, le=1)
    taxes_annual: float = Field(default=0.0, ge=0)
    insurance_annual: float = Field(default=0.0, ge=0)
    maintenance_pct: float = Field(default=0.05, ge=0, le=1)
    capex_pct: float = Field(default=0.05, ge=0, le=1)
    management_pct: float = Field(default=0.0, ge=0, le=1)
    hoa_monthly: float = Field(default=0.0, ge=0)
    utilities_monthly: float = Field(default=0.0, ge=0)
    closing_costs: float = Field(default=0.0, ge=0)
    rehab: float = Field(default=0.0, ge=0)


class NoticeWizardRequest(BaseModel):
    """The Notice Wizard's guided picks - jurisdiction and reason are closed lists checked
    server-side, tenure is optional (only the no-cause reason uses it)."""

    jurisdiction: str
    reason: str
    tenure_years: float | None = Field(default=None, ge=0, le=100)


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


class _AdminAttemptLimiter:
    """A basic brute-force backoff on the admin code header.

    Unlike the landlord access code, this one gate sits in front of every landlord's
    contact details at once - `hmac.compare_digest` alone makes guessing no faster than
    trying codes one at a time, but nothing before this stopped an unattended script from
    doing exactly that. Bounded to a fixed number of addresses (LRU-evicted) so a spoofed
    flood of source addresses cannot grow this without bound.
    """

    _MAX_ATTEMPTS = 5
    _LOCKOUT_SECONDS = 60.0
    _TRACKED_ADDRESSES = 1000

    def __init__(self) -> None:
        self._attempts: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._guard = threading.Lock()

    def blocked(self, address: str) -> bool:
        with self._guard:
            entry = self._attempts.get(address)
            if entry is None:
                return False
            count, locked_until = entry
            return count >= self._MAX_ATTEMPTS and time.monotonic() < locked_until

    def record_failure(self, address: str) -> None:
        with self._guard:
            count, _ = self._attempts.get(address, (0, 0.0))
            self._attempts[address] = (count + 1, time.monotonic() + self._LOCKOUT_SECONDS)
            self._attempts.move_to_end(address)
            while len(self._attempts) > self._TRACKED_ADDRESSES:
                self._attempts.popitem(last=False)

    def record_success(self, address: str) -> None:
        with self._guard:
            self._attempts.pop(address, None)


class _RateLimiter:
    """A basic per-address flood guard, for the two forms reachable with no access code
    configured at all (account signup, the property-manager handoff) - not a security
    boundary the way the admin lockout above is, just a floor against one script
    hammering either one. A burst over the cap is refused until the window rolls over,
    then it simply works again; nothing is locked out.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._hits: OrderedDict[str, list[float]] = OrderedDict()
        self._guard = threading.Lock()

    def allow(self, address: str) -> bool:
        now = time.monotonic()
        with self._guard:
            hits = [t for t in self._hits.get(address, []) if now - t < self._window_seconds]
            allowed = len(hits) < self._max_requests
            if allowed:
                hits.append(now)
            self._hits[address] = hits
            self._hits.move_to_end(address)
            while len(self._hits) > 1000:
                self._hits.popitem(last=False)
            return allowed


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
        "admin_store": None,
        "pm_fit_sender": None,
        "facts": None,
    }
    sessions = _Sessions(settings.max_sessions)
    daily = _DailyCounter()
    admin_attempts = _AdminAttemptLimiter()
    form_rate_limiter = _RateLimiter(max_requests=10, window_seconds=60.0)

    def require_access(x_access_code: str | None = Header(default=None)) -> None:
        expected = settings.access_code()
        if not expected:
            return
        if not x_access_code or not hmac.compare_digest(x_access_code, expected):
            raise HTTPException(status_code=401, detail="Access code required.")

    def require_admin_access(
        request: Request, x_admin_code: str | None = Header(default=None)
    ) -> None:
        """Independent of `require_access`: knowing the landlord code grants nothing here.

        Unlike the landlord gate, an unset code refuses rather than allows - this surface
        holds every landlord's contact details at once, so it must never be reachable with
        no code configured at all. Repeated wrong guesses from one address are throttled
        for the same reason - see `_AdminAttemptLimiter`.
        """
        address = client_ip_of(request)
        if admin_attempts.blocked(address):
            raise HTTPException(status_code=429, detail="Too many attempts. Try again shortly.")
        expected = settings.admin_code()
        if not expected or not x_admin_code or not hmac.compare_digest(x_admin_code, expected):
            admin_attempts.record_failure(address)
            raise HTTPException(status_code=401, detail="Admin code required.")
        admin_attempts.record_success(address)

    def rate_limit_public_forms(request: Request) -> None:
        if not form_rate_limiter.allow(client_ip_of(request)):
            raise HTTPException(status_code=429, detail="Too many requests. Try again shortly.")

    def browser_of(x_browser_id: str | None = Header(default=None)) -> str:
        """Which browser is asking. Sent as a header so ids stay out of the request log."""
        return (x_browser_id or "").strip()[:128]

    def client_ip_of(request: Request) -> str:
        """The visitor's own address, for the admin panel's "location by IP" lookup.

        `request.client.host` is the last hop, which behind a proxy or a Cloudflare tunnel
        is the tunnel's own address, not theirs. `Cf-Connecting-Ip` (Cloudflare) and the
        first hop of `X-Forwarded-For` (everything else) both carry the original address
        when one is present, so they are tried first.
        """
        forwarded_for = request.headers.get("x-forwarded-for", "")
        candidate = (
            request.headers.get("cf-connecting-ip", "").strip()
            or forwarded_for.split(",")[0].strip()
            or (request.client.host if request.client else "")
        )
        return candidate[:64]

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

    def get_admin_store() -> Any:
        if state["admin_store"] is None:
            from markai.web.admin_store import AdminStore

            settings.ensure_dirs()
            state["admin_store"] = AdminStore(settings.data_dir / "admin.db")
        return state["admin_store"]

    def get_pm_fit_sender() -> Any:
        if state["pm_fit_sender"] is None:
            from markai.web.admin_store import pm_fit_alert_sender

            state["pm_fit_sender"] = pm_fit_alert_sender(settings) or False
        return state["pm_fit_sender"] or None

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

    def get_facts() -> Any:
        """Loaded once, same as the advisor's own copy - the Notice Wizard needs only this,
        never the knowledge base or a model call, so it never waits on either."""
        if state["facts"] is None:
            from markai.sources.facts import facts_path, load_facts

            state["facts"] = load_facts(facts_path(settings.sources_file))
        return state["facts"]

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

    @app.get("/api/nudges")
    def nudges(_: None = Depends(require_access)) -> dict[str, Any]:
        """Small, dismissible, time-relevant reminders - proactive, not just reactive."""
        from markai.web.nudges import active_nudges

        return {"nudges": active_nudges()}

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
    async def create_account(
        payload: SignupRequest,
        response: Response,
        browser: str = Depends(browser_of),
        _: None = Depends(require_access),
        __: None = Depends(rate_limit_public_forms),
    ) -> dict[str, Any]:
        from markai.web.accounts import (
            SignupError,
            anonymous_owner,
            ensure_domain_is_reachable,
            parse,
        )

        accounts = get_accounts()
        # Checked before the row exists, so one person filling the form on their phone and
        # their laptop reaches the CRM once.
        already_a_lead = accounts.seen_before(payload.email)
        try:
            # Local checks first - a malformed email or an obviously fake phone should
            # never reach a network call at all. Only once the form is well-formed does
            # the domain get checked, and that check is awaited rather than blocking: a
            # domain with unresponsive DNS must not be able to tie up a worker thread from
            # the pool every other route shares.
            account = parse(payload.model_dump())
            await ensure_domain_is_reachable(account.email)
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
        get_admin_store().reassign(anonymous, saved.owner_id)
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

    @app.get("/admin")
    def admin_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "admin.html")

    @app.get("/theme.css")
    def theme_css() -> FileResponse:
        return FileResponse(STATIC_DIR / "theme.css", media_type="text/css")

    # PWA installability assets - static, public, no auth needed. Named routes rather than
    # a StaticFiles mount, matching every other static file this app already serves.
    _pwa_files = {
        "/manifest.json": ("manifest.json", "application/manifest+json"),
        "/sw.js": ("sw.js", "text/javascript"),
        "/favicon.png": ("favicon.png", "image/png"),
        "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
        "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
        "/icons/icon-512-maskable.png": ("icons/icon-512-maskable.png", "image/png"),
        "/icons/apple-touch-icon.png": ("icons/apple-touch-icon.png", "image/png"),
    }
    for _route, (_rel_path, _media_type) in _pwa_files.items():

        def _serve_pwa_file(
            rel_path: str = _rel_path, media_type: str = _media_type
        ) -> FileResponse:
            return FileResponse(STATIC_DIR / rel_path, media_type=media_type)

        app.get(_route, include_in_schema=False)(_serve_pwa_file)

    def _admin_rows(history: Any, fits: dict[str, Any]) -> list[dict[str, Any]]:
        """Every visitor as a flat dict, signed-up landlords first, then anonymous ones.

        Shared by the JSON listing and the CSV export so the two can never drift apart.
        """

        def fit_fields(fit: Any) -> dict[str, Any]:
            return {
                "signals": fit.signals if fit else [],
                "good_fit": fit.good_fit if fit else False,
                "manual_override": fit.manual_override if fit else None,
                "notes": fit.notes if fit else "",
                "hidden": fit.hidden if fit else False,
                "ai_good_fit": fit.ai_good_fit if fit else None,
                "ai_reasoning": fit.ai_reasoning if fit else "",
                "ai_analyzed_at": fit.ai_analyzed_at if fit else None,
            }

        accounts = get_accounts()
        rows: list[dict[str, Any]] = []
        for account, created_at in accounts.all():
            threads = history.list(account.owner_id, limit=5)
            rows.append(
                {
                    "id": account.owner_id,
                    "anonymous": False,
                    "name": account.name,
                    "email": account.email,
                    "phone": account.phone,
                    "neighborhood": account.neighborhood,
                    "created_at": created_at,
                    # Their own words, newest first - the cheapest honest summary there is.
                    "summary": [t.title for t in threads],
                    **fit_fields(fits.get(account.owner_id)),
                }
            )
        for owner_id, last_seen in history.anonymous_owners():
            threads = history.list(owner_id, limit=5)
            rows.append(
                {
                    "id": owner_id,
                    "anonymous": True,
                    "name": "",
                    "email": "",
                    "phone": "",
                    "neighborhood": "",
                    "created_at": last_seen,
                    "summary": [t.title for t in threads],
                    **fit_fields(fits.get(owner_id)),
                }
            )
        return rows

    @app.get("/api/admin/users")
    def admin_users(
        include_hidden: bool = False, _: None = Depends(require_admin_access)
    ) -> dict[str, Any]:
        """Every visitor with a summary of what they asked and the AI's PM-fit call.

        Hidden visitors are left out unless ``include_hidden=1`` - hiding is soft (a flag,
        not a delete), so the "Show hidden" toggle in the admin page is how one comes back.
        """
        rows = _admin_rows(get_history(), get_admin_store().all())
        if not include_hidden:
            rows = [r for r in rows if not r["hidden"]]
        return {"users": rows}

    @app.get("/api/admin/users/export.csv")
    def admin_users_export(
        include_hidden: bool = False, _: None = Depends(require_admin_access)
    ) -> Response:
        import csv
        import io

        rows = _admin_rows(get_history(), get_admin_store().all())
        if not include_hidden:
            rows = [r for r in rows if not r["hidden"]]
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "name",
                "email",
                "phone",
                "neighborhood",
                "signed_up_or_last_seen_utc",
                "signals",
                "good_fit",
                "notes",
                "ai_reasoning",
            ]
        )
        for row in rows:
            when = datetime.fromtimestamp(row["created_at"], tz=UTC).isoformat()
            writer.writerow(
                [
                    _csv_safe(row["name"]),
                    _csv_safe(row["email"]),
                    _csv_safe(row["phone"]),
                    _csv_safe(row["neighborhood"]),
                    when,
                    _csv_safe(";".join(row["signals"])),
                    row["good_fit"],
                    _csv_safe(row["notes"]),
                    _csv_safe(row["ai_reasoning"]),
                ]
            )
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=jay-visitors.csv"},
        )

    @app.get("/api/admin/users/{owner_id}")
    def admin_user_detail(owner_id: str, _: None = Depends(require_admin_access)) -> dict[str, Any]:
        """One visitor's full picture: contact details, the AI's call, and every full
        conversation - the one place the transcript is shown, not just its title, and only
        to staff behind the admin code.
        """
        account = None
        if owner_id.startswith("account:"):
            account = get_accounts().get(owner_id[len("account:") :])
        fit = get_admin_store().get(owner_id)
        threads = get_history().list_with_messages(owner_id)
        if account is None and fit is None and not threads:
            raise HTTPException(status_code=404, detail="No such visitor.")
        return {
            "id": owner_id,
            "anonymous": account is None,
            "name": account.name if account else "",
            "email": account.email if account else "",
            "phone": account.phone if account else "",
            "neighborhood": account.neighborhood if account else "",
            "last_ip": fit.last_ip if fit else "",
            "signals": fit.signals if fit else [],
            "good_fit": fit.good_fit if fit else False,
            "manual_override": fit.manual_override if fit else None,
            "notes": fit.notes if fit else "",
            "hidden": fit.hidden if fit else False,
            "ai_good_fit": fit.ai_good_fit if fit else None,
            "ai_reasoning": fit.ai_reasoning if fit else "",
            "ai_analyzed_at": fit.ai_analyzed_at if fit else None,
            "threads": [t.to_dict(with_messages=True) for t in threads],
        }

    @app.post("/api/admin/users/{owner_id}/override")
    def admin_override(
        owner_id: str,
        payload: PmFitOverrideRequest,
        _: None = Depends(require_admin_access),
    ) -> dict[str, Any]:
        get_admin_store().set_override(owner_id, payload.good_fit)
        return {"saved": True}

    @app.post("/api/admin/users/{owner_id}/notes")
    def admin_notes(
        owner_id: str,
        payload: PmFitNotesRequest,
        _: None = Depends(require_admin_access),
    ) -> dict[str, Any]:
        get_admin_store().set_notes(owner_id, payload.notes)
        return {"saved": True}

    @app.post("/api/admin/users/{owner_id}/hidden")
    def admin_hidden(
        owner_id: str,
        payload: PmFitHiddenRequest,
        _: None = Depends(require_admin_access),
    ) -> dict[str, Any]:
        get_admin_store().set_hidden(owner_id, payload.hidden)
        return {"saved": True}

    @app.post("/api/admin/users/bulk-hidden")
    def admin_bulk_hidden(
        payload: BulkHiddenRequest, _: None = Depends(require_admin_access)
    ) -> dict[str, Any]:
        store = get_admin_store()
        for owner_id in payload.owner_ids:
            store.set_hidden(owner_id, payload.hidden)
        return {"saved": True, "count": len(payload.owner_ids)}

    @app.post("/api/admin/users/{owner_id}/analyze")
    def admin_analyze(owner_id: str, _: None = Depends(require_admin_access)) -> dict[str, Any]:
        """One cheap, staff-triggered model call over the full transcript - a real read,
        not five regex patterns. Never runs on its own; only a click spends anything.
        """
        from markai.web.admin_store import ClassificationError, classify_fit

        threads = get_history().list_with_messages(owner_id)
        try:
            good_fit, reasoning = classify_fit(settings, threads)
        except ClassificationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        get_admin_store().set_ai_analysis(owner_id, good_fit, reasoning)
        return {"ai_good_fit": good_fit, "ai_reasoning": reasoning}

    @app.get("/api/log")
    def read_log(
        property_id: str = "",
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        """Their money log for the panel: what happened, what is still open, what it adds
        up to. A maintenance/visit/note row with no dollar amount is tracking, not money -
        it belongs to the Maintenance Tracker, not here, so it is left out rather than
        cluttering a view that is supposed to be about what moved."""
        log = get_ledger()
        labels = {p.id: p.label for p in get_portfolio().list(owner)}

        def rendered(entry: Any) -> dict[str, Any]:
            data = entry.to_dict()
            data["property_label"] = labels.get(entry.property_id, "")
            return data

        def is_money_entry(entry: Any) -> bool:
            return entry.kind in ("expense", "bill", "income") or bool(entry.amount)

        # Fetched wider than the display cap, since filtering after a plain limit could
        # otherwise let a run of costless maintenance notes push real money entries out of
        # the page entirely.
        entries = [
            e for e in log.list(owner, property_id=property_id, limit=240) if is_money_entry(e)
        ]
        open_items = [e for e in log.open_items(owner, limit=80) if is_money_entry(e)]

        return {
            "entries": [rendered(e) for e in entries[:60]],
            "open": [rendered(e) for e in open_items[:20]],
            "totals": log.totals(owner, property_id=property_id),
            "count": log.count(owner),
        }

    @app.get("/api/log/monthly")
    def read_log_monthly(
        property_id: str = "",
        months: int = 12,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        """Money in vs out, month by month - the trend chart on the Income & expenses
        page, not something the chat or the Maintenance Tracker needs."""
        return {
            "months": get_ledger().monthly_totals(owner, property_id=property_id, months=months)
        }

    @app.get("/api/log/by-property")
    def read_log_by_property(
        owner: str = Depends(owner_of), _: None = Depends(require_access)
    ) -> dict[str, Any]:
        """Net income by building, for the portfolio comparison chart - the same totals()
        the rest of the accounting view trusts, just run once per building. Anything not
        assigned to a building is its own row rather than silently missing from a chart
        that is supposed to account for every dollar."""
        log = get_ledger()
        properties = get_portfolio().list(owner)
        whole = log.totals(owner)
        rows = []
        assigned_in = assigned_out = 0.0
        for p in properties:
            t = log.totals(owner, property_id=p.id)
            rows.append(
                {
                    "property_id": p.id,
                    "label": p.label,
                    "in": t.get("in", 0.0),
                    "out": t.get("out", 0.0),
                    "net": t.get("net", 0.0),
                }
            )
            assigned_in += t.get("in", 0.0)
            assigned_out += t.get("out", 0.0)
        unassigned_in = round(whole.get("in", 0.0) - assigned_in, 2)
        unassigned_out = round(whole.get("out", 0.0) - assigned_out, 2)
        if unassigned_in or unassigned_out:
            rows.append(
                {
                    "property_id": "",
                    "label": "Unassigned",
                    "in": unassigned_in,
                    "out": unassigned_out,
                    "net": round(unassigned_in - unassigned_out, 2),
                }
            )
        return {"properties": rows}

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
        """Marking something done reaches the same completion logic no matter which panel
        it was clicked in - a maintenance issue with a cost still gets its companion
        expense logged here, not only through the Maintenance Tracker's own button, since a
        priced issue now shows in both places."""
        entry = get_ledger().get(owner, entry_id)
        if entry is not None and entry.kind == "maintenance":
            log = owner_log(owner)
            completed = log.complete_maintenance(entry_id) if log else None
            return {"closed": completed is not None}
        return {"closed": get_ledger().close(owner, entry_id)}

    @app.post("/api/log/{entry_id}/edit")
    def edit_log(
        entry_id: str,
        payload: LogEditRequest,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        from markai.web.ledger import LogError

        log = owner_log(owner)
        if log is None:
            raise HTTPException(status_code=404, detail="No entry of yours has that id.")
        try:
            updated = log.update(
                entry_id, payload.what, payload.amount, payload.vendor, payload.date
            )
        except LogError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="No entry of yours has that id.")
        return {"entry": updated.to_dict()}

    @app.delete("/api/log/{entry_id}")
    def forget_log(
        entry_id: str,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        """Deleting is a person's decision, which is why it is here and not a tool."""
        return {"deleted": get_ledger().delete(owner, entry_id)}

    @app.post("/api/log/{entry_id}/vendor-cost")
    def set_vendor_cost(
        entry_id: str,
        payload: VendorCostRequest,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        """Who is doing the work and what it costs - rarely both known the moment an
        issue is first reported, so this fills them in any time before it is done."""
        from markai.web.ledger import LogError

        try:
            saved = get_ledger().update_vendor_cost(owner, entry_id, payload.vendor, payload.amount)
        except LogError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"saved": saved}

    @app.post("/api/log/{entry_id}/photo")
    async def upload_photo(
        entry_id: str,
        file: UploadFile,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        from markai.web.photos import PhotoError, save_photo

        log = owner_log(owner)
        entry = log.get(entry_id) if log else None
        if entry is None:
            raise HTTPException(status_code=404, detail="No entry of yours has that id.")
        raw = await file.read()
        try:
            photo_path = save_photo(settings.data_dir, owner, entry_id, raw)
        except PhotoError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        get_ledger().set_photo(owner, entry_id, photo_path)
        return {"saved": True}

    @app.get("/api/log/{entry_id}/photo")
    def read_photo(
        entry_id: str,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> FileResponse:
        from markai.web.photos import photo_file

        log = owner_log(owner)
        entry = log.get(entry_id) if log else None
        if entry is None or not entry.photo_path:
            raise HTTPException(status_code=404, detail="No photo on that entry.")
        path = photo_file(settings.data_dir, entry.photo_path)
        if path is None:
            raise HTTPException(status_code=404, detail="That photo is gone.")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/maintenance")
    def read_maintenance(
        owner: str = Depends(owner_of), _: None = Depends(require_access)
    ) -> dict[str, Any]:
        """Open maintenance issues worst-first, and recently completed ones alongside -
        the same log_entries rows the generic log already holds, just the one view "what
        needs attention, and what did we already deal with here" actually needs."""
        log = owner_log(owner)
        open_items = log.open_maintenance() if log else []
        history = log.maintenance_history() if log else []
        return {
            "open": [item.to_dict() for item in open_items],
            "history": [item.to_dict() for item in history],
        }

    @app.post("/api/maintenance/{entry_id}/complete")
    def complete_maintenance(
        entry_id: str,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        """Mark a maintenance issue done - and if it has a cost recorded, this also logs
        a matching expense, so the money actually counts instead of sitting invisibly on a
        row `totals()` never counts as spent."""
        log = owner_log(owner)
        result = log.complete_maintenance(entry_id) if log else None
        if result is None:
            raise HTTPException(
                status_code=404, detail="No open maintenance entry of yours has that id."
            )
        return {"entry": result.to_dict()}

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

        # Units, not properties: a single 6-flat is past the self-management burnout point
        # on its own, and three single-families add up to the same load a 2-property count
        # would miss. 4 units is the documented tipping point landlords report burning out
        # past, not a guess - a two-flat-to-three-flat landlord isn't there yet.
        total_before = sum(p.units or 1 for p in get_portfolio().list(owner))
        try:
            saved = get_portfolio().add(owner, payload.model_dump())
        except PropertyError as exc:
            # The message names the field and the limit, so it is useful to show.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        total_after = total_before + (saved.units or 1)
        if signed_in is not None and total_before < 4 <= total_after:
            # Fires once, the moment the count crosses the line, however it gets there -
            # one more small building or one big one added at once. A fifth unit later is
            # not new information.
            _queue_candidate_lead(
                signed_in,
                "portfolio_growth",
                f"Now manages {total_after} units - past the self-management burnout point",
            )
        return {"property": saved.to_dict()}

    @app.delete("/api/properties/{property_id}")
    def forget_property(
        property_id: str,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        return {"deleted": get_portfolio().delete(owner, property_id)}

    @app.post("/api/calc/deal")
    def calc_deal(payload: DealRequest, _: None = Depends(require_access)) -> dict[str, Any]:
        """Mortgage payment, NOI, cap rate, cash-on-cash, DSCR - live, in the browser.

        Pure math already tested in `calculators.py`; no AI call, no storage, so it costs
        nothing and answers instantly. Same engine `mark calc deal` and Jay's own tool-call
        use, so all three always agree.
        """
        from markai.advisor.calculators import analyze_deal

        return analyze_deal(**payload.model_dump())

    @app.post("/api/notice-wizard")
    def notice_wizard(
        payload: NoticeWizardRequest, _: None = Depends(require_access)
    ) -> dict[str, Any]:
        """Which notice to serve, from the same reviewed, cited facts chat already uses -
        a jurisdiction-filtered lookup, not a model call, so it is instant and free.
        """
        from markai.advisor.notice_wizard import JURISDICTIONS, REASONS, find_notice_rules

        if payload.jurisdiction not in JURISDICTIONS:
            raise HTTPException(status_code=400, detail="Unknown jurisdiction.")
        if payload.reason not in REASONS:
            raise HTTPException(status_code=400, detail="Unknown reason.")
        results = find_notice_rules(
            get_facts(), payload.jurisdiction, payload.reason, payload.tenure_years
        )
        return {
            "results": [
                {
                    "jurisdiction": o.jurisdiction,
                    "topic": o.topic,
                    "rule": o.rule,
                    "citation": o.citation,
                    "url": o.url,
                }
                for o in results
            ]
        }

    @app.post("/api/feedback")
    def feedback(
        payload: FeedbackRequest,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
    ) -> dict[str, Any]:
        """A thumb on the last answer. A down is a content gap the owner can act on."""
        saved = get_history().rate(owner, payload.session_id, payload.rating, payload.note)
        return {"saved": saved}

    def _escalation_contact() -> tuple[str | None, str | None]:
        """Who to hand a case to when it has moved past what a tool can tell you, and the
        one place both `/api/handoff` and `/api/business` read it from - so deleting the
        url from the manifest turns the offer off everywhere at once, not just one of them.
        """
        from markai.sources.manifest import load_manifest

        try:
            business = load_manifest(settings.sources_file).business
            return business.escalation_name, business.escalation_url
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("no escalation contact available: %s", exc)
            return None, None

    @app.get("/api/business")
    def business_info(_: None = Depends(require_access)) -> dict[str, Any]:
        """Static, deployment-wide config a tool's own footer needs - not session or
        user-specific, so the page can fetch this once and hold onto it."""
        name, url = _escalation_contact()
        return {"escalation_name": name, "escalation_url": url}

    @app.post("/api/handoff")
    def handoff(
        payload: ResetRequest,
        owner: str = Depends(owner_of),
        _: None = Depends(require_access),
        __: None = Depends(rate_limit_public_forms),
    ) -> dict[str, Any]:
        """Case notes for the property manager, built from what is already stored."""
        from markai.web.handoff import build_handoff

        name, url = _escalation_contact()

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
        client_ip: str = Depends(client_ip_of),
        _: None = Depends(require_access),
    ) -> EventSourceResponse:
        from markai.advisor.attachments import AttachmentError, decode_all

        if owner:
            # Every question, signal or not, so a visitor who never trips a pain-point flag
            # is still reachable for the admin panel's "location by IP" lookup.
            get_admin_store().note_visit(owner, client_ip)

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
                        "Add your info so nothing gets lost. Jay will remember this "
                        "conversation and give you sharper answers for your rental "
                        "instead of general advice."
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
        if owner:
            # Signals are recorded for anyone with an owner id, signed in or not, so staff
            # can see an anonymous visitor's activity too. The alert email and the CRM lead
            # need a real contact, so those two still only fire once someone has signed in.
            def on_flags(flags: list[str]) -> None:
                from markai.web.admin_store import send_pm_fit_alert_soon

                admin_store = get_admin_store()
                for flag, reason in PM_FIT_FLAGS.items():
                    if flag not in flags:
                        continue
                    newly_seen = admin_store.record_signal(owner, flag)
                    if newly_seen and signed_in is not None:
                        send_pm_fit_alert_soon(get_pm_fit_sender(), signed_in, flag, reason)
                if FLAG_PM_INTEREST in flags and signed_in is not None:
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
        except MissingApiKeyError:
            logger.exception("advisor unavailable: no API key configured")
            yield {
                "event": "error",
                "data": json.dumps({"message": "Jay is not set up yet. Try again shortly."}),
            }
            return
        except FileNotFoundError:
            # The real message is a raw OS path (e.g. "[Errno 2] ... '/mnt/data/...'") - useful
            # to the operator, not to whichever landlord's browser happens to receive it.
            logger.exception("advisor unavailable: a required file was not found")
            yield {
                "event": "error",
                "data": json.dumps({"message": "Jay is not set up yet. Try again shortly."}),
            }
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
                yield {
                    "event": "tool",
                    "data": json.dumps({"name": event.text, "input": event.tool_input or {}}),
                }
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
    except Exception:  # a crash here must not hang the browser, or hand it the exception text
        logger.exception("chat stream failed")
        yield {
            "event": "error",
            "data": json.dumps({"message": "Something went wrong. Please try again."}),
        }
    finally:
        lock.release()


app = create_app()
