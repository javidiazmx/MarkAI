"""The Notice Wizard: a guided lookup over facts.yaml, jurisdiction filtered first so a
Chicago-heavy corpus can never answer for somewhere else."""

from __future__ import annotations

from datetime import date

from markai.advisor.notice_wizard import JURISDICTIONS, REASONS, find_notice_rules
from markai.sources.facts import FactBook, Ordinance

TODAY = date(2026, 9, 9)


def _rule(id: str, jurisdiction: str, topic: str, rule: str, **kwargs) -> Ordinance:
    return Ordinance(
        id=id, jurisdiction=jurisdiction, topic=topic, rule=rule, citation="Test source", **kwargs
    )


def test_jurisdictions_and_reasons_are_the_expected_closed_lists():
    assert JURISDICTIONS == [
        "Chicago",
        "Suburban Cook County",
        "Evanston",
        "Illinois (no local ordinance)",
    ]
    assert set(REASONS) == {"nonpayment", "lease_violation", "no_cause_nonrenewal"}


def test_chicago_facts_never_answer_for_a_different_jurisdiction():
    """The whole reason this filters jurisdiction first: a Chicago-heavy corpus used to
    crowd out a correct suburban or Illinois-general match on keyword overlap alone."""
    book = FactBook(
        ordinances=[
            _rule(
                "chi-1",
                "Chicago",
                "Lease renewal notice",
                "60 days notice for non-renewal, 120 if three years or longer.",
            ),
            _rule(
                "il-1",
                "Illinois",
                "Notice to terminate month-to-month tenancy",
                "Month-to-month rental agreements require thirty days notice.",
            ),
        ]
    )
    results = find_notice_rules(
        book, "Illinois (no local ordinance)", "no_cause_nonrenewal", 5.0, TODAY
    )
    assert [o.id for o in results] == ["il-1"], "the Chicago rule must not appear here at all"


def test_suburban_cook_matches_cook_county_but_not_chicago():
    book = FactBook(
        ordinances=[
            _rule("chi-1", "Chicago", "Lease renewal notice", "Chicago's own rule."),
            _rule("cook-1", "Cook County", "Non-renewal notice", "60 or 120 days for the suburbs."),
            _rule(
                "cook-2",
                "Suburban Cook County, IL",
                "Non-renewal notice",
                "At least 60 days before the end of the lease.",
            ),
        ]
    )
    results = find_notice_rules(book, "Suburban Cook County", "no_cause_nonrenewal", 5.0, TODAY)
    ids = {o.id for o in results}
    assert ids == {"cook-1", "cook-2"}
    assert "chi-1" not in ids


def test_evanston_only_matches_evanston():
    book = FactBook(
        ordinances=[
            _rule("chi-1", "Chicago", "Lease renewal notice", "Chicago's own rule."),
            _rule(
                "ev-1",
                "Evanston, IL",
                "Non-renewal notice",
                "Landlords must give tenants 90 days notice of intent not to renew.",
            ),
        ]
    )
    results = find_notice_rules(book, "Evanston", "no_cause_nonrenewal", 1.0, TODAY)
    assert [o.id for o in results] == ["ev-1"]


def test_an_unmatched_jurisdiction_returns_nothing_not_someone_elses_rule():
    book = FactBook(
        ordinances=[_rule("chi-1", "Chicago", "Lease renewal notice", "Chicago's own rule.")]
    )
    assert find_notice_rules(book, "Evanston", "no_cause_nonrenewal", 1.0, TODAY) == []


def test_a_superseded_ordinance_is_left_out():
    book = FactBook(
        ordinances=[
            _rule(
                "old",
                "Chicago",
                "Old notice rule",
                "The old rule.",
                effective_from=date(2000, 1, 1),
                effective_to=date(2010, 1, 1),
            )
        ]
    )
    assert find_notice_rules(book, "Chicago", "no_cause_nonrenewal", 5.0, TODAY) == []


def test_when_nothing_mentions_the_reason_the_jurisdictions_other_facts_still_show():
    """Better to show what this jurisdiction does have than nothing at all."""
    book = FactBook(
        ordinances=[
            _rule("chi-1", "Chicago", "Something unrelated", "A fact about parking permits.")
        ]
    )
    results = find_notice_rules(book, "Chicago", "nonpayment", None, TODAY)
    assert [o.id for o in results] == ["chi-1"]


def test_results_are_capped_at_five():
    book = FactBook(
        ordinances=[
            _rule(f"chi-{i}", "Chicago", "Notice", "60 days notice for non-renewal.")
            for i in range(10)
        ]
    )
    assert len(find_notice_rules(book, "Chicago", "no_cause_nonrenewal", 5.0, TODAY)) == 5


def test_a_jurisdiction_label_that_names_chicago_while_meaning_the_opposite_is_not_chicago():
    """Real facts.yaml has labels like "Cook County (suburbs outside Chicago)" and "Chicago
    suburbs" - they contain the word "chicago" but describe the opposite of the city
    itself. Naive substring matching would let one answer for Chicago, which is the exact
    backwards mistake this module exists to prevent."""
    book = FactBook(
        ordinances=[
            _rule(
                "suburb-1",
                "Cook County (suburbs outside Chicago)",
                "Security deposit interest",
                "Buildings of 24 units or less in suburban Cook County are exempt.",
            ),
            _rule(
                "suburb-2",
                "Chicago suburbs",
                "Security deposit",
                "In Chicago suburbs the RLTO does not apply.",
            ),
            _rule(
                "il-outside",
                "Illinois (outside Chicago)",
                "Security deposit interest",
                "The Illinois Security Deposit Interest Act applies outside Chicago.",
            ),
        ]
    )
    assert find_notice_rules(book, "Chicago", "nonpayment", None, TODAY) == []
    cook_ids = {
        o.id for o in find_notice_rules(book, "Suburban Cook County", "nonpayment", None, TODAY)
    }
    assert cook_ids == {"suburb-1"}
    il_ids = {
        o.id
        for o in find_notice_rules(book, "Illinois (no local ordinance)", "nonpayment", None, TODAY)
    }
    assert il_ids == {"il-outside"}


def test_a_label_naming_chicago_and_cook_together_answers_for_suburban_cook_too():
    """ "Chicago/Cook County" or "Cook County (including Chicago)" states one rule for both
    places, not a Chicago-only rule that happens to mention the county - real facts.yaml
    entries like the illegal-lockout ban and the hand-delivery requirement are genuinely
    shared this way. But a Chicago-only label (no "cook" in it at all) - like the Fair
    Notice Ordinance's own tenure-escalated notice periods always are - must still never
    answer for Suburban Cook County."""
    book = FactBook(
        ordinances=[
            _rule(
                "shared-1",
                "Chicago/Cook County",
                "Tenant belongings and lockouts",
                "Landlords cannot remove a tenant's belongings or change the locks.",
            ),
            _rule(
                "chi-only",
                "Chicago",
                "Lease renewal notice",
                "60 days notice for non-renewal, 120 if three years or longer.",
            ),
        ]
    )
    cook_ids = {
        o.id
        for o in find_notice_rules(book, "Suburban Cook County", "no_cause_nonrenewal", 5.0, TODAY)
    }
    assert cook_ids == {"shared-1"}, "the joint label answers here too"
    assert "chi-only" not in cook_ids, "a Chicago-only label never does"


def test_tenure_is_ignored_for_reasons_that_have_no_tenure_tiers():
    """Tenure tiers only exist for a no-cause non-renewal notice. Passing a tenure_years
    for nonpayment or a lease violation must not let a stray tenure word (e.g. "three
    years or longer") outrank the fact that actually answers that reason."""
    book = FactBook(
        ordinances=[
            _rule(
                "nonpay",
                "Suburban Cook County",
                "Nonpayment",
                "Voiding a 5-day notice by accepting payment after it was served.",
            ),
            _rule(
                "renewal",
                "Suburban Cook County",
                "Non-renewal notice",
                "60 or 120 days notice depending on whether the tenant has lived there "
                "three years or longer.",
            ),
        ]
    )
    results = find_notice_rules(book, "Suburban Cook County", "nonpayment", 5.0, TODAY)
    assert results[0].id == "nonpay"
