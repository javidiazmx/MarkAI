"""Mining facts out of the indexed sources: what gets read, and what gets refused."""

from __future__ import annotations

import json

import pytest
import yaml

from markai.facts_miner import (
    MinerReport,
    Proposal,
    as_yaml_entry,
    build_batch_prompt,
    candidates_in,
    estimate,
    fingerprint,
    group_proposals,
    has_a_price,
    insert_into_facts,
    mine,
    quote_is_real,
    worth_reading,
)
from markai.models import Document, SourceKind

RULE = "The landlord must return the security deposit within 45 days of the tenant vacating."
STORY = "We bought a three flat in Logan Square back in 2015 and it was a grind at first."


# --- what is worth spending a token on ----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        RULE,
        "Heat must reach 68 degrees during the day under the ordinance.",
        "A late fee cannot exceed $10 plus 5% of the amount over $500.",
        "You are required to file within 30 days of the notice.",
    ],
)
def test_a_passage_that_states_a_rule_is_read(text):
    assert worth_reading(text) is True


@pytest.mark.parametrize(
    "text",
    [
        STORY,
        "The building has 6 units and a shared yard.",  # a number, no rule
        "You must always treat tenants fairly.",  # a rule, no number
        "",
    ],
)
def test_commentary_is_not(text):
    """Most of a corpus is prose. Reading it costs money and yields nothing."""
    assert worth_reading(text) is False


# --- the safety property ------------------------------------------------------------------


def test_a_quote_has_to_be_in_the_passage():
    assert quote_is_real("must return the security deposit within 45 days", RULE) is True
    assert quote_is_real("must return\n  the security  deposit within 45 days", RULE) is True


@pytest.mark.parametrize(
    ("quote", "why"),
    [
        ("must return the deposit within 30 days", "a number that drifted"),
        ("the landlord may keep the deposit", "a claim the passage never made"),
        ("45 days", "too short to mean anything"),
        ("", "nothing at all"),
    ],
)
def test_anything_the_passage_does_not_say_is_refused(quote, why):
    assert quote_is_real(quote, RULE) is False, why


class FakeMessages:
    """An Anthropic client that answers with whatever entries a test hands it."""

    def __init__(self, payloads: list[dict], fail_first: bool = False) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict] = []
        self.fail_first = fail_first

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("the API had a moment")
        payload = self.payloads.pop(0) if self.payloads else {"entries": []}

        class Block:
            type = "text"
            text = json.dumps(payload)

        class Usage:
            input_tokens = 100
            output_tokens = 50

        class Response:
            content = [Block()]
            usage = Usage()

        return Response()


class FakeClient:
    def __init__(self, payloads: list[dict], fail_first: bool = False) -> None:
        self.messages = FakeMessages(payloads, fail_first)


@pytest.fixture
def mined_store(settings):
    """A store holding one passage with a rule in it and one with a story."""
    from markai.knowledge.chunking import chunk_document
    from markai.knowledge.store import KnowledgeStore

    settings.ensure_dirs()
    store = KnowledgeStore(settings.db_path)
    for index, text in enumerate([RULE + " " + RULE, STORY + " " + STORY]):
        doc = Document(
            id=f"d{index}",
            kind=SourceKind.WEBSITE,
            title=f"Post {index}",
            locator=f"https://example.com/{index}",
            text=text,
        )
        doc.ensure_hash()
        store.upsert_document(doc, chunk_document(doc, target_words=60, overlap_words=10))
    yield store
    store.close()


def test_only_the_passages_with_rules_are_sent(mined_store):
    found, seen = candidates_in(mined_store)
    assert seen == 2, "both passages were looked at"
    # The label carries the document link after a separator, so an accepted rule can point
    # back at the page. The title is the half in front of it.
    assert [label.split("\u241f")[0] for _, label, _ in found] == ["Post 0"]


def test_a_proposal_carries_the_quote_and_the_source(mined_store):
    client = FakeClient(
        [
            {
                "entries": [
                    {
                        "kind": "ordinance",
                        "jurisdiction": "Chicago",
                        "topic": "security deposit return",
                        "rule": "45 days from the tenant vacating.",
                        "citation": "RLTO 5-12-080",
                        "quote": "must return the security deposit within 45 days",
                        "passage": 0,
                    }
                ]
            }
        ]
    )
    report = mine(mined_store, client, "claude-opus-5")

    assert len(report.proposals) == 1
    proposal = report.proposals[0]
    assert proposal.topic == "security deposit return"
    assert proposal.source_title == "Post 0"
    assert proposal.quote in RULE
    assert report.cost_usd > 0


def test_an_entry_the_passage_does_not_support_is_thrown_away(mined_store):
    """The whole safety property. A rule the sources never stated would arrive wearing
    their name, and the owner would have no way to tell."""
    client = FakeClient(
        [
            {
                "entries": [
                    {
                        "kind": "ordinance",
                        "topic": "deposit interest",
                        "rule": "Interest is 5% a year.",
                        "quote": "the landlord must pay 5 percent interest annually",
                        "passage": 0,
                    }
                ]
            }
        ]
    )
    report = mine(mined_store, client, "claude-opus-5")
    assert report.proposals == []
    assert report.dropped_unquoted == 1


def test_an_entry_pointing_at_no_passage_is_thrown_away(mined_store):
    client = FakeClient(
        [
            {
                "entries": [
                    {
                        "kind": "ordinance",
                        "topic": "x",
                        "rule": "y",
                        "quote": "z" * 40,
                        "passage": 99,
                    }
                ]
            }
        ]
    )
    report = mine(mined_store, client, "claude-opus-5")
    assert report.proposals == [] and report.dropped_unquoted == 1


def test_a_failed_batch_does_not_lose_the_run(mined_store):
    client = FakeClient([{"entries": []}], fail_first=True)
    report = mine(mined_store, client, "claude-opus-5")
    assert report.batches_failed == 1
    assert report.read_chunk_ids == [], "a batch that failed is not marked as read"


def test_a_second_run_skips_what_the_first_one_read(mined_store):
    client = FakeClient([{"entries": []}])
    first = mine(mined_store, client, "claude-opus-5")
    assert first.read_chunk_ids

    again = mine(
        mined_store, FakeClient([]), "claude-opus-5", already_read=set(first.read_chunk_ids)
    )
    assert again.passages_read == 0, "nothing left to pay for"


def test_the_passages_reach_the_model_escaped(mined_store):
    prompt = build_batch_prompt([(0, "A post", "Rent is due <b>on the first</b> & no later")])
    assert "&lt;b&gt;" in prompt and "&amp;" in prompt, "a crawl is untrusted text"


def test_the_estimate_is_in_dollars():
    plan = estimate([("c", "t", "x" * 1200)] * 120)
    assert plan["batches"] == 10
    assert 0.3 < plan["usd"] < 3.0, "cheap enough to run, dear enough to be told first"
    assert plan["usd_max"] > plan["usd"], "the owner decides against the worst case"


def test_the_estimate_covers_what_the_first_real_run_cost():
    """2640 passages of about 1190 characters came to $12.36. The estimate has to cover it.

    The first version quoted $8.21 for that run because it allowed 700 output tokens per
    *request*, and the reply is entries, which scale with passages.
    """
    plan = estimate([("c", "t", "x" * 1190)] * 2640)
    assert plan["usd"] >= 12.36, f"undershot again: ${plan['usd']:.2f}"
    assert plan["usd"] < 20.0, "generous, not alarming"


# --- a pile too big to walk one at a time -------------------------------------------------


def _raw(topic, rule, quote, source, kind="ordinance", **extra):
    return {
        "kind": kind,
        "topic": topic,
        "rule": rule,
        "quote": quote,
        "source": source,
        **extra,
    }


def test_the_same_sentence_from_four_posts_is_one_decision():
    quote = "The landlord must return the deposit within 45 days."
    waiting = [
        _raw("security deposit", "Return it in 45 days.", quote, f"Post {n}") for n in range(4)
    ]
    groups = group_proposals(waiting)
    assert len(groups) == 1
    assert groups[0].support == 4, "it says how many of their sources stated it"
    assert len(groups[0].sources()) == 4
    assert groups[0].indices == [0, 1, 2, 3], "accepting it answers all four"


def test_two_rules_that_differ_in_a_number_stay_two_rules():
    waiting = [
        _raw("deposit", "45 days.", "Return the deposit within 45 days.", "A"),
        _raw("deposit", "30 days.", "Return the deposit within 30 days.", "B"),
    ]
    assert len(group_proposals(waiting)) == 2, "guessing these are the same merges a number"


def test_a_price_and_a_rule_quoting_one_sentence_are_not_merged():
    quote = "A new boiler runs $8,000 and must be permitted."
    waiting = [
        _raw("boiler", "Needs a permit.", quote, "A"),
        _raw("boiler", "$8,000.", quote, "B", kind="cost", low=8000, high=8000),
    ]
    assert len({fingerprint(raw) for raw in waiting}) == 2


def test_subjects_the_owner_already_covers_go_to_the_back():
    waiting = [
        _raw("heat ordinance", "68 degrees.", "Heat must reach 68 degrees by day.", "A"),
        _raw("radon testing", "Test it.", "A radon test is required within 30 days.", "B"),
    ]
    groups = group_proposals(waiting, known_terms=frozenset({"heat", "ordinance"}))
    assert [g.lead["topic"] for g in groups] == ["radon testing", "heat ordinance"]
    assert groups[-1].known is True, "so the prompt can say you already have one of these"


def test_one_subject_arrives_together():
    waiting = [
        _raw(
            "deposit interest", "Pay interest.", "Interest is due on deposits over 6 months.", "A"
        ),
        _raw("snow removal", "Shovel it.", "Snow must be cleared within 24 hours.", "B"),
        _raw("deposit return", "45 days.", "Return the deposit within 45 days.", "C"),
    ]
    topics = [g.topic for g in group_proposals(waiting)]
    assert topics[0] == topics[1], "both deposit rules, then the snow one"


def test_the_lead_of_a_group_is_the_one_worth_reading():
    quote = "Heat must reach 68 degrees during the day."
    waiting = [
        _raw("heat", "68 by day.", quote, "Short post"),
        _raw("heat", "68 degrees from 8:30am to 10:30pm.", quote, "Long post", citation="5-12-110"),
    ]
    (group,) = group_proposals(waiting)
    assert group.lead["citation"] == "5-12-110", "a cited entry beats an uncited one"


def test_a_price_with_no_number_never_reaches_the_file():
    assert (
        has_a_price(_raw("boiler", "It is dear.", "A boiler is dear.", "A", kind="cost")) is False
    )
    assert has_a_price(_raw("boiler", "$8k.", "A boiler is $8,000.", "A", kind="cost", low=8000))
    assert has_a_price(_raw("heat", "68.", "Heat must reach 68.", "A")) is True


def test_an_entry_says_how_many_sources_stated_it():
    proposal = Proposal(
        kind="ordinance",
        topic="heat",
        rule="68 degrees by day.",
        quote="Heat must reach 68 degrees during the day.",
        source_title="A post",
        support=4,
    )
    entry = yaml.safe_load(insert_into_facts("", "ordinances", as_yaml_entry(proposal, "mined-1")))
    assert "stated in 4 of your sources" in entry["ordinances"][0]["notes"]


# --- writing it down ----------------------------------------------------------------------


def test_an_accepted_entry_keeps_the_owners_comments():
    body = (
        "# Rules for yourself:\n#   - Every ordinance needs a citation.\n"
        "ordinances:\n  - id: existing\n    jurisdiction: Chicago\n    topic: t\n"
        "    rule: r\n    citation: c\n"
    )
    proposal = Proposal(
        kind="ordinance",
        topic="security deposit return",
        rule="45 days from the tenant vacating.",
        quote="must return the security deposit within 45 days",
        source_title="Post 0",
        jurisdiction="Chicago",
        citation="RLTO 5-12-080",
    )
    out = insert_into_facts(body, "ordinances", as_yaml_entry(proposal, "mined-1"))

    assert "# Rules for yourself:" in out, "the file is edited by hand; the comments are why"
    loaded = yaml.safe_load(out)
    assert [o["id"] for o in loaded["ordinances"]] == ["mined-1", "existing"]
    assert loaded["ordinances"][0]["citation"] == "RLTO 5-12-080"
    assert "must return the security deposit" in loaded["ordinances"][0]["notes"]


def test_a_cost_entry_lands_in_the_costs_section():
    proposal = Proposal(
        kind="cost",
        topic="Boiler replacement",
        rule="Nine to sixteen thousand.",
        quote="a boiler runs $9,000 to $16,000 in a small building",
        source_title="Ep. 214",
        low=9000,
        high=16000,
        unit="per building",
    )
    out = insert_into_facts("ordinances: []\n", "costs", as_yaml_entry(proposal, "mined-1"))
    loaded = yaml.safe_load(out)
    assert loaded["costs"][0]["low"] == 9000 and loaded["costs"][0]["high"] == 16000


def test_an_entry_with_quotes_and_colons_in_it_still_parses():
    """A rule copied out of a blog post is arbitrary text, not a YAML-safe string."""
    proposal = Proposal(
        kind="ordinance",
        topic='deposits: the "45 day" rule',
        rule='He said: "return it within 45 days", and meant it.',
        quote="return it within 45 days",
        source_title="A post: part 2",
    )
    out = insert_into_facts("ordinances: []\n", "ordinances", as_yaml_entry(proposal, "mined-1"))
    loaded = yaml.safe_load(out)
    assert loaded["ordinances"][0]["topic"] == 'deposits: the "45 day" rule'


def test_the_accepted_entry_is_valid_to_the_fact_loader(tmp_path):
    """It has to load as a real ordinance, citation and all, or Jay never sees it."""
    from markai.sources.facts import load_facts

    proposal = Proposal(
        kind="ordinance",
        topic="security deposit return",
        rule="45 days from the tenant vacating.",
        quote="must return the security deposit within 45 days",
        source_title="Post 0",
        citation="RLTO 5-12-080",
    )
    path = tmp_path / "facts.yaml"
    path.write_text(
        insert_into_facts("", "ordinances", as_yaml_entry(proposal, "mined-1")), encoding="utf-8"
    )
    book = load_facts(path)
    assert book.ordinances[0].citation == "RLTO 5-12-080"
    assert book.ordinances[0].jurisdiction == "Chicago"


def test_a_report_with_no_calls_costs_nothing():
    assert MinerReport().cost_usd == 0.0


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("# keep me\nordinances: []\n", "an empty inline list becomes a block"),
        (
            "# keep me\nordinances:\n  - id: old\n    topic: t\n    rule: r\n    citation: c\n",
            "a block gains one",
        ),
        ("costs: []\n", "a missing key is created"),
        ("", "an empty file"),
    ],
)
def test_the_key_is_never_written_twice(body, why):
    """YAML keeps the last of two identical keys and drops the first without a word. An
    append that looked fine would delete every rule the owner had written."""
    proposal = Proposal(
        kind="ordinance", topic="t", rule="r", quote="q" * 30, source_title="S", citation="c"
    )
    out = insert_into_facts(body, "ordinances", as_yaml_entry(proposal, "mined-1"))
    assert out.count("\nordinances:") + out.startswith("ordinances:") == 1, why
    assert "mined-1" in [o["id"] for o in yaml.safe_load(out)["ordinances"]]


def test_an_existing_rule_is_never_lost():
    body = "ordinances:\n  - id: old\n    topic: t\n    rule: r\n    citation: c\n"
    proposal = Proposal(
        kind="ordinance", topic="t", rule="r", quote="q" * 30, source_title="S", citation="c"
    )
    out = insert_into_facts(body, "ordinances", as_yaml_entry(proposal, "mined-1"))
    assert [o["id"] for o in yaml.safe_load(out)["ordinances"]] == ["mined-1", "old"]


def test_a_shape_this_cannot_edit_is_refused_not_guessed_at():
    from markai.facts_miner import CannotInsert

    proposal = Proposal(
        kind="ordinance", topic="t", rule="r", quote="q" * 30, source_title="S", citation="c"
    )
    with pytest.raises(CannotInsert, match="inline"):
        insert_into_facts("ordinances: [a, b]\n", "ordinances", as_yaml_entry(proposal, "mined-1"))


def test_an_accepted_rule_points_back_at_the_page_it_came_from():
    proposal = Proposal(
        kind="ordinance",
        topic="t",
        rule="r",
        quote="q" * 30,
        source_title="S",
        citation="c",
        source_url="https://www.gcrealtyinc.com/blog/rlto",
    )
    entry = yaml.safe_load(insert_into_facts("", "ordinances", as_yaml_entry(proposal, "mined-1")))[
        "ordinances"
    ][0]
    assert entry["url"] == "https://www.gcrealtyinc.com/blog/rlto"


# --- mining without spending anything -----------------------------------------------------


def test_a_sentence_that_states_a_rule_is_proposed_verbatim():
    from markai.facts_miner import sentences_worth_proposing

    text = (
        "We bought the place in 2015 and it was a grind. "
        "Heat must reach 68 degrees during the day under the Chicago ordinance. "
        "Anyway, the tenants were great."
    )
    found = sentences_worth_proposing(text)
    assert found == ["Heat must reach 68 degrees during the day under the Chicago ordinance."]


def test_a_number_in_one_sentence_and_a_rule_in_another_is_not_joined():
    from markai.facts_miner import sentences_worth_proposing

    text = "We paid $8,000 for the boiler. You must give the tenant notice."
    assert sentences_worth_proposing(text) == [], (
        "joining two sentences would invent a rule neither of them states"
    )


def test_the_free_miner_never_paraphrases():
    from markai.facts_miner import proposal_from_sentence, quote_is_real

    passage = "Under the RLTO the landlord must return the deposit within 45 days. Full stop."
    sentence = "Under the RLTO the landlord must return the deposit within 45 days."
    proposal = proposal_from_sentence(sentence, "A post␟https://x.test/1", passage)
    assert proposal.quote == sentence == proposal.rule
    assert quote_is_real(proposal.quote, passage) is True
    assert proposal.source_url == "https://x.test/1"


def test_the_free_miner_prices_a_range_and_names_a_jurisdiction():
    from markai.facts_miner import proposal_from_sentence

    sentence = "In Evanston a tuckpointing job must run $9,000 to $16,000 on a six flat."
    proposal = proposal_from_sentence(sentence, "A post␟", sentence)
    assert proposal.kind == "cost"
    assert (proposal.low, proposal.high) == (9000.0, 16000.0)
    assert proposal.jurisdiction == "Evanston"


def test_the_free_miner_names_no_jurisdiction_it_was_not_given():
    from markai.facts_miner import jurisdiction_from

    assert jurisdiction_from("The landlord must give 30 days notice.") == ""


def test_the_free_run_reads_the_whole_corpus_and_costs_nothing(mined_store):
    from markai.facts_miner import mine_locally

    report = mine_locally(mined_store)
    assert report.passages_read > 0
    assert report.cost_usd == 0.0
    assert report.dropped_unquoted == 0, "the quote is a slice of the passage by construction"
    assert all(p.quote in p.rule for p in report.proposals)


def test_a_second_free_run_does_not_read_the_same_passages(mined_store):
    from markai.facts_miner import mine_locally

    first = mine_locally(mined_store)
    again = mine_locally(mined_store, already_read=set(first.read_chunk_ids))
    assert again.passages_read == 0


# --- one fact said three ways -------------------------------------------------------------


def test_the_same_number_in_three_wordings_is_one_decision():
    """The first real run proposed "Average apartment rent" three times. It is one fact."""
    from markai.facts_miner import group_proposals

    waiting = [
        _raw(
            "Average apartment rent",
            "About $2,100 a month.",
            "The average apartment rent runs about $2,100 a month.",
            "Post A",
            kind="cost",
            low=2100,
            high=2100,
        ),
        _raw(
            "average apartment rent",
            "Around $2,100.",
            "Around $2,100 is what the average apartment goes for.",
            "Post B",
            kind="cost",
            low=2100,
            high=2100,
        ),
        _raw(
            "Average apartment rent",
            "$2,100 a month on average.",
            "You are looking at $2,100 a month on average in that stretch.",
            "Post C",
            kind="cost",
            low=2100,
            high=2100,
        ),
    ]
    groups = group_proposals(waiting)
    assert len(groups) == 1, "same subject, same number, three wordings"
    assert groups[0].support == 3
    assert sorted(groups[0].indices) == [0, 1, 2], "accepting it answers all three"


def test_the_same_subject_with_a_different_number_is_still_two_decisions():
    from markai.facts_miner import group_proposals

    waiting = [
        _raw("2-bed rent", "$2,100.", "A two bed runs $2,100.", "A", kind="cost", low=2100),
        _raw("2-bed rent", "$2,600.", "A two bed runs $2,600.", "B", kind="cost", low=2600),
    ]
    assert len(group_proposals(waiting)) == 2, "a merge that swallowed a number would be a lie"


def test_a_market_rent_is_labelled_so_it_can_be_left_out():
    from markai.facts_miner import group_proposals, is_market_rent

    assert is_market_rent(_raw("Average apartment rent", "r", "q", "A", kind="cost")) is True
    assert is_market_rent(_raw("Boiler replacement", "r", "q", "A", kind="cost")) is False
    assert is_market_rent(_raw("Rent limits", "r", "q", "A")) is False, "an ordinance is not a rent"

    (group,) = group_proposals([_raw("Condo rent range", "r", "q", "A", kind="cost", low=1800)])
    assert group.rent is True


def test_an_accepted_price_says_when_it_was_true():
    from markai.facts_miner import as_yaml_entry, insert_into_facts

    proposal = Proposal(
        kind="cost",
        topic="tuckpointing",
        rule="$9,000 to $16,000 on a six flat.",
        quote="Tuckpointing must run $9,000 to $16,000 on a six flat.",
        source_title="A post",
        source_date="2025-06-01",
        low=9000,
        high=16000,
    )
    entry = yaml.safe_load(insert_into_facts("", "costs", as_yaml_entry(proposal, "mined-1")))
    assert entry["costs"][0]["as_of"] == "2025-06-01", (
        "a price with no date is a number, not a fact about the market"
    )
    assert "page dated 2025-06-01" in entry["costs"][0]["notes"]


def test_the_label_carries_the_page_its_date_and_a_usable_link(mined_store):
    from markai.facts_miner import candidates_in, split_label

    found, _ = candidates_in(mined_store)
    title, url, _dated = split_label(found[0][1])
    assert title == "Post 0"
    # The fixture sets only a locator, which is how some ingesters leave a document; the
    # citation still needs somewhere to point.
    assert url == "https://example.com/0"
    assert split_label("Old style␟https://x.test/1") == ("Old style", "https://x.test/1", "")


# --- finding the ones that matter ---------------------------------------------------------


def test_a_rule_that_names_its_section_is_marked():
    from markai.facts_miner import cites_a_section

    assert cites_a_section(
        _raw("deposit interest", "Interest each year.", "Under RLTO 5-12-080 you must pay it.", "A")
    )
    assert cites_a_section(_raw("notice", "30 days.", "765 ILCS 705 requires 30 days notice.", "A"))
    assert not cites_a_section(
        _raw("railing", "42 inches.", "A porch railing must be 42 inches high.", "A")
    ), "a number is not a citation"


def test_a_subject_somebody_asked_about_comes_first():
    from markai.facts_miner import group_proposals

    waiting = [
        _raw("snow removal", "24 hours.", "Snow must be cleared within 24 hours.", "A"),
        _raw(
            "security deposit interest",
            "Interest is due each year.",
            "The landlord must pay interest on the deposit every 12 months.",
            "B",
        ),
    ]
    asked = ["how much interest do I owe on a security deposit"]
    groups = group_proposals(waiting, asked=asked)
    assert groups[0].lead["topic"] == "security deposit interest"
    assert groups[0].wanted is True
    assert groups[1].wanted is False, "one shared word is not a match"


def test_nothing_is_wanted_when_nobody_has_asked_anything():
    from markai.facts_miner import group_proposals

    groups = group_proposals([_raw("snow", "24 hours.", "Snow must go within 24 hours.", "A")])
    assert groups[0].wanted is False


# --- what is not about renting property at all --------------------------------------------


@pytest.mark.parametrize(
    "topic",
    [
        "Illinois budget adoption deadline",
        "Restricted cannabis zone petition deadline",
        "City vehicle sticker purchase deadline",
        "FOIA appeal deadline",
        "Animal adoption hold period",
        "Petition signature gathering period",
    ],
)
def test_a_city_page_deadline_is_off_topic(topic):
    """The corpus is full of these and no landlord will ever ask about one."""
    from markai.facts_miner import is_landlord_business

    assert is_landlord_business({"topic": topic}) is False


@pytest.mark.parametrize(
    "topic",
    ["Security deposit return deadline", "Voucher search period", "1099 deadline"],
)
def test_one_unambiguous_word_settles_it_on_the_label_alone(topic):
    from markai.facts_miner import is_landlord_business

    assert is_landlord_business({"topic": topic}) is True


@pytest.mark.parametrize(
    "topic,quote",
    [
        (
            "Damage statement deadline",
            "An itemized statement of damages must reach the tenant within 30 days.",
        ),
        (
            "Discrimination charge filing deadline",
            "A housing discrimination charge must be filed within 300 days.",
        ),
        (
            "Assessment complaint deadline",
            "A complaint on the assessed value of the property must be filed within 30 days.",
        ),
    ],
)
def test_a_bureaucratic_label_is_judged_on_its_sentence(topic, quote):
    """ "Damage", "discrimination" and "assessment" are in every government page.

    On their own they decide nothing; with the sentence they are plainly landlord business.
    """
    from markai.facts_miner import is_landlord_business

    assert is_landlord_business({"topic": topic}) is False
    assert is_landlord_business({"topic": topic, "quote": quote}) is True


def test_the_sentence_decides_when_the_label_is_bare():
    from markai.facts_miner import is_landlord_business

    assert is_landlord_business(
        {"topic": "Grace period", "quote": "Rent must be paid within a 5 day grace period."}
    )
    assert not is_landlord_business(
        {"topic": "Grace period", "quote": "A sticker ticket must be paid within 7 days."}
    )


def test_off_topic_goes_behind_everything():
    from markai.facts_miner import group_proposals

    waiting = [
        _raw(
            "cannabis petition comment period",
            "30 days.",
            "Comments must be filed in 30 days.",
            "A",
        ),
        _raw(
            "security deposit return", "45 days.", "The deposit must be returned in 45 days.", "B"
        ),
    ]
    groups = group_proposals(waiting)
    assert groups[0].on_topic is True
    assert groups[-1].on_topic is False


def test_one_shared_word_is_not_a_subject_you_already_cover():
    """Thirty entries marked two thirds of a thousand proposals as covered. One word is not."""
    from markai.facts_miner import group_proposals

    waiting = [
        _raw("radon testing deadline", "30 days.", "A radon test is required within 30 days.", "A")
    ]
    (loose,) = group_proposals(waiting, known_terms=frozenset({"deadline", "notice", "rent"}))
    assert loose.known is False, "sharing the word 'deadline' is not the same rule"

    (tight,) = group_proposals(waiting, known_terms=frozenset({"radon", "testing"}))
    assert tight.known is True
