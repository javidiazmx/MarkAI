"""Hybrid retrieval: BM25 over every chunk, optional cosine search over Voyage embeddings,
fused with Reciprocal Rank Fusion.

Coverage ("covered" / "weak" / "none") is decided from RAW scores (top BM25 score and top
cosine), never from fused scores, so the thresholds in ``Settings`` keep their meaning. The
whole corpus lives in memory after ``refresh()``; a query never touches the database.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from rank_bm25 import BM25Okapi

from markai.config import Settings
from markai.knowledge.embeddings import Embedder
from markai.knowledge.store import KnowledgeStore
from markai.models import Chunk, Document, RetrievedChunk, SourceKind

logger = logging.getLogger(__name__)

RRF_K = 60

# Share of query terms a chunk must contain for the small-corpus fallback to count.
OVERLAP_COVERAGE_RATIO = 0.6
MAX_VECTOR_CANDIDATES = 200

_TOKEN = re.compile(r"[a-z0-9']+")

STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or but if of to in on at for with by from as is are was were be been being
    it its this that these those i you he she we they them my your our their me him her us
    do does did have has had not no so what which who whom how when where why can could will
    would should may might about into than then there here just also very up out over some
    any all am s t don't doesn't isn't it's i'm you're we're they're
    """.split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase, ``[a-z0-9']+`` tokens, minus a small stopword list. Numbers are kept."""
    tokens: list[str] = []
    for raw in _TOKEN.findall(text.lower()):
        tok = raw.strip("'")
        if tok.endswith("'s"):
            tok = tok[:-2]
        if not tok or tok in STOPWORDS:
            continue
        tokens.append(tok)
    return tokens


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """Sum ``1 / (k + rank)`` over every ranking an id appears in (ranks are 1-based)."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            fused[item] = fused.get(item, 0.0) + 1.0 / (k + rank)
    return fused


@dataclass
class RetrievalResult:
    """What the advisor gets back for one question."""

    chunks: list[RetrievedChunk]
    coverage: Literal["covered", "weak", "none"]
    lexical_used: bool
    vector_used: bool
    top_lexical_score: float = 0.0
    top_cosine: float = 0.0
    query_tokens: list[str] = field(default_factory=list)


class Retriever:
    """In-memory hybrid retriever over a ``KnowledgeStore``. Call ``refresh()`` after ingest."""

    def __init__(
        self, store: KnowledgeStore, embedder: Embedder | None, settings: Settings
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.settings = settings
        self._chunks: list[Chunk] = []
        self._chunk_index: dict[str, int] = {}
        self._docs: dict[str, Document] = {}
        self._bm25: BM25Okapi | None = None
        self._matrix: np.ndarray | None = None
        self._matrix_ids: list[str] = []
        self.refresh()

    # --- loading ---------------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload chunks and documents from the store and rebuild the indexes."""
        chunks = self.store.all_chunks()
        docs = {doc.id: doc for doc in self.store.list_documents()}
        # Drop chunks whose parent document is missing (should not happen; be defensive).
        chunks = [c for c in chunks if c.doc_id in docs]
        self._chunks = chunks
        self._chunk_index = {c.id: i for i, c in enumerate(chunks)}
        self._docs = docs

        self._chunk_tokens: list[list[str]] = []
        self._bm25 = None
        if chunks:
            corpus = [tokenize(c.text) for c in chunks]
            self._chunk_tokens = corpus
            if any(corpus):  # never build BM25 on an empty (or all-stopword) corpus
                self._bm25 = BM25Okapi(corpus)

        self._matrix = None
        self._matrix_ids = []
        if self.embedder is not None and chunks:
            loaded = self.store.embeddings_matrix(self.embedder.name)
            if loaded is not None:
                ids, matrix = loaded
                keep = [i for i, cid in enumerate(ids) if cid in self._chunk_index]
                if keep:
                    matrix = matrix[keep].astype(np.float32, copy=False)
                    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                    norms[norms == 0] = 1.0
                    self._matrix = matrix / norms
                    self._matrix_ids = [ids[i] for i in keep]
        logger.info(
            "retriever loaded %d chunks from %d documents (bm25=%s, vectors=%d)",
            len(chunks),
            len(docs),
            self._bm25 is not None,
            len(self._matrix_ids),
        )

    def is_empty(self) -> bool:
        return not self._chunks

    # --- rankings --------------------------------------------------------------------------

    def _lexical_ranking(self, tokens: list[str]) -> tuple[list[str], float]:
        """Chunk ids with BM25 score > 0, best first, plus the raw top score."""
        if self._bm25 is None or not tokens:
            return [], 0.0
        scores = np.asarray(self._bm25.get_scores(tokens), dtype=float)
        positive = np.flatnonzero(scores > 0)
        if positive.size == 0:
            # BM25 gives every term a negative weight once it appears in most of the corpus,
            # which is normal for a small knowledge base. Fall back to plain term overlap so a
            # one-document store still returns its best passages; coverage stays conservative
            # because the caller only sees the (zero) BM25 score.
            return self._overlap_ranking(tokens), 0.0
        order = sorted(positive.tolist(), key=lambda i: (-scores[i], self._chunks[i].id))
        return [self._chunks[i].id for i in order], float(scores[order[0]])

    def _overlap_ranking(self, tokens: list[str]) -> list[str]:
        """Rank chunks by how many distinct query terms they contain."""
        wanted = set(tokens)
        hits: list[tuple[int, int, str]] = []
        for index, chunk in enumerate(self._chunks):
            overlap = len(wanted & set(self._chunk_tokens[index]))
            if overlap:
                hits.append((-overlap, index, chunk.id))
        hits.sort()
        return [chunk_id for _neg, _index, chunk_id in hits]

    def _vector_ranking(self, query: str) -> tuple[list[str], float, bool]:
        """Chunk ids by cosine similarity, best first; ``used`` is False if no vectors."""
        if self.embedder is None or self._matrix is None or not self._matrix_ids:
            return [], 0.0, False
        try:
            qvec = np.asarray(self.embedder.embed_query(query), dtype=np.float32)
        except Exception:  # network / API problems must never break an answer
            logger.warning("query embedding failed; falling back to lexical only", exc_info=True)
            return [], 0.0, False
        norm = float(np.linalg.norm(qvec))
        if qvec.size != self._matrix.shape[1] or norm == 0:
            logger.warning("query embedding unusable (dim %d); lexical only", qvec.size)
            return [], 0.0, False
        sims = self._matrix @ (qvec / norm)
        order = np.argsort(-sims, kind="stable")[:MAX_VECTOR_CANDIDATES]
        ranking = [self._matrix_ids[i] for i in order if sims[i] > 0]
        top = float(sims[order[0]]) if order.size else 0.0
        return ranking, top, True

    # --- coverage --------------------------------------------------------------------------

    def _overlap_ratio(self, tokens: list[str], chunk_id: str | None) -> float:
        """Share of the distinct query terms that appear in a chunk."""
        if not tokens or chunk_id is None:
            return 0.0
        index = self._chunk_index.get(chunk_id)
        if index is None or index >= len(self._chunk_tokens):
            return 0.0
        wanted = set(tokens)
        return len(wanted & set(self._chunk_tokens[index])) / len(wanted)

    def _coverage(
        self,
        top_bm25: float,
        has_tokens: bool,
        top_cosine: float,
        has_vectors: bool,
        positive_count: int,
        overlap_ratio: float = 0.0,
    ) -> Literal["covered", "weak", "none"]:
        min_rel = self.settings.min_relevance
        weak_rel = self.settings.weak_relevance
        min_cos = self.settings.min_cosine
        cos_ok = has_vectors and top_cosine >= min_cos
        if (top_bm25 <= 0 or not has_tokens) and not cos_ok:
            # BM25 scores everything at or below zero on a very small corpus, where a term that
            # appears in most chunks carries a negative weight. Fall back to plain term overlap
            # so a one- or two-source knowledge base can still answer, but stay at "weak" so
            # Mark hedges rather than asserting.
            if has_tokens and overlap_ratio >= OVERLAP_COVERAGE_RATIO:
                return "weak"
            return "none"
        if top_bm25 < min_rel and not cos_ok:
            return "none"
        if top_bm25 < weak_rel and top_cosine < 0.5:
            return "weak"
        if positive_count < 2:
            return "weak"
        return "covered"

    # --- retrieval -------------------------------------------------------------------------

    def retrieve(self, query: str, k: int | None = None) -> RetrievalResult:
        """Return the best ``k`` chunks for ``query`` with a coverage verdict."""
        k = k or self.settings.top_k
        if self.is_empty():
            return RetrievalResult([], "none", False, False)

        tokens = tokenize(query)
        lex_ranking, top_bm25 = self._lexical_ranking(tokens)
        lexical_used = self._bm25 is not None and bool(tokens)
        vec_ranking, top_cosine, vector_used = self._vector_ranking(query)

        rankings = [r for r in (lex_ranking, vec_ranking) if r]
        if len(rankings) == 2:
            fused = reciprocal_rank_fusion(rankings, k=RRF_K)
        elif rankings:
            fused = {cid: 1.0 / (RRF_K + rank) for rank, cid in enumerate(rankings[0], start=1)}
        else:
            fused = {}

        lex_rank = {cid: r for r, cid in enumerate(lex_ranking, start=1)}
        vec_rank = {cid: r for r, cid in enumerate(vec_ranking, start=1)}

        candidates: list[RetrievedChunk] = []
        for cid, score in fused.items():
            idx = self._chunk_index.get(cid)
            if idx is None:
                continue
            chunk = self._chunks[idx]
            doc = self._docs.get(chunk.doc_id)
            if doc is None:
                continue
            candidates.append(
                RetrievedChunk(
                    chunk=chunk,
                    document=doc,
                    score=score,
                    lexical_rank=lex_rank.get(cid),
                    vector_rank=vec_rank.get(cid),
                )
            )
        candidates.sort(key=lambda rc: (-rc.score, rc.chunk.id))
        candidates = self._dedupe_episodes(candidates)[:k]

        if lexical_used:
            positive_count = len(lex_ranking)
        else:
            positive_count = sum(
                1
                for rc in candidates
                if rc.vector_rank is not None
                and self._cosine_at_rank(rc.vector_rank, vec_ranking, top_cosine) >= 0
            )
            positive_count = len(vec_ranking) if vector_used else 0
        overlap_ratio = self._overlap_ratio(tokens, lex_ranking[0] if lex_ranking else None)
        coverage = self._coverage(
            top_bm25, bool(tokens), top_cosine, vector_used, positive_count, overlap_ratio
        )

        logger.info(
            "retrieve: tokens=%d lexical=%s vector=%s top_bm25=%.2f top_cos=%.2f -> %s (%d)",
            len(tokens),
            lexical_used,
            vector_used,
            top_bm25,
            top_cosine,
            coverage,
            len(candidates),
        )
        return RetrievalResult(
            chunks=candidates,
            coverage=coverage,
            lexical_used=lexical_used,
            vector_used=vector_used,
            top_lexical_score=top_bm25,
            top_cosine=top_cosine,
            query_tokens=tokens,
        )

    @staticmethod
    def _cosine_at_rank(rank: int, ranking: list[str], top: float) -> float:
        """Placeholder kept trivial: positive cosine entries are the whole vector ranking."""
        return top if 0 < rank <= len(ranking) else -1.0

    @staticmethod
    def _dedupe_episodes(candidates: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Collapse the same episode ingested twice (e.g. YouTube + RSS) to one chunk.

        Only applies when an episode number appears under two different documents. The
        surviving chunk prefers the YouTube document (it has timestamped deep links),
        then the highest fused score. Input must already be sorted best-first.
        """
        docs_by_episode: dict[str, set[str]] = {}
        for rc in candidates:
            ep = rc.document.episode
            if ep:
                docs_by_episode.setdefault(ep, set()).add(rc.document.id)
        duplicated = {ep for ep, ids in docs_by_episode.items() if len(ids) > 1}
        if not duplicated:
            return candidates

        chosen: dict[str, RetrievedChunk] = {}
        for rc in candidates:
            ep = rc.document.episode
            if not ep or ep not in duplicated:
                continue
            current = chosen.get(ep)
            if current is None:
                chosen[ep] = rc
                continue
            rc_yt = rc.document.kind == SourceKind.YOUTUBE
            cur_yt = current.document.kind == SourceKind.YOUTUBE
            if rc_yt and not cur_yt:
                chosen[ep] = rc  # candidates are sorted, so this is the best YouTube chunk
        keep_ids = {rc.chunk.id for rc in chosen.values()}
        return [
            rc
            for rc in candidates
            if rc.document.episode not in duplicated or rc.chunk.id in keep_ids
        ]
