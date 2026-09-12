"""Chunking, storage, and retrieval."""

from __future__ import annotations

import threading

import numpy as np

from markai.knowledge.chunking import approx_tokens, chunk_document
from markai.knowledge.embeddings import build_embedder
from markai.knowledge.retriever import Retriever, reciprocal_rank_fusion, tokenize
from markai.knowledge.store import KnowledgeStore
from markai.models import Document, Segment, SourceKind
from tests.fakes import FakeEmbedder

# --- chunking -------------------------------------------------------------------------


def test_text_chunks_are_deterministic_and_overlap(toy_documents):
    website = toy_documents[0]
    first = chunk_document(website, target_words=30, overlap_words=8)
    second = chunk_document(website, target_words=30, overlap_words=8)
    assert [c.id for c in first] == [c.id for c in second]
    assert len(first) > 1
    assert all(c.text.strip() for c in first)
    tail = first[0].text.split()[-8:]
    assert any(word in first[1].text for word in tail)


def test_av_chunks_carry_a_time_window(toy_documents):
    youtube = toy_documents[1]
    chunks = chunk_document(youtube, av_window_seconds=40.0)
    assert len(chunks) > 1
    assert chunks[0].start_time == 0.0
    assert chunks[0].end_time is not None and chunks[0].end_time > 0
    assert all(c.start_time is not None for c in chunks)


def test_an_oversized_paragraph_is_split():
    doc = Document(
        id="d",
        kind=SourceKind.WEBSITE,
        title="Long",
        locator="https://x.test/long",
        text=" ".join(f"word{i}" for i in range(900)),
    )
    chunks = chunk_document(doc, target_words=100, overlap_words=10)
    assert len(chunks) > 1
    assert all(len(c.text.split()) <= 200 for c in chunks)


def test_an_empty_document_yields_no_chunks():
    doc = Document(id="d", kind=SourceKind.WEBSITE, title="Empty", locator="x", text="   ")
    assert chunk_document(doc) == []


def test_chunk_ids_are_namespaced_by_document(toy_documents):
    chunks = chunk_document(toy_documents[0], target_words=30)
    assert all(c.id.startswith(toy_documents[0].id) for c in chunks)
    assert chunks[0].id.endswith(":0000")


def test_token_estimate_is_roughly_a_quarter_of_the_characters():
    assert approx_tokens("a" * 400) == 100


# --- store ----------------------------------------------------------------------------


def test_document_roundtrip_keeps_every_field(settings, toy_documents):
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    original = toy_documents[1]
    store.upsert_document(original, chunk_document(original))

    loaded = store.get_document(original.id)
    assert loaded.title == original.title
    assert loaded.kind == SourceKind.YOUTUBE
    assert loaded.link == original.link
    assert loaded.episode == "212"
    assert loaded.published_at == "2023-04-18"
    assert loaded.content_hash == original.content_hash
    assert len(loaded.segments) == len(original.segments)
    assert isinstance(loaded.segments[0], Segment)
    store.close()


def test_upsert_replaces_the_previous_chunks(settings, toy_documents):
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    doc = toy_documents[0]
    store.upsert_document(doc, chunk_document(doc, target_words=25))
    many = len(store.chunks_for_document(doc.id))

    doc.text = "Now it is short."
    doc.content_hash = ""
    doc.ensure_hash()
    store.upsert_document(doc, chunk_document(doc, target_words=25))
    few = len(store.chunks_for_document(doc.id))

    assert few < many
    assert len(store.all_chunks()) == few
    store.close()


def test_embeddings_are_stored_and_filtered_by_model(settings, toy_documents):
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    embedder = FakeEmbedder()
    doc = toy_documents[0]
    chunks = chunk_document(doc, target_words=30)
    store.upsert_document(
        doc,
        chunks,
        embedder.embed_documents([c.text for c in chunks]),
        embedding_model=embedder.name,
    )

    matrix = store.embeddings_matrix(embedder.name)
    assert matrix is not None
    ids, vectors = matrix
    assert len(ids) == len(chunks)
    assert isinstance(vectors, np.ndarray)
    assert vectors.dtype == np.float32
    assert store.embeddings_matrix("some-other-model") is None
    assert store.stats().embedded_chunks == len(chunks)
    store.close()


def test_delete_and_locator_listing(settings, toy_documents):
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    for doc in toy_documents:
        store.upsert_document(doc, chunk_document(doc))
    assert len(store.list_locators()) == 3
    assert len(store.list_documents(SourceKind.YOUTUBE)) == 1

    store.delete_document(toy_documents[0].id)
    assert store.get_document(toy_documents[0].id) is None
    assert store.chunks_for_document(toy_documents[0].id) == []
    store.close()


def test_busy_timeout_is_set_so_a_second_writer_process_waits_instead_of_erroring(
    settings, toy_documents
):
    """Two `mark ingest` processes can share one data/markai.db. WAL still serializes writers,
    so a generous busy_timeout - not Python's 5s connect-timeout default - is what lets the
    loser wait out an ordinary contention window instead of raising "database is locked"."""
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    (timeout_ms,) = store._conn.execute("PRAGMA busy_timeout").fetchone()
    assert timeout_ms == 30000
    store.close()


def test_concurrent_writers_never_double_store_identical_content(settings):
    """The race from the audit: two writer *processes* (simulated here as two separate
    SQLite connections to the same file, driven from two threads) each ingest a document
    with identical text under a different id/locator, at the same instant. The old
    check-then-act (SELECT for a twin, then a separate INSERT) let both pass the check
    before either committed. `store_if_new_or_changed`'s `BEGIN IMMEDIATE` must serialize
    them so exactly one round trips "added" and the other "duplicate" - never both
    "added", which would leave two identical passages searchable.
    """
    settings.ensure_dirs()
    store_a = KnowledgeStore(settings.db_path)
    store_b = KnowledgeStore(settings.db_path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    outcomes: dict[str, str] = {}

    def race(store, key, doc_id, text):
        try:
            doc = Document(
                id=doc_id,
                kind=SourceKind.WEBSITE,
                title=doc_id,
                locator=doc_id,
                text=text,
            )
            doc.ensure_hash()
            chunks = chunk_document(doc)
            barrier.wait(timeout=5)
            outcome, _twin = store.store_if_new_or_changed(doc, chunks)
            outcomes[key] = outcome
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors` below
            errors.append(exc)

    for round_i in range(15):
        text = f"Identical bytes shared by both writers, round {round_i}."
        outcomes.clear()
        ta = threading.Thread(target=race, args=(store_a, "a", f"a-{round_i}", text))
        tb = threading.Thread(target=race, args=(store_b, "b", f"b-{round_i}", text))
        ta.start()
        tb.start()
        ta.join(timeout=10)
        tb.join(timeout=10)

        assert not errors, f"round {round_i}: {errors}"
        assert set(outcomes.values()) == {"added", "duplicate"}, (round_i, outcomes)

    # Never two rows sharing a content hash, however the two connections interleaved.
    hashes = [d.content_hash for d in store_a.list_documents()]
    assert len(hashes) == len(set(hashes))

    store_a.close()
    store_b.close()


def test_question_log_feeds_the_gaps_report(settings):
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    store.log_question(
        "s1",
        "Anything about roof decks?",
        "none",
        ["legal_topic"],
        True,
        {"input_tokens": 10, "output_tokens": 5},
    )
    store.log_question(
        "s1", "Deposits?", "covered", [], False, {"input_tokens": 8, "output_tokens": 4}
    )

    gaps = store.list_gaps()
    assert len(gaps) == 1
    assert "roof decks" in gaps[0]["question"]
    stats = store.stats()
    assert stats.questions_total == 2
    assert stats.questions_not_covered == 1
    assert stats.tokens_total == 27
    store.close()


def test_ingest_run_recording_updates_the_timestamp(settings):
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    assert store.stats().last_ingest_at is None
    store.record_ingest_run({"added": 1, "finished_at": "2026-01-01T00:00:00+00:00"})
    assert store.stats().last_ingest_at is not None
    store.close()


# --- retrieval ------------------------------------------------------------------------


def test_tokenize_drops_stopwords_but_keeps_numbers():
    tokens = tokenize("What is the 5-day notice for a tenant?")
    assert "5" in tokens and "day" in tokens and "notice" in tokens
    assert "the" not in tokens and "is" not in tokens


def test_rrf_rewards_agreement():
    scores = reciprocal_rank_fusion([["a", "b", "c"], ["b", "a", "c"]])
    assert scores["a"] > scores["c"]
    assert round(scores["a"], 6) == round(scores["b"], 6)


def test_retrieval_finds_the_right_document(settings, store):
    retriever = Retriever(store, None, settings)
    result = retriever.retrieve("security deposit interest")
    assert result.coverage in ("covered", "weak")
    assert result.chunks
    assert "deposit" in result.chunks[0].chunk.text.lower()
    assert result.chunks[0].document.kind == SourceKind.WEBSITE
    assert result.lexical_used


def test_retrieval_finds_the_heat_episode(settings, store):
    retriever = Retriever(store, None, settings)
    result = retriever.retrieve("boiler heat ordinance temperature")
    assert result.chunks
    assert result.chunks[0].document.episode == "198"


def test_gibberish_is_not_covered(settings, store):
    retriever = Retriever(store, None, settings)
    assert retriever.retrieve("zzzqqq wubble frobnicate").coverage == "none"


def test_a_stopword_only_query_is_not_covered(settings, store):
    retriever = Retriever(store, None, settings)
    assert retriever.retrieve("the a is of and").coverage == "none"


def test_an_empty_store_never_builds_bm25(settings, empty_store):
    retriever = Retriever(empty_store, None, settings)
    assert retriever.is_empty()
    result = retriever.retrieve("anything at all")
    assert result.chunks == [] and result.coverage == "none"


def test_refresh_picks_up_new_documents(settings, empty_store, toy_documents):
    retriever = Retriever(empty_store, None, settings)
    assert retriever.is_empty()

    doc = toy_documents[0]
    empty_store.upsert_document(doc, chunk_document(doc, target_words=40))
    retriever.refresh()

    assert not retriever.is_empty()
    assert retriever.retrieve("security deposit interest").chunks


def test_vector_search_is_used_when_an_embedder_exists(settings, empty_store, toy_documents):
    embedder = FakeEmbedder()
    for doc in toy_documents:
        chunks = chunk_document(doc, target_words=40)
        empty_store.upsert_document(
            doc,
            chunks,
            embedder.embed_documents([c.text for c in chunks]),
            embedding_model=embedder.name,
        )
    retriever = Retriever(empty_store, embedder, settings)
    result = retriever.retrieve("security deposit interest")
    assert result.vector_used
    assert result.chunks


def test_results_are_capped_at_k(settings, store):
    retriever = Retriever(store, None, settings)
    assert len(retriever.retrieve("chicago tenant deposit heat screening", k=2).chunks) <= 2


def test_build_embedder_is_none_without_a_key(settings):
    assert build_embedder(settings) is None


def test_a_single_source_store_still_answers(settings, empty_store, toy_documents):
    """BM25 scores everything at zero on a tiny corpus; term overlap must rescue it."""
    doc = toy_documents[0]
    empty_store.upsert_document(doc, chunk_document(doc, target_words=400))
    retriever = Retriever(empty_store, None, settings)

    relevant = retriever.retrieve("security deposit interest")
    assert relevant.chunks
    assert relevant.coverage == "weak"

    irrelevant = retriever.retrieve("zzzqqq wubble frobnicate")
    assert irrelevant.chunks == []
    assert irrelevant.coverage == "none"


def test_embeddings_can_be_backfilled_without_re_ingesting(settings, toy_documents):
    """Adding a Voyage key later must not mean re-downloading every source."""
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    doc = toy_documents[0]
    chunks = chunk_document(doc, target_words=40)
    store.upsert_document(doc, chunks)  # ingested with no embedder configured

    embedder = FakeEmbedder()
    pending = store.chunks_missing_embeddings(embedder.name)
    assert len(pending) == len(chunks)
    assert store.embeddings_matrix(embedder.name) is None

    store.set_embeddings(
        {
            c.id: v
            for c, v in zip(
                pending, embedder.embed_documents([c.text for c in pending]), strict=True
            )
        },
        embedder.name,
    )

    assert store.chunks_missing_embeddings(embedder.name) == []
    matrix = store.embeddings_matrix(embedder.name)
    assert matrix is not None and len(matrix[0]) == len(chunks)
    assert store.stats().embedded_chunks == len(chunks)
    store.close()


def test_switching_embedding_model_marks_everything_as_pending(settings, toy_documents):
    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    doc = toy_documents[0]
    chunks = chunk_document(doc, target_words=40)
    embedder = FakeEmbedder()
    store.upsert_document(
        doc,
        chunks,
        embedder.embed_documents([c.text for c in chunks]),
        embedding_model=embedder.name,
    )
    assert store.chunks_missing_embeddings(embedder.name) == []
    assert len(store.chunks_missing_embeddings("voyage-3.5")) == len(chunks)
    store.close()


# --- a question in the other language --------------------------------------------------
#
# "desalojo inquilino no paga" retrieved exactly the right eviction posts with a semantic
# score of 0.550 - over the bar - and still came back "weak", so Jay told the owner it was
# not covered. A Spanish question shares no words with English sources, so the keyword arm
# scores zero by construction and the "at least two keyword hits" rule could never pass.


def _coverage_for(settings, **kwargs):
    from markai.knowledge.retriever import Retriever

    verdict = Retriever.__dict__["_coverage"]
    args = {
        "top_bm25": 0.0,
        "has_tokens": True,
        "top_cosine": 0.0,
        "has_vectors": True,
        "positive_count": 0,
        "overlap_ratio": 0.0,
    }
    args.update(kwargs)

    class Stub:
        pass

    stub = Stub()
    stub.settings = settings
    return verdict(stub, **args)


def test_a_strong_semantic_match_alone_counts_as_covered(settings):
    """The owner's real numbers: no keyword signal at all, cosine 0.550."""
    assert _coverage_for(settings, top_cosine=0.550, positive_count=0) == "covered"


def test_a_middling_semantic_match_without_keywords_stays_weak(settings):
    """Above the 0.35 floor but under the covered bar: found something, not sure enough."""
    assert _coverage_for(settings, top_cosine=0.42, positive_count=0) == "weak"


def test_below_the_cosine_floor_is_none(settings):
    assert _coverage_for(settings, top_cosine=0.30, positive_count=0) == "none"


def test_nothing_on_either_arm_is_still_none(settings):
    assert _coverage_for(settings, top_cosine=0.0, has_vectors=False) == "none"


def test_the_covered_bar_is_tunable(settings):
    """Raising it takes the owner's real 0.550 case back to weak, which is the knob."""
    strict = settings.model_copy(update={"covered_cosine": 0.7})
    assert _coverage_for(strict, top_cosine=0.550, positive_count=0) == "weak"


def test_keyword_evidence_alone_still_carries_a_question(settings):
    """English questions must behave exactly as before."""
    assert _coverage_for(settings, top_bm25=9.0, positive_count=4, has_vectors=False) == "covered"
    assert _coverage_for(settings, top_bm25=9.0, positive_count=1, has_vectors=False) == "weak"


def test_declined_gaps_can_be_cleared_without_losing_the_question_log(settings):
    """Those rows were never missing material: Jay said he had nothing and he did."""
    from markai.knowledge.store import KnowledgeStore

    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    store.log_question(None, "who is Mark Ainley?", "covered", [], True, {})
    store.log_question(None, "Parking permits in Winnetka?", "none", [], True, {})
    store.log_question(None, "Snow removal in Evanston?", "weak", [], True, {})

    assert len(store.list_gaps(10)) == 3
    assert store.forget_declined_gaps() == 1
    assert [g["coverage"] for g in store.list_gaps(10)] == ["weak", "none"]
    assert store.stats().questions_total == 3, "the usage record is untouched"
    assert store.forget_declined_gaps() == 0, "nothing left to clear"
    store.close()
