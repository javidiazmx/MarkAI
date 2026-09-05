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
        if self.youtube_cookies_file:
            return "cookies"
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
