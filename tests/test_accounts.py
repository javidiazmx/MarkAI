"""Two questions free, then the form: the counting, the validation, and the gate."""

from __future__ import annotations

import pytest

from markai.web.accounts import Accounts, SignupError, parse

FORM = {
    "name": "Javier Diaz",
    "email": "javier@example.com",
    "phone": "(312) 555-0134",
    "neighborhood": "Logan Square",
}


@pytest.fixture
def accounts(tmp_path) -> Accounts:
    store = Accounts(tmp_path / "accounts.db", free_questions=2)
    yield store
    store.close()


# --- the form ---------------------------------------------------------------------------


def test_every_field_is_kept():
    account = parse(FORM)
    assert account.name == "Javier Diaz"
    assert account.email == "javier@example.com"
    assert account.phone == "(312) 555-0134"
    assert account.neighborhood == "Logan Square"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "", "your name"),
        ("name", "J", "your name"),
        ("email", "javier", "email address"),
        ("email", "javier@", "email address"),
        ("email", "javier@example", "email address"),
        ("phone", "555", "area code"),
        ("phone", "", "area code"),
        ("neighborhood", "", "neighborhood"),
    ],
)
def test_a_field_that_cannot_be_used_says_which_one(field, value, message):
    with pytest.raises(SignupError, match=message):
        parse({**FORM, field: value})


def test_a_phone_written_any_way_is_accepted():
    for written in ("312-555-0134", "+1 312 555 0134", "3125550134", "(312) 555 0134 x22"):
        assert parse({**FORM, "phone": written}).phone


def test_newlines_pasted_into_a_field_are_flattened():
    assert parse({**FORM, "name": "Javier\n\nDiaz"}).name == "Javier Diaz"


# --- the count --------------------------------------------------------------------------


def test_two_questions_are_free_then_the_form(accounts):
    assert accounts.needs_signup("b1") is False
    assert accounts.free_left("b1") == 2

    accounts.count_question("b1")
    assert accounts.free_left("b1") == 1
    assert accounts.needs_signup("b1") is False

    accounts.count_question("b1")
    assert accounts.free_left("b1") == 0
    assert accounts.needs_signup("b1") is True


def test_signing_up_lifts_the_gate(accounts):
    accounts.count_question("b1")
    accounts.count_question("b1")
    assert accounts.needs_signup("b1") is True

    accounts.create("b1", FORM)
    assert accounts.needs_signup("b1") is False
    assert accounts.get("b1").neighborhood == "Logan Square"


def test_one_browser_does_not_spend_anothers_questions(accounts):
    accounts.count_question("b1")
    accounts.count_question("b1")
    assert accounts.needs_signup("b1") is True
    assert accounts.needs_signup("b2") is False
    assert accounts.get("b2") is None


def test_a_browser_with_no_id_is_never_counted_or_gated(accounts):
    assert accounts.count_question("") == 0
    assert accounts.questions_used("") == 0
    assert accounts.needs_signup("") is False
    with pytest.raises(SignupError, match="nowhere to save"):
        accounts.create("", FORM)


def test_no_free_questions_means_the_form_comes_first(tmp_path):
    store = Accounts(tmp_path / "accounts.db", free_questions=0)
    assert store.needs_signup("b1") is True
    store.close()


def test_signing_up_twice_updates_rather_than_duplicates(accounts):
    accounts.create("b1", FORM)
    accounts.create("b1", {**FORM, "neighborhood": "Berwyn"})
    assert accounts.get("b1").neighborhood == "Berwyn"


def test_the_fields_never_reach_the_log(accounts, caplog):
    with caplog.at_level("DEBUG"):
        accounts.create("b1", FORM)
    assert "javier@example.com" not in caplog.text
    assert "555-0134" not in caplog.text
    assert "Javier" not in caplog.text
    assert "account created" in caplog.text


def test_a_broken_counter_does_not_raise(accounts, caplog):
    accounts.close()
    with caplog.at_level("WARNING"):
        assert accounts.count_question("b1") == 0
    assert "could not count a question" in caplog.text
