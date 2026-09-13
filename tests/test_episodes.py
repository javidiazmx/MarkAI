"""The episode index: guest parsing, topics, the search, and the tool Jay calls."""

from __future__ import annotations

import pytest

from markai.knowledge.episodes import (
    EPISODE_TOOL,
    catalog,
    dedupe_terms,
    deep_link,
    find_moments,
    guest_from_title,
    run_episode_tool,
    strip_episode_prefix,
)
from markai.knowledge.retriever import Retriever
from markai.models import Document, SourceKind


@pytest.fixture
def retriever(store, settings) -> Retriever:
    return Retriever(store, None, settings)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Ep. 214: Boilers 101", "Boilers 101"),
        ("Episode 214 - Boilers 101", "Boilers 101"),
        ("#214 Boilers 101", "Boilers 101"),
        ("214: Boilers 101", "Boilers 101"),
        ("Boilers 101", "Boilers 101"),
    ],
)
def test_the_episode_number_is_stripped_from_the_title(title, expected):
    assert strip_episode_prefix(title) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Ep. 214: Boilers 101 with Jane Doe", "Jane Doe"),
        ("Ep 92: Screening ft. Robert Van Dyke", "Robert Van Dyke"),
        ("Guest: Maria Lopez on section 8", "Maria Lopez"),
        ("Jane Doe on scaling to 100 units", "Jane Doe"),
        ("214: Maria Lopez - eviction court", "Maria Lopez"),
        ("Boilers 101 con Juan Perez", "Juan Perez"),
    ],
)
def test_a_guest_in_the_title_is_read_off_it(title, expected):
    assert guest_from_title(title) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Every one of these is a real title off the owner's channel.
        (
            "Crazy Stories of Off-Market Real Estate Deals with Igor Mike Kajpust",
            "Igor Mike Kajpust",
        ),
        ("Chicago Ground-Up Developments with Matt Katsaros", "Matt Katsaros"),
        ("0-9 Unit Portfolio in Under Two Years with Hart Turner", "Hart Turner"),
        (
            "36th Ward: Future of Chicago with Alderman Gilbert Villegas",
            "Alderman Gilbert Villegas",
        ),
        # Two guests, written two ways.
        (
            "Cracking Cash Flow on Chicago's West Side with Jay Patel and Tedi Nati",
            "Jay Patel and Tedi Nati",
        ),
        (
            "Breaking Down Chicagoland Syndications W/Duke Dennis & Bryan Sonn",
            "Duke Dennis and Bryan Sonn",
        ),
    ],
)
def test_the_owners_own_titles_are_read_correctly(title, expected):
    """`Cracking Cash Flow` came back as the guest until the head pattern learned to
    stand down when the title already said "with"."""
    assert guest_from_title(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Ep. 214: Boilers 101",
        # Real ones with no guest named. Nothing is better than a guess.
        "Chicago's Josh Bandoch Using Neuroscience to Win Negotiations",
        "Brendan McElhaney's Journey To Wholesaling Deal Flow",
        "What to do with a 2 flat in Berwyn",
        "Ep 5: Screening with a co-signer",  # "a co-signer" is not a name
        "Section 8 on the South Side",
        "Ep. 12: Deposits with Mark Ainley",  # the host is not a guest
        "",
    ],
)
def test_a_title_without_a_guest_returns_none(title):
    assert guest_from_title(title) is None


def test_a_youtube_link_jumps_to_the_second(toy_documents):
    youtube = next(d for d in toy_documents if d.kind == SourceKind.YOUTUBE)
    assert deep_link(youtube, 92.5).endswith("&t=92s")
    assert deep_link(youtube, None) == youtube.link


def test_a_podcast_link_stays_as_it_is(toy_documents):
    podcast = next(d for d in toy_documents if d.kind == SourceKind.PODCAST)
    assert deep_link(podcast, 92.5) == podcast.link


def test_a_link_that_is_not_a_url_is_dropped():
    doc = Document(
        id="x", kind=SourceKind.PODCAST, title="Local file", locator="file:notes.txt", text="hi"
    )
    assert deep_link(doc, None) is None


def test_a_singular_and_its_plural_are_one_topic():
    assert dedupe_terms(["deposits", "deposit", "boiler"]) == ["deposits", "boiler"]


def test_a_search_returns_the_episode_the_minute_and_a_quote(retriever):
    moments = find_moments(retriever, "heat ordinance temperature", limit=3)
    assert moments, "the heat episode is in the toy corpus"
    heat = moments[0]
    assert heat.number == "198"
    assert heat.kind == SourceKind.PODCAST
    assert heat.timestamp is not None, "a transcript hit carries the minute"
    assert "heat" in heat.quote.lower() or "degrees" in heat.quote.lower()
    assert heat.label().startswith("Ep. 198 · ")


def test_a_search_never_returns_a_web_page(retriever):
    # The deposit rules live on a website in the toy corpus, and it outranks both episodes.
    for moment in find_moments(retriever, "security deposit interest", limit=5):
        assert moment.kind in (SourceKind.PODCAST, SourceKind.YOUTUBE)


def test_one_moment_per_episode(retriever):
    moments = find_moments(retriever, "screening applicants credit income", limit=5)
    numbers = [m.number for m in moments]
    assert len(numbers) == len(set(numbers)), "an episode is listed once, at its best moment"


def test_a_youtube_hit_links_to_the_moment(retriever):
    moments = find_moments(retriever, "screening criteria every applicant", limit=3)
    youtube = next(m for m in moments if m.kind == SourceKind.YOUTUBE)
    assert "t=" in youtube.url


def test_an_empty_query_searches_nothing(retriever):
    assert find_moments(retriever, "   ") == []


def test_an_empty_knowledge_base_returns_nothing(empty_store, settings):
    assert find_moments(Retriever(empty_store, None, settings), "boilers") == []


def test_topics_come_from_what_sets_the_episode_apart(retriever, toy_documents):
    heat = next(d for d in toy_documents if d.kind == SourceKind.PODCAST)
    terms = retriever.distinctive_terms(heat.id, limit=6)
    # A three-document corpus gives BM25 very little to work with, so the contract is only
    # that nothing generic sneaks in and nothing crashes.
    assert all(len(term) >= 4 and not term.isdigit() for term in terms)
    assert retriever.distinctive_terms("no-such-doc") == []


def test_the_catalog_lists_every_episode_newest_first(store):
    entries = catalog(store)
    assert [e.number for e in entries] == ["212", "198"]
    assert all(e.transcribed for e in entries)
    assert not any(e.kind == SourceKind.WEBSITE for e in entries)


def test_the_catalog_can_be_filtered_by_guest(store, toy_documents):
    guest = Document(
        id=Document.make_id(SourceKind.PODCAST, "episode:300"),
        kind=SourceKind.PODCAST,
        title="Ep. 300: Turnover costs with Jane Doe",
        locator="episode:300",
        text="Turnover costs run higher than most owners budget.",
        episode="300",
        link="https://example.com/300",
    )
    guest.ensure_hash()
    store.upsert_document(guest, [])

    found = catalog(store, guest="jane")
    assert [e.number for e in found] == ["300"]
    assert found[0].guest == "Jane Doe"
    assert catalog(store, guest="nobody") == []


def test_a_show_notes_episode_is_marked_as_one(store):
    notes = Document(
        id=Document.make_id(SourceKind.PODCAST, "episode:301"),
        kind=SourceKind.PODCAST,
        title="Ep. 301: No transcript",
        locator="episode:301",
        text="Show notes only.",
        episode="301",
        metadata={"transcript_method": "show_notes"},
    )
    notes.ensure_hash()
    store.upsert_document(notes, [])
    entry = next(e for e in catalog(store) if e.number == "301")
    assert entry.transcribed is False


# --- the tool ---------------------------------------------------------------------------


def test_the_tool_is_shaped_the_way_opus_5_wants_it():
    assert EPISODE_TOOL["name"] == "find_episode"
    assert EPISODE_TOOL["strict"] is True
    schema = EPISODE_TOOL["input_schema"]
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == sorted(schema["properties"]), (
        "a strict tool has to require every property it declares"
    )


def test_the_tool_returns_episodes_as_plain_data(retriever):
    result = run_episode_tool(retriever, {"topic": "heat ordinance", "limit": 2})
    assert result["found"] >= 1
    first = result["episodes"][0]
    assert set(first) >= {"number", "title", "guest", "url", "timestamp", "topics", "quote"}
    assert isinstance(first["topics"], list)


def test_the_tool_clamps_a_silly_limit(retriever):
    assert len(run_episode_tool(retriever, {"topic": "chicago", "limit": 99})["episodes"]) <= 5
    assert run_episode_tool(retriever, {"topic": "chicago", "limit": "x"})["found"] >= 0


def test_the_tool_refuses_an_empty_topic(retriever):
    assert "error" in run_episode_tool(retriever, {"topic": "  ", "limit": 3})


def test_a_broken_search_is_an_error_not_a_crash():
    class Broken:
        def is_empty(self):
            return False

        def retrieve(self, *_args, **_kwargs):
            raise RuntimeError("index is gone")

    result = run_episode_tool(Broken(), {"topic": "boilers", "limit": 3})
    assert result == {"error": "The episode index could not be searched."}


# --- topics on a corpus big enough to have a middle ------------------------------------


@pytest.fixture
def wide_store(settings):
    """60 episodes: one subject shared by ten of them, one name, and a footer on all."""
    from markai.knowledge.chunking import chunk_document
    from markai.knowledge.store import KnowledgeStore

    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    for index in range(60):
        body = ["subscribe to straightupchicagoinvestor every week for the show footer"] * 3
        if index < 10:
            # A real subject: ten episodes touch it, and the first one is about it.
            body += ["the boiler and the radiator carry the heating load"] * 3
            body += ["klemm walked the building with us and priced the work"] * 3
        if index == 0:
            body += ["boiler boiler radiator radiator condensate condensate"] * 2
            body += ["spybar came up once and never again in this corpus"] * 3
        doc = Document(
            id=Document.make_id(SourceKind.PODCAST, f"episode:{index}"),
            kind=SourceKind.PODCAST,
            title=f"Ep. {index}: Heating systems with Jonathan Klemm",
            locator=f"episode:{index}",
            text=" ".join(body),
            episode=str(index),
            link=f"https://example.com/{index}",
        )
        doc.ensure_hash()
        store.upsert_document(doc, chunk_document(doc, target_words=60, overlap_words=10))
    yield store
    store.close()


def test_a_word_from_the_footer_is_not_a_topic(wide_store, settings):
    terms = Retriever(wide_store, None, settings).distinctive_terms(
        Document.make_id(SourceKind.PODCAST, "episode:0")
    )
    assert "straightupchicagoinvestor" not in terms
    assert "subscribe" not in terms, "in every episode, so it distinguishes none of them"


def test_a_word_used_in_one_episode_only_is_not_a_topic(wide_store, settings):
    terms = Retriever(wide_store, None, settings).distinctive_terms(
        Document.make_id(SourceKind.PODCAST, "episode:0")
    )
    assert "spybar" not in terms, "a one-off is a name or a brand, not a subject"
    assert "boiler" in terms, "and the subject ten episodes share is"


def test_the_guests_name_is_not_repeated_as_a_topic(wide_store, settings):
    retriever = Retriever(wide_store, None, settings)
    doc_id = Document.make_id(SourceKind.PODCAST, "episode:0")
    # "klemm" is spread over ten episodes and said often enough to score, so only the
    # exclusion keeps it out: it is already on screen as the guest.
    assert "klemm" in retriever.distinctive_terms(doc_id, exclude=frozenset())
    moment = find_moments(retriever, "boiler radiator heating load", limit=1)[0]
    assert moment.guest == "Jonathan Klemm"
    assert "klemm" not in moment.topics and "jonathan" not in moment.topics
    assert "heating" not in moment.topics, "it is in the title, which the reader can see"


def test_a_plural_and_its_singular_are_one_topic():
    assert dedupe_terms(["porches", "porch", "gutters", "gutter"]) == ["porches", "gutters"]
    assert dedupe_terms(["policy", "policies"]) == ["policy"]


def test_a_quote_loses_the_caption_speaker_markers():
    from markai.knowledge.episodes import clean_quote

    # A leading ">>" just means the passage starts as someone begins talking - dropped,
    # not truncated on. The second ">>" is a real splice into a different speaker, so
    # the quote stops there rather than reading as one person's unbroken line.
    assert clean_quote(">> Good stuff here. >>  I got nothing else, man.") == "Good stuff here."
    assert clean_quote("No markers at all here.") == "No markers at all here."
    assert clean_quote("  line one\n\nline two  ") == "line one line two"


def test_speaker_turns_become_legible_instead_of_escaping_to_noise():
    from markai.knowledge.episodes import mark_speaker_turns

    # Rendered into the prompt, a raw ">>" escapes to "&gt;&gt;" (prompt_builder escapes
    # all knowledge-base text) and reads as noise, not the turn boundary it is. This runs
    # first so the model sees something legible - and it must not touch what is stored,
    # since nothing here depends on a re-ingest.
    assert mark_speaker_turns(">> I know, right?") == " [voice changes] I know, right?"
    assert mark_speaker_turns("no markers here") == "no markers here"
    assert mark_speaker_turns("") == ""
