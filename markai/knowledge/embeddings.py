"""Optional semantic embeddings via Voyage AI.

Embeddings are strictly optional: without ``VOYAGE_API_KEY`` the knowledge base runs on BM25
alone. ``build_embedder`` returns ``None`` in that case and the rest of the system treats a
missing embedder as "lexical only". Nothing here talks to the network at import time.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from markai.config import Settings

logger = logging.getLogger(__name__)

_LEXICAL_ONLY_LOGGED = False


class Embedder(Protocol):
    """Anything that can embed documents and queries into the same vector space."""

    name: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class VoyageEmbedder:
    """Hosted Voyage AI embeddings (``voyage-3.5`` by default), batched.

    ``client`` may be injected for tests; otherwise ``voyageai.Client(api_key=...)`` is created
    lazily on first use so importing this module never requires the key.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "voyage-3.5",
        batch_size: int = 128,
        client: Any | None = None,
    ) -> None:
        self.name = model
        self.model = model
        self.batch_size = max(1, int(batch_size))
        self._api_key = api_key
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            import voyageai  # local import: keeps module import cheap and network-free

            self._client = voyageai.Client(api_key=self._api_key)
        return self._client

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        if not texts:
            return []
        client = self._get_client()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            result = client.embed(batch, model=self.model, input_type=input_type)
            embeddings = getattr(result, "embeddings", result)
            for vec in embeddings:
                vectors.append([float(x) for x in vec])
            logger.debug(
                "embedded batch of %d texts (%s) with %s", len(batch), input_type, self.model
            )
        if len(vectors) != len(texts):
            raise RuntimeError(f"Voyage returned {len(vectors)} embeddings for {len(texts)} texts")
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(list(texts), "document")

    def embed_query(self, text: str) -> list[float]:
        vectors = self._embed([text], "query")
        return vectors[0]


def build_embedder(settings: Settings) -> Embedder | None:
    """Return a ``VoyageEmbedder`` when a Voyage key is configured, else ``None``."""
    global _LEXICAL_ONLY_LOGGED
    key = settings.voyage_key()
    if not key:
        if not _LEXICAL_ONLY_LOGGED:
            logger.info("VOYAGE_API_KEY not set; retrieval is lexical-only (BM25).")
            _LEXICAL_ONLY_LOGGED = True
        return None
    return VoyageEmbedder(
        api_key=key,
        model=settings.embedding_model,
        batch_size=settings.embedding_batch_size,
    )
