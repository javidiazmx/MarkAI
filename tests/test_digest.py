"""The monthly digest: built from the ledger and portfolio, nothing invented."""

from __future__ import annotations

from datetime import date

import pytest

from markai.web.digest import build_digest, digest_email
from markai.web.ledger import Ledger
from markai.web.portfolio import Portfolio

OWNER = "account:a1"
SINCE = date(2026, 8, 15)


class FakeAccount:
    def __init__(self, name: str, email: str) -> None:
        self.name = name
        self.email = email
        self.owner_id = OWNER


@pytest.fixture
def ledger(tmp_path):
    store = Ledger(tmp_path / "ledger.db")
    yield store
    store.close_db()


@pytest.fixture
def portfolio(tmp_path):
    store = Portfolio(tmp_path / "portfolio.db")
    yield store
    store.close()


def test_a_quiet_month_has_nothing_to_say(ledger, portfolio):
    digest = build_digest(
        FakeAccount("Javier Diaz", "javier@example.com"), ledger, portfolio, SINCE
    )
    assert digest.has_anything_to_say is False
    assert digest.money_in == 0.0
    assert digest.open_items == []


def test_income_and_expenses_roll_up_into_the_digest(ledger, portfolio):
    ledger.add(OWNER, {"kind": "income", "what": "September rent", "amount": 4200})
    ledger.add(OWNER, {"kind": "expense", "what": "New boiler", "amount": 800})
    ledger.add(OWNER, {"kind": "bill", "what": "Water bill", "amount": 200})

    digest = build_digest(
        FakeAccount("Javier Diaz", "javier@example.com"), ledger, portfolio, SINCE
    )
    assert digest.money_in == 4200.0
    assert digest.money_out == 1000.0
    assert digest.net == 3200.0
    assert digest.has_anything_to_say is True


def test_open_items_are_included_even_with_no_money_moved(ledger, portfolio):
    ledger.add(OWNER, {"kind": "maintenance", "what": "No heat in unit 2"})

    digest = build_digest(
        FakeAccount("Javier Diaz", "javier@example.com"), ledger, portfolio, SINCE
    )
    assert digest.open_items == ["No heat in unit 2"]
    assert digest.has_anything_to_say is True


def test_property_count_reflects_the_portfolio(ledger, portfolio):
    portfolio.add(OWNER, {"label": "2145 W Division"})
    portfolio.add(OWNER, {"label": "Berwyn six flat"})

    digest = build_digest(
        FakeAccount("Javier Diaz", "javier@example.com"), ledger, portfolio, SINCE
    )
    assert digest.property_count == 2


def test_the_digest_email_is_short_and_names_the_numbers(ledger, portfolio):
    ledger.add(OWNER, {"kind": "income", "what": "September rent", "amount": 4200})
    ledger.add(OWNER, {"kind": "maintenance", "what": "No heat in unit 2"})

    digest = build_digest(
        FakeAccount("Javier Diaz", "javier@example.com"), ledger, portfolio, SINCE
    )
    subject, body = digest_email(digest)
    assert "Javier" in body
    assert "4,200.00" in body
    assert "No heat in unit 2" in body
    assert "$" in subject


def test_the_digest_email_survives_a_blank_name(ledger, portfolio):
    digest = build_digest(FakeAccount("", "javier@example.com"), ledger, portfolio, SINCE)
    ledger.add(OWNER, {"kind": "income", "what": "Rent", "amount": 100})
    digest = build_digest(FakeAccount("", "javier@example.com"), ledger, portfolio, SINCE)
    _, body = digest_email(digest)
    assert body.startswith("Hi there,")
