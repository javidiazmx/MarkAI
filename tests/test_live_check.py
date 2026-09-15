"""Checking a rate/fee figure against its registered government source - offline, respx-
mocked, no real network in this suite (the government pages here are fakes)."""

from __future__ import annotations

from datetime import date

import httpx
import respx

from markai.ingest.websites import make_client
from markai.sources.facts import Ordinance
from markai.sources.live_check import (
    AUTHORITATIVE_SOURCES,
    check_live,
    wizard_jurisdiction_for,
)

COOK_URL = AUTHORITATIVE_SOURCES["Suburban Cook County"]


def _rule(jurisdiction: str, rule: str) -> Ordinance:
    return Ordinance(
        id="x",
        jurisdiction=jurisdiction,
        topic="Late fees",
        rule=rule,
        citation="Test source",
        effective_from=date(2021, 1, 1),
    )


def test_wizard_jurisdiction_for_reuses_the_notice_wizards_own_matching():
    assert wizard_jurisdiction_for("Cook County, IL") == "Suburban Cook County"
    assert wizard_jurisdiction_for("Chicago suburbs") is None, (
        "a label that means the opposite of Chicago must not resolve to it here either"
    )
    assert wizard_jurisdiction_for("Some Other State") is None


def test_a_jurisdiction_with_no_registered_source_is_reported_as_such():
    ordinance = _rule("Some Other State", "The fee is $10.")
    with make_client() as client:
        result = check_live(ordinance, client)
    assert result.status == "no_source"
    assert result.source_url is None


def test_a_rule_with_no_dollar_or_percent_figure_has_nothing_to_check():
    ordinance = _rule("Cook County, IL", "Notice must be hand delivered or mailed.")
    with make_client() as client:
        result = check_live(ordinance, client)
    assert result.status == "no_source"
    assert result.source_url == COOK_URL


@respx.mock(assert_all_called=False)
def test_every_figure_found_on_the_page_is_confirmed(respx_mock):
    respx_mock.get(COOK_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><p>The late fee is $10 if the rent is $1000 or less, plus "
            "5% of any amount over $1000.</p></body></html>",
        )
    )
    ordinance = _rule(
        "Cook County, IL", "Late fees cannot exceed $10 for the first $1,000 of rent plus 5%."
    )
    with make_client() as client:
        result = check_live(ordinance, client)
    assert result.status == "confirmed"
    assert result.source_url == COOK_URL


@respx.mock(assert_all_called=False)
def test_a_figure_missing_from_the_page_is_reported_as_not_found_not_wrong(respx_mock):
    respx_mock.get(COOK_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><p>Landlords must maintain the premises.</p></body></html>",
        )
    )
    ordinance = _rule("Cook County, IL", "Late fees cannot exceed $10 plus 5%.")
    with make_client() as client:
        result = check_live(ordinance, client)
    assert result.status == "not_found"
    assert "not proof" in result.detail


@respx.mock(assert_all_called=False)
def test_a_fetch_failure_is_reported_not_raised(respx_mock):
    respx_mock.get(COOK_URL).mock(return_value=httpx.Response(503))
    ordinance = _rule("Cook County, IL", "Late fees cannot exceed $10 plus 5%.")
    with make_client() as client:
        result = check_live(ordinance, client)
    assert result.status == "fetch_failed"
    assert result.source_url == COOK_URL
