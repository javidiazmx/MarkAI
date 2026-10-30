"""`mark export`: what goes to another chatbot, and what never does."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from markai.advisor.guardrails import LEGAL_DISCLAIMER
from markai.cli import app
from markai.sources.export import (
    POLICY_LABEL,
    canonical_jurisdiction,
    context_pack,
    instructions,
    split_note,
)
from markai.sources.facts import FactBook
from markai.sources.manifest import SourceManifest

runner = CliRunner()
TODAY = date(2026, 10, 30)


def _rule(**over):
    base = {
        "id": "mined-1",
        "jurisdiction": "Chicago",
        "topic": "Late fees",
        "rule": "Ten dollars on the first five hundred.",
        "citation": "CRLTO 5-12-140(h)",
        "url": "https://www.gcrealtyinc.com/blog/late-fees",
        "notes": "From: $10 on the first $500 of monthly rent.",
    }
    return {**base, **over}


def _book(*rules, costs=()):
    return FactBook.model_validate(
        {"ordinances": [_rule(**r) for r in rules], "costs": list(costs)}
    )


def _pack(*rules, costs=(), manifest=None):
    body, held = context_pack(
        _book(*rules, costs=costs), manifest or SourceManifest(), TODAY, include_costs=True
    )
    return body, held


def _seed_site(data):
    """One page of GC's own site, so the background half has something to write."""
    from markai.knowledge.store import KnowledgeStore
    from markai.models import Document, SourceKind

    url = "https://www.gcrealtyinc.com/services"
    doc = Document(
        id=Document.make_id(SourceKind.WEBSITE, url),
        kind=SourceKind.WEBSITE,
        title="Our services",
        locator=url,
        text="We manage residential rentals across Chicagoland.",
        link=url,
    )
    doc.ensure_hash()
    store = KnowledgeStore(data / "markai.db")
    store.upsert_document(doc, [])
    store.close()


# --- jurisdiction, which is the whole reason this is dangerous -----------------------


def test_an_evanston_rule_is_never_filed_under_chicago():
    """The failure this pack exists to prevent: one town answering for another."""
    body, _ = _pack(
        {"id": "mined-1", "jurisdiction": "Chicago", "topic": "Late fees"},
        {"id": "mined-2", "jurisdiction": "Evanston, IL", "topic": "Non-renewal notice"},
    )
    chicago = body.index("## Chicago")
    evanston = body.index("## Evanston")
    assert body.index("`mined-2`") > evanston
    assert not (chicago < body.index("`mined-2`") < evanston)


def test_one_town_spelled_two_ways_gets_one_heading():
    """Berkeley and "Berkeley, IL" are not two places whose rules might differ."""
    body, _ = _pack(
        {"id": "mined-1", "jurisdiction": "Berkeley"},
        {"id": "mined-2", "jurisdiction": "Berkeley, IL"},
    )
    assert body.count("## Berkeley") == 1
    assert "## Berkeley, IL" not in body


@pytest.mark.parametrize(
    ("raw", "heading"),
    [
        ("Federal (Fair Housing)", "Applies anywhere (federal)"),
        ("United States", "Applies anywhere (federal)"),
        ("Cook County, IL", "Cook County (suburban / unincorporated)"),
        ("Suburban Cook County", "Cook County (suburban / unincorporated)"),
        ("Mt. Prospect", "Mount Prospect"),
        ("Schaumburg, IL", "Schaumburg"),
        ("", "Jurisdiction not stated in the source"),
    ],
)
def test_jurisdiction_labels_collapse_only_onto_the_same_place(raw, heading):
    assert canonical_jurisdiction(raw)[1] == heading


def test_an_unknown_town_keeps_its_own_name_rather_than_joining_a_county():
    """Widening a municipal rule into a county one is the same mistake, pointed outward."""
    assert canonical_jurisdiction("Pingree Grove")[1] == "Pingree Grove"
    assert canonical_jurisdiction("Glen Ellyn, IL")[1] == "Glen Ellyn"


def test_the_baselines_sort_before_the_places():
    body, _ = _pack(
        {"id": "mined-1", "jurisdiction": "Schaumburg"},
        {"id": "mined-2", "jurisdiction": "Illinois"},
        {"id": "mined-3", "jurisdiction": "Federal"},
    )
    assert (
        body.index("## Applies anywhere (federal)")
        < body.index("## Illinois (statewide)")
        < body.index("## Schaumburg")
    )


# --- what is held back, and what is merely relabelled --------------------------------


def test_screening_thresholds_are_withheld_and_the_reason_is_said_out_loud():
    """One company's criteria restated by a stranger as the standard is a fair-housing risk."""
    body, held = _pack(
        {"id": "mined-459", "rule": "Net income must equal 4x monthly rent."},
        {"id": "mined-1", "rule": "Ten dollars on the first five hundred."},
    )
    assert "mined-459" not in body
    assert "4x monthly rent" not in body
    assert any("mined-459" in line and "screening" in line for line in held)
    assert "`mined-1`" in body, "the rest of the pack is unaffected"


def test_a_house_rule_is_labelled_not_dropped():
    body, held = _pack(
        {
            "id": "mined-232",
            "jurisdiction": "Chicago",
            "topic": "Guest occupancy limit",
            "url": "https://www.gcrealtyinc.com/resident-handbook",
        }
    )
    assert held == []
    assert POLICY_LABEL in body
    assert f"## {POLICY_LABEL}" in body, "and it gets its own section, away from the ordinances"


def test_a_superseded_rule_never_reaches_the_pack():
    body, _ = _pack(
        {"id": "mined-1", "effective_to": date(2020, 1, 1)},
        {"id": "mined-2"},
    )
    assert "`mined-1`" not in body and "`mined-2`" in body


# --- the quote, which is what makes a claim checkable --------------------------------


def test_the_owners_commentary_is_never_printed_as_a_quoted_source():
    """A note the owner typed, presented as the sentence the rule came from, is a forgery."""
    quote, note = split_note(
        "Added by hand, not mined: the Wizard had no Evanston fact. "
        'From the city page: "Serve a 30-day written notice."'
    )
    assert quote == ""
    assert note.startswith("Added by hand")


def test_a_correction_before_the_marker_stays_a_note_and_the_quote_stays_a_quote():
    quote, note = split_note("Corrected: re-pointed to a GC source. From: $10 on the first $500.")
    assert quote == "$10 on the first $500."
    assert note == "Corrected: re-pointed to a GC source."


def test_a_quote_is_reflowed_but_never_truncated():
    long_quote = "word " * 300
    body, _ = _pack({"id": "mined-1", "notes": f"From: {long_quote}"})
    assert "…" not in body and "..." not in body
    assert body.count("word word word") > 1


# --- money, where the wrong symbol is a wrong answer ---------------------------------


def test_a_unit_that_is_not_money_carries_no_dollar_sign():
    body, _ = _pack(
        {"id": "mined-1"},
        costs=[
            {"id": "mined-88", "item": "Days on market", "low": 17, "high": 20, "unit": "days"},
            {"id": "mined-99", "item": "Filing fee", "low": 287, "high": 500, "unit": "USD"},
        ],
    )
    assert "17 to 20 days" in body
    assert "$17" not in body
    assert "$287 to $500" in body


# --- nothing private -----------------------------------------------------------------


def test_the_pack_carries_no_settings_no_key_and_nobody_s_question(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("business:\n  name: GC Realty\nwebsites: []\n", encoding="utf-8")
    (tmp_path / "facts.yaml").write_text(
        "ordinances:\n"
        "  - id: mined-1\n"
        "    jurisdiction: Chicago\n"
        "    topic: Late fees\n"
        "    rule: Ten dollars on the first five hundred.\n"
        "    citation: CRLTO 5-12-140(h)\n",
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")

    from markai.knowledge.store import KnowledgeStore

    store = KnowledgeStore(data / "markai.db")
    store.log_question("s1", "how long do I have to return a deposit", "none", [], True, {})
    store.close()
    _seed_site(data)

    result = runner.invoke(app, ["export", "--facts"])
    assert result.exit_code == 0, result.stdout

    pack = (data / "gc-context-pack.md").read_text(encoding="utf-8")
    rules = (data / "gc-context-instructions.md").read_text(encoding="utf-8")
    for body in (pack, rules):
        assert "sk-ant" not in body and "secret-value" not in body
        assert "how long do I have to return" not in body
        assert "MARKAI_" not in body and str(data) not in body
    assert "`mined-1`" in pack


def test_the_files_round_trip_as_utf8(tmp_path, monkeypatch):
    """Written with the locale codec, the em dashes in here kill the command on Windows."""
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("business:\n  name: GC Realty\nwebsites: []\n", encoding="utf-8")
    (tmp_path / "facts.yaml").write_text(
        "ordinances:\n"
        "  - id: mined-1\n"
        "    jurisdiction: Evanston\n"
        "    topic: Late fees\n"
        '    rule: "Capped at $25 — see § 5-3-3(A)(8)."\n'
        "    citation: Evanston RLTO\n",
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data))

    _seed_site(data)
    assert runner.invoke(app, ["export", "--facts"]).exit_code == 0
    body = (data / "gc-context-pack.md").read_text(encoding="utf-8")
    assert "—" in body and "§" in body


# --- the instructions half -----------------------------------------------------------


def test_the_instructions_fit_the_box_even_fully_loaded():
    """The bare block proves the floor. A real manifest fills never_say, so test that."""
    from markai.sources.export import INSTRUCTIONS_LIMIT

    loaded = SourceManifest.model_validate(
        {
            "business": {
                "name": "GC Realty & Development",
                "never_say": [f"Never say thing number {n}." for n in range(10)],
                "extra_instructions": "Mention the seven counties. " * 10,
            }
        }
    )
    for manifest in (SourceManifest(), loaded):
        text = instructions(manifest, kept=443, costs=101, today=TODAY, pack_name="pack.md")
        assert len(text) < INSTRUCTIONS_LIMIT


def test_the_instructions_ask_for_the_disclaimer_rather_than_forbidding_it():
    """Jay is told not to write it because code appends it. Nothing appends it over there."""
    text = instructions(SourceManifest(), kept=1, costs=0, today=TODAY, pack_name="pack.md")
    assert LEGAL_DISCLAIMER in text, "reproduced word for word, never paraphrased"
    assert "word for word" in text.lower()


def test_the_instructions_tell_it_to_say_when_it_does_not_know():
    """The one rule that must be inverted: Jay never narrates a gap, this must."""
    text = instructions(SourceManifest(), kept=1, costs=0, today=TODAY, pack_name="pack.md")
    assert "say so" in text.lower()
    assert "not jay" in text.lower()


def test_never_say_reaches_the_instructions_when_the_owner_has_filled_it():
    manifest = SourceManifest.model_validate(
        {"business": {"name": "GC Realty", "never_say": ["Never quote a management fee."]}}
    )
    text = instructions(manifest, kept=1, costs=0, today=TODAY, pack_name="pack.md")
    assert "Never quote a management fee." in text


def test_an_empty_never_say_is_flagged_rather_than_passed_over(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("business:\n  name: GC Realty\nwebsites: []\n", encoding="utf-8")
    (tmp_path / "facts.yaml").write_text(
        "ordinances:\n"
        "  - id: mined-1\n"
        "    jurisdiction: Chicago\n"
        "    topic: Late fees\n"
        "    rule: Ten dollars.\n"
        "    citation: CRLTO\n",
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data))

    _seed_site(data)
    result = runner.invoke(app, ["export", "--facts"])
    assert result.exit_code == 0
    assert "never_say" in result.stdout


def test_an_empty_fact_book_writes_no_pack_and_says_so(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data))

    _seed_site(data)
    result = runner.invoke(app, ["export", "--facts"])
    assert result.exit_code == 0, "the background half still has something to say"
    assert not (data / "gc-context-pack.md").exists()
    assert "No facts to export" in result.stdout


def test_every_rule_that_should_be_in_the_pack_is_in_it_exactly_once():
    """A pack that silently drops an entry is worse than one that refuses to build."""
    import re

    rules = [
        {"id": f"mined-{n}", "jurisdiction": j}
        for n, j in enumerate(
            ["Chicago", "Evanston", "Illinois", "Federal", "Schaumburg", "Cook County"], start=1
        )
    ]
    rules.append({"id": "mined-459", "rule": "Net income must equal 4x monthly rent."})
    costs = [{"id": "mined-900", "item": "Filing fee", "low": 287, "high": 500, "unit": "USD"}]

    body, held = _pack(*rules, costs=costs)
    found = re.findall(r"`(mined-\d+)`", body)

    assert sorted(found) == sorted(set(found)), "no entry is rendered twice"
    assert set(found) == {f"mined-{n}" for n in range(1, 7)} | {"mined-900"}
    assert len(held) == 1


# --- the guards the footer promises ---------------------------------------------------


def test_a_vendor_quote_pasted_into_a_cost_entry_stops_the_export():
    """`CostRange.source` is exactly where a phone quote lands. One paste, a named person."""
    from markai.sources.export import personal_details

    book = FactBook.model_validate(
        {
            "ordinances": [_rule()],
            "costs": [
                {
                    "id": "mined-900",
                    "item": "Boiler swap",
                    "low": 9000,
                    "high": 12000,
                    "unit": "USD",
                    "source": "quote from Hernandez HVAC, (312) 555-0199, h@example.com",
                }
            ],
        }
    )
    blocking, _ = personal_details(book, ("gcrealtyinc.com",))
    assert any("phone number" in line and "mined-900" in line for line in blocking)
    assert any("email address" in line for line in blocking)


def test_the_owners_own_domain_is_not_a_leak():
    from markai.sources.export import personal_details

    book = FactBook.model_validate(
        {"ordinances": [_rule(citation="Ask mark@gcrealtyinc.com for the current rate card")]}
    )
    assert personal_details(book, ("gcrealtyinc.com",))[0] == []
    assert personal_details(book, ())[0] != [], "and it is a leak when it is not their domain"


def test_a_street_address_is_reported_not_blocked():
    """GC Realty's own address is quoted inside a real entry. Blocking on it blocks them."""
    from markai.sources.export import personal_details

    book = FactBook.model_validate(
        {"ordinances": [_rule(notes="From: name GC Realty at 219 E Irving Park Rd as insured.")]}
    )
    blocking, addresses = personal_details(book, ())
    assert blocking == []
    assert any("219 E Irving Park Rd" in line for line in addresses)


def test_the_owners_commentary_stays_out_unless_asked_for():
    """It is where they record that one of their own posts has a figure backwards."""
    rule = _rule(notes="Corrected: our own post has this backwards. From: $10 on the first $500.")
    off, _ = _pack(rule)
    assert "NOTE FROM THE OWNER" not in off
    assert "has this backwards" not in off
    assert "$10 on the first $500." in off, "the quoted half still ships"

    on, _ = context_pack(_book(rule), SourceManifest(), TODAY, owner_notes=True)
    assert "has this backwards" in on


def test_an_id_cited_but_not_defined_is_caught():
    """The withhold gate is id-level; free text routes straight around it."""
    from markai.sources.export import orphan_ids

    body, _ = context_pack(
        _book(_rule(notes="Corrected: see mined-337, which has it right.")),
        SourceManifest(),
        TODAY,
        owner_notes=True,
    )
    assert "mined-337" in orphan_ids(body)
    assert orphan_ids(_pack(_rule())[0]) == []


# --- the widening the first version shipped -------------------------------------------


def _real_labels():
    import yaml

    raw = yaml.safe_load(open("sources/facts.yaml", encoding="utf-8"))
    return sorted({(o.get("jurisdiction") or "") for o in raw.get("ordinances") or []})


def test_no_real_label_is_ever_filed_under_a_place_it_excludes():
    """Data-driven over the live file, because a hand-picked list is what missed this.

    The first version matched `"chicago" in label`, so "Chicago suburbs", "Cook County
    (suburbs outside Chicago)" and "Illinois (outside Chicago)" all printed under
    `## Chicago` - the Evanston mistake pointed inward, and `notice_wizard._excludes_chicago`
    already existed to prevent it.
    """
    from markai.sources.export import headings_for

    for label in _real_labels():
        low = label.lower()
        headings = [heading for _, heading in headings_for(label)]
        if "outside" in low or "suburb" in low or "chicagoland" in low or "chicago area" in low:
            assert "Chicago" not in headings, f"{label!r} filed under Chicago"
        if "outside" in low:
            assert "Illinois (statewide)" not in headings, f"{label!r} filed as statewide"


def test_a_label_naming_two_places_is_filed_under_both():
    from markai.sources.export import headings_for

    assert [h for _, h in headings_for("Chicago / Cook County, IL")] == [
        "Chicago",
        "Cook County (suburban / unincorporated)",
    ]
    assert [h for _, h in headings_for("Oak Park, Evanston, Mount Prospect")] == [
        "Evanston",
        "Oak Park",
        "Mount Prospect",
    ]


def test_a_rule_naming_two_places_prints_under_each():
    body, _ = _pack({"id": "mined-1", "jurisdiction": "Cook County (including Chicago)"})
    assert body.count("`mined-1`") == 2
    assert "## Chicago" in body and "## Cook County (suburban / unincorporated)" in body


def test_a_town_is_not_matched_on_a_fragment_of_its_name():
    """ "Schaumburg" contains "cha" and "Wilmette" contains "il"."""
    from markai.sources.export import headings_for

    assert [h for _, h in headings_for("Schaumburg, IL")] == ["Schaumburg"]
    assert [h for _, h in headings_for("Wilmette")] == ["Wilmette"]


def test_a_voucher_rule_does_not_claim_to_apply_everywhere():
    from markai.sources.export import headings_for

    assert [h for _, h in headings_for("Section 8 / HUD")] == [
        "Federal, but only for voucher and subsidized tenancies"
    ]
    assert [h for _, h in headings_for("Federal (HUD/FHA)")] == [
        "Federal, but only for voucher and subsidized tenancies"
    ]
    assert [h for _, h in headings_for("Federal (Fair Housing)")] == ["Applies anywhere (federal)"]


def test_the_stacking_line_names_the_sections_that_exist():
    with_baselines, _ = _pack(
        {"id": "mined-1", "jurisdiction": "Illinois"},
        {"id": "mined-2", "jurisdiction": "Evanston"},
    )
    assert "*Illinois (statewide)* stack" in with_baselines

    without, _ = _pack({"id": "mined-1", "jurisdiction": "Evanston"})
    assert "No section here applies everywhere" in without
    assert "apply everywhere" not in without.replace("No section here applies everywhere", "")


def test_a_pasted_email_header_never_becomes_the_quoted_source():
    """ "From: " is the RFC 5322 header every forwarded thread opens with."""
    quote, note = split_note(
        "Spoke to the owner on N Kedzie. Forwarded thread From: maria@example.com, "
        "she wants the deposit back."
    )
    assert quote == ""
    assert note.startswith("Spoke to the owner")


def test_the_policy_marker_is_the_same_string_in_both_files():
    """The one literal the two files must agree on is the marker the bot looks for."""
    rules = instructions(SourceManifest(), kept=1, costs=0, today=TODAY, pack_name="p.md")
    body, _ = _pack(
        {"id": "mined-1", "url": "https://www.gcrealtyinc.com/resident-handbook"},
    )
    assert POLICY_LABEL in rules and POLICY_LABEL in body


def test_the_refusal_text_is_handed_over_byte_exact():
    """Owner-approved wording, reproduced under an instruction to repeat it word for word."""
    from markai.advisor.guardrails import HIGH_RISK_RESPONSE

    rules = instructions(SourceManifest(), kept=1, costs=0, today=TODAY, pack_name="p.md")
    assert HIGH_RISK_RESPONSE in rules
