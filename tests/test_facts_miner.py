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
