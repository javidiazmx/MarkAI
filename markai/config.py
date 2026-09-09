"""Runtime settings for MarkAI, loaded from environment variables and ``.env``.

Only ``ANTHROPIC_API_KEY`` is required to chat. Everything else has a sensible default.
Variables prefixed ``MARKAI_`` map to the fields below; the API keys and the web access code
are read from their conventional, unprefixed names (``ANTHROPIC_API_KEY``, ``VOYAGE_API_KEY``,
``MARKAI_WEB_ACCESS_CODE``).

pydantic-settings reads ``.env`` into this object only (it does not export to ``os.environ``),
so the advisor passes ``settings.anthropic_key()`` to the Anthropic client explicitly.

Never print, log, or ``model_dump()`` a Settings object. Secrets are ``SecretStr`` but the
rest is not meant for display either; ``mark status`` and ``/api/status`` use explicit
whitelists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]

Effort = Literal["low", "medium", "high", "xhigh", "max"]


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class Settings(BaseSettings):
    """All tunables in one place. Construct with ``Settings()`` to read ``.env`` + environment.

    Tests should construct ``Settings(_env_file=None, data_dir=tmp_path, ...)`` so the
    developer's real ``.env`` is never read.
    """

    model_config = SettingsConfigDict(
        env_prefix="MARKAI_",
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- Claude -------------------------------------------------------------------------
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="ANTHROPIC_API_KEY",
        description="Anthropic API key. Required to chat. Passed explicitly to the SDK client.",
    )
    model: str = Field(default="claude-opus-5", description="Anthropic model id.")
    effort: Effort = Field(
        default="medium",
        description=(
            "Reasoning effort passed as output_config.effort. 'medium' keeps chat answers "
            "quick; raise to 'high' for harder analysis."
        ),
    )
    max_tokens: int = Field(
        default=16000,
        ge=1024,
        description="Ceiling on thinking + answer tokens per model call (raised to 64000 "
        "automatically when effort is xhigh/max).",
    )
    system_prompt_path: Path = Field(default=Path("prompts/mark_system_prompt.md"))

    # --- Embeddings (optional) -------------------------------------------------------------
    voyage_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="VOYAGE_API_KEY",
        description="Voyage AI key. When unset, retrieval is lexical-only (BM25).",
    )
    embedding_model: str = Field(default="voyage-3.5", description="Voyage embedding model.")
    embedding_batch_size: int = Field(default=128, ge=1, le=1000)
    # Voyage caps an account with no payment method at 3 requests and 10,000 tokens a minute.
    # 0 means "no limit I know of"; the backfill switches these on by itself the first time
    # Voyage says otherwise, so a free account needs no configuration.
    embedding_requests_per_minute: int = Field(default=0, ge=0)
    embedding_tokens_per_minute: int = Field(default=0, ge=0)

    # --- Paths & knowledge base ---------------------------------------------------------------
    project_root: Path = Field(default=PROJECT_ROOT)
    data_dir: Path = Field(default=Path("data"))
    sources_file: Path = Field(default=Path("sources/sources.yaml"))
    top_k: int = Field(default=8, ge=1, le=50, description="Chunks handed to Mark per question.")
    min_relevance: float = Field(
        default=2.0,
        description="Raw BM25 score below which the best hit is treated as no coverage.",
    )
    weak_relevance: float = Field(
        default=5.0,
        description="Raw BM25 score below which coverage is reported as 'weak'.",
    )
    min_cosine: float = Field(default=0.35, description="Cosine floor when embeddings exist.")
    chunk_target_words: int = Field(default=350, ge=50)
    chunk_overlap_words: int = Field(default=60, ge=0)
    av_window_seconds: float = Field(default=120.0, gt=0)

    cache_ttl: str = Field(
        default="1h",
        pattern="^(5m|1h)$",
        description=(
            "How long the frozen system prefix stays cached. A write costs 1.25x at 5m and "
            "2x at 1h; a read costs 0.1x. So 5m pays off from the second question within "
            "five minutes, 1h from the third within an hour - and either one costs more "
            "than no cache at all if the questions are further apart than that."
        ),
    )

    covered_cosine: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Semantic score above which a question counts as covered when the keyword arm "
            "found nothing. A question in Spanish shares no words with English sources, so "
            "it has no keyword signal by construction and this bar decides it alone."
        ),
    )

    fast_mode: bool = Field(
        default=False,
        description=(
            "Run Opus 5 in fast mode: up to 2.5x the output speed at double the token "
            "price ($10/$50 per MTok instead of $5/$25). Claude API only, and it has its "
            "own rate limit, so a 429 falls back to standard speed for that question."
        ),
    )

    account_required: bool = Field(
        default=True,
        description=(
            "Ask a landlord for a free account after their free questions are spent. Off: "
            "the page never asks. The terminal never asks either way."
        ),
    )
    lead_email_to: str = Field(
        default="",
        description=(
            "The CRM's inbound address for a new lead, such as LeadSimple's "
            "new-deal...@newlead.leadsimple.com. Needs the SMTP settings below."
        ),
    )
    smtp_host: str = Field(
        default="",
        description="Mail server for sending the lead. Google Workspace: smtp.gmail.com.",
    )
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = Field(default="", description="Usually the sending address.")
    smtp_password: SecretStr | None = Field(
        default=None,
        description=(
            "The mailbox password. On Google Workspace or Gmail this is an app password, "
            "never the account password."
        ),
    )
    smtp_from: str = Field(
        default="", description="What the lead is sent from. Defaults to the username."
    )
    smtp_starttls: bool = Field(
        default=True, description="STARTTLS on port 587. Port 465 uses SSL instead."
    )

    crm_webhook_url: str = Field(
        default="",
        description=(
            "Where a new lead is POSTed as JSON: LeadSimple's inbound URL, a Zapier or "
            "Make catch hook, or your own endpoint. Empty: leads queue and wait, so "
            "nothing is lost while this is being set up."
        ),
    )
    crm_webhook_token: SecretStr | None = Field(
        default=None,
        description="Sent as `Authorization: Bearer ...` when the CRM needs a key.",
    )
    crm_webhook_header: str = Field(
        default="",
        description=(
            "An extra header for a CRM that wants something other than a bearer token, "
            "written as `Name: value`."
        ),
    )
    session_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="How long a sign-in cookie lasts before a landlord signs in again.",
    )
    cookie_secure: bool = Field(
        default=False,
        description=(
            "Send the sign-in cookie only over https. Off by default because the page is "
            "usually run on http://127.0.0.1; turn it on the moment it is behind a domain."
        ),
    )
    free_questions_before_signup: int = Field(
        default=2,
        ge=0,
        le=100,
        description=(
            "Questions a browser gets answered before the signup form. Two, because a "
            "landlord who has had two real answers knows what they are signing up for."
        ),
    )

    legal_disclaimer_in_answers: bool = Field(
        default=False,
        description=(
            "Append the legal disclaimer to a legal answer. Off: the browser page shows "
            "the full notice once on a first visit, which is where a standing disclaimer "
            "belongs. `mark ask` has no such screen, so it always appends it."
        ),
    )

    show_citations: bool = Field(
        default=False,
        description=(
            "Put [S1] markers in the answer and list the sources under it. Off by default: "
            "the owner wants advice in Mark's voice, not a footnoted report. Grounding is "
            "unchanged either way - only whether the working is shown."
        ),
    )

    # --- Ingestion -------------------------------------------------------------------------
    crawl_delay_seconds: float = Field(default=0.5, ge=0)
    # --- Getting past a YouTube IP block ------------------------------------------------
    # Politeness stops you being blocked; it does nothing once you already are. These give
    # the caption requests a different address or a signed-in identity. All optional.
    youtube_proxy_url: SecretStr | None = Field(
        default=None,
        description="An http(s) proxy for caption requests. May contain credentials.",
    )
    webshare_username: str | None = Field(default=None)
    webshare_password: SecretStr | None = Field(default=None)
    youtube_cookies_file: Path | None = Field(
        default=None,
        description="Netscape cookies.txt exported from a signed-in browser.",
    )
    youtube_cookies_from_browser: str | None = Field(
        default=None,
        description=(
            "Read cookies straight out of an installed browser: firefox, edge, chrome, "
            "brave, opera, vivaldi, safari. Nothing to export and nothing to install."
        ),
    )

    youtube_delay_seconds: float = Field(
        default=2.0,
        ge=0,
        description=(
            "Pause between caption requests. Without one YouTube blocks the machine "
            "after a few dozen videos, which turns a large channel into many sessions."
        ),
    )
    http_timeout_seconds: float = Field(default=30.0, gt=0)
    max_page_bytes: int = Field(
        default=25_000_000,
        ge=100_000,
        description="How much of a page to read. Bigger pages are truncated, not skipped.",
    )
    transcribe_model: str = Field(
        default="small",
        description="faster-whisper model size (tiny/base/small/medium/large-v3).",
    )
    youtube_languages: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["en", "en-US"],
        description="Caption languages to try, in order (env form: en,en-US).",
    )

    # --- Web UI ---------------------------------------------------------------------------
    web_host: str = Field(default="127.0.0.1")
    web_port: int = Field(default=8000)
    web_access_code: SecretStr | None = Field(
        default=None,
        description="If set, every /api/* request must send header X-Access-Code with this value.",
    )
    max_sessions: int = Field(default=200, ge=1)
    max_question_chars: int = Field(default=4000, ge=100)
    daily_question_limit: int = Field(default=500, ge=1, description="Global per-UTC-day cap.")
    per_session_question_limit: int = Field(default=40, ge=1)

    # --- Validators -----------------------------------------------------------------------
    @field_validator("youtube_languages", mode="before")
    @classmethod
    def _languages_from_csv(cls, value: object) -> object:
        return _split_csv(value)

    @model_validator(mode="after")
    def _resolve_relative_paths(self) -> Settings:
        for name in ("data_dir", "sources_file", "system_prompt_path"):
            value: Path = getattr(self, name)
            if not value.is_absolute():
                object.__setattr__(self, name, (self.project_root / value).resolve())
        return self

    # --- Secret accessors (never log the return values) ------------------------------------
    @staticmethod
    def _reveal(value: SecretStr | str | None) -> str | None:
        """Unwrap a secret. Tolerates a plain string, which ``model_copy`` can introduce."""
        if value is None:
            return None
        if isinstance(value, SecretStr):
            return value.get_secret_value() or None
        return str(value) or None

    def anthropic_key(self) -> str | None:
        return self._reveal(self.anthropic_api_key)

    def smtp_secret(self) -> str | None:
        return self._reveal(self.smtp_password)

    def crm_token(self) -> str | None:
        return self._reveal(self.crm_webhook_token)

    def crm_headers(self) -> dict[str, str]:
        """Whatever the CRM needs to accept the POST. Never logged, never in `mark status`."""
        headers: dict[str, str] = {}
        token = self.crm_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self.crm_webhook_header and ":" in self.crm_webhook_header:
            name, _, value = self.crm_webhook_header.partition(":")
            if name.strip():
                headers[name.strip()] = value.strip()
        return headers

    def voyage_key(self) -> str | None:
        return self._reveal(self.voyage_api_key)

    def access_code(self) -> str | None:
        return self._reveal(self.web_access_code)

    def youtube_proxy(self) -> str | None:
        return self._reveal(self.youtube_proxy_url)

    def webshare_secret(self) -> str | None:
        return self._reveal(self.webshare_password)

    def youtube_unblock_method(self) -> str:
        """Which way out of an IP block is configured. Names the method, never the secret."""
        if self.webshare_username and self.webshare_password:
            return "webshare proxy"
        if self.youtube_proxy_url:
            return "proxy"
        if self.youtube_cookies_from_browser:
            return f"{self.youtube_cookies_from_browser} cookies"
        if self.youtube_cookies_file:
            return "cookies file"
        return "none"

    def request_max_tokens(self) -> int:
        """max_tokens actually sent: xhigh/max effort needs headroom for thinking."""
        if self.effort in ("xhigh", "max"):
            return max(self.max_tokens, 64000)
        return self.max_tokens

    # --- Derived paths --------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "markai.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def web_cache_dir(self) -> Path:
        return self.raw_dir / "web"

    @property
    def youtube_cache_dir(self) -> Path:
        return self.raw_dir / "youtube"

    @property
    def podcast_audio_dir(self) -> Path:
        return self.raw_dir / "podcast" / "audio"

    @property
    def podcast_transcripts_dir(self) -> Path:
        return self.raw_dir / "podcast" / "transcripts"

    def ensure_dirs(self) -> None:
        """Create every data directory Mark writes to."""
        for path in (
            self.data_dir,
            self.raw_dir,
            self.web_cache_dir,
            self.youtube_cache_dir,
            self.podcast_audio_dir,
            self.podcast_transcripts_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    """Convenience constructor used by the CLI and web app."""
    return Settings()
