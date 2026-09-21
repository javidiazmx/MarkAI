"""The lead form, the remembered device, and the two free questions before it."""

from __future__ import annotations

import time

import pytest

from markai.web.accounts import (
    Accounts,
    SignupError,
    anonymous_owner,
    ensure_domain_is_reachable,
    parse,
)

FORM = {
    "name": "Javier Diaz",
    "email": "Javier@Example.com",
    "phone": "(312) 555-0134",
    "neighborhood": "Logan Square",
}


@pytest.fixture
def accounts(tmp_path) -> Accounts:
    store = Accounts(tmp_path / "accounts.db", free_questions=2)
    yield store
    store.close()


# --- the form ---------------------------------------------------------------------------


def test_every_field_is_kept_and_the_email_is_normalized():
    account = parse(FORM)
    assert account.name == "Javier Diaz"
    assert account.email == "javier@example.com", "so the same address is the same address"
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
        ("email", "javier@mailinator.com", "reach you at"),
        ("email", "javier@guerrillamail.com", "reach you at"),
        ("phone", "555", "phone number"),
        ("phone", "", "phone number"),
        ("phone", "1234567890", "phone number"),  # area code can't start with 1
        ("phone", "0123456789", "phone number"),  # area code can't start with 0
        ("phone", "5551234567", "phone number"),  # 555 is reserved, never a real area code
        ("phone", "1111111111", "phone number"),  # every fake-number generator's filler
        ("phone", "2222222222", "phone number"),  # NANP-valid shape, still all one digit
    ],
)
def test_a_field_that_cannot_be_used_says_which_one(field, value, message):
    with pytest.raises(SignupError, match=message):
        parse({**FORM, field: value})


def test_a_real_shaped_area_code_with_a_555_exchange_is_still_accepted():
    """555 the area code is reserved fiction; 555 as the *exchange* (the middle three of a
    real area code) is just an ordinary number - "312-555-0134" is GC Realty's own test
    fixture and must keep working."""
    assert parse({**FORM, "phone": "312-555-0134"}).phone


async def test_an_email_domain_that_cannot_receive_mail_is_rejected(monkeypatch):
    from markai.web import accounts

    async def unreachable(domain: str) -> bool:
        return False

    monkeypatch.setattr(accounts, "_domain_can_receive_mail", unreachable)
    with pytest.raises(SignupError, match="typo"):
        await ensure_domain_is_reachable("javier@thisdomaindoesnotexist.invalid")


async def test_a_reachable_domain_passes():
    # The autouse "offline" fixture stubs _domain_can_receive_mail to True, so this only
    # needs to confirm the happy path raises nothing - not a real DNS query.
    await ensure_domain_is_reachable("javier@example.com")


@pytest.mark.parametrize(
    "email",
    [
        "javier@abc.mailinator.com",
        "javier@sub.deeper.mailinator.com",
        "javier@sub.guerrillamail.com",
    ],
)
def test_a_subdomain_of_a_disposable_provider_is_still_rejected(email):
    """A live finding: the blocklist used to be an exact match, so "abc.mailinator.com"
    sailed through untouched - and since these services wildcard MX for every subdomain,
    the reachability check didn't catch it either. The domain check must cover the whole
    family, not just the bare domain."""
    with pytest.raises(SignupError, match="reach you at"):
        parse({**FORM, "email": email})


def test_a_domain_that_merely_ends_with_a_disposable_name_is_not_rejected():
    """"notmailinator.com" is not a subdomain of "mailinator.com" - a suffix check done by
    string-slicing rather than dot-boundary would wrongly conflate the two."""
    assert parse({**FORM, "email": "javier@notmailinator.com"}).email


def test_the_neighborhood_is_wanted_but_not_demanded():
    """It sharpens an answer. Refusing the whole form over it would cost a lead."""
    assert parse({**FORM, "neighborhood": ""}).neighborhood == ""


def test_a_phone_written_any_way_is_accepted():
    for written in ("312-555-0134", "+1 312 555 0134", "3125550134", "(312) 555 0134 x22"):
        assert parse({**FORM, "phone": written}).phone


def test_newlines_pasted_into_a_field_are_flattened():
    assert parse({**FORM, "name": "Javier\n\nDiaz"}).name == "Javier Diaz"


# --- the remembered device ----------------------------------------------------------------


def test_filling_the_form_remembers_this_device(accounts):
    account, token = accounts.create(FORM)
    assert accounts.account_for_token(token).email == "javier@example.com"
    assert accounts.needs_signup(account.owner_id) is False, "no wall on this device"


def test_get_finds_an_account_by_its_bare_id(accounts):
    account, _ = accounts.create(FORM)
    assert accounts.get(account.id).email == "javier@example.com"


def test_get_with_an_unknown_or_empty_id_is_none(accounts):
    assert accounts.get("nope") is None
    assert accounts.get("") is None


def test_the_stored_session_is_not_the_token(accounts, tmp_path):
    import sqlite3

    _account, token = accounts.create(FORM)
    rows = sqlite3.connect(tmp_path / "accounts.db").execute("SELECT * FROM sessions").fetchall()
    assert token not in str(rows), "a stolen database yields no usable cookie"


def test_a_token_nobody_issued_is_nobody(accounts):
    accounts.create(FORM)
    assert accounts.account_for_token("made-up-token") is None
    assert accounts.account_for_token(None) is None


def test_an_expired_session_is_refused(accounts, monkeypatch):
    _account, token = accounts.create(FORM)
    monkeypatch.setattr(time, "time", lambda: 9e9)  # thirty years on
    assert accounts.account_for_token(token) is None


def test_a_second_device_fills_the_form_again_and_gets_its_own(accounts):
    """No password means nothing can prove who anyone is, so nothing is handed over."""
    first, first_token = accounts.create(FORM)
    second, second_token = accounts.create(FORM)

    assert first.id != second.id
    assert accounts.account_for_token(first_token).id == first.id
    assert accounts.account_for_token(second_token).id == second.id
    assert first.owner_id != second.owner_id, "one device never reads the other's chats"


def test_a_repeat_email_is_recognised_so_the_crm_hears_it_once(accounts):
    assert accounts.seen_before("javier@example.com") is False
    accounts.create(FORM)
    assert accounts.seen_before("JAVIER@example.com") is True
    assert accounts.seen_before("someone@example.com") is False


# --- the free questions -------------------------------------------------------------------


def test_two_questions_are_free_then_the_form(accounts):
    owner = anonymous_owner("b1")
    assert accounts.free_left(owner) == 2
    assert accounts.needs_signup(owner) is False

    accounts.count_question(owner)
    assert accounts.free_left(owner) == 1
    accounts.count_question(owner)
    assert accounts.free_left(owner) == 0
    assert accounts.needs_signup(owner) is True


def test_one_browser_does_not_spend_anothers_questions(accounts):
    for _ in range(2):
        accounts.count_question(anonymous_owner("b1"))
    assert accounts.needs_signup(anonymous_owner("b1")) is True
    assert accounts.needs_signup(anonymous_owner("b2")) is False


def test_nobody_is_never_counted_or_walled(accounts):
    assert anonymous_owner("") == ""
    assert accounts.count_question("") == 0
    assert accounts.needs_signup("") is False


def test_no_free_questions_means_the_form_comes_first(tmp_path):
    store = Accounts(tmp_path / "accounts.db", free_questions=0)
    assert store.needs_signup(anonymous_owner("b1")) is True
    store.close()


# --- what never reaches the log -----------------------------------------------------------


def test_the_fields_never_reach_the_log(accounts, caplog):
    with caplog.at_level("DEBUG"):
        accounts.create(FORM)
    for secret in ("javier@example.com", "555-0134", "Javier"):
        assert secret not in caplog.text
    assert "signup recorded" in caplog.text


def test_a_broken_counter_does_not_raise(accounts, caplog):
    accounts.close()
    with caplog.at_level("WARNING"):
        assert accounts.count_question(anonymous_owner("b1")) == 0
    assert "could not count a question" in caplog.text


# --- the shapes that came before ------------------------------------------------------------


@pytest.mark.parametrize("shape", ["browser", "password"])
def test_an_earlier_database_keeps_its_signups(tmp_path, shape):
    """Two older shapes exist in the wild. Neither is dropped on the way through."""
    import sqlite3

    path = tmp_path / "accounts.db"
    old = sqlite3.connect(path)
    if shape == "browser":
        old.executescript(
            "CREATE TABLE accounts (browser_id TEXT PRIMARY KEY, name TEXT, email TEXT,"
            " phone TEXT, neighborhood TEXT, created_at REAL);"
            "CREATE TABLE usage (browser_id TEXT PRIMARY KEY, questions INTEGER);"
        )
        old.execute("INSERT INTO accounts VALUES ('b1','J','j@example.com','312','Logan',1.0)")
        old.execute("INSERT INTO usage VALUES ('b1', 2)")
    else:
        old.executescript(
            "CREATE TABLE accounts (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,"
            " name TEXT, phone TEXT, neighborhood TEXT, password_hash TEXT NOT NULL,"
            " created_at REAL, last_login_at REAL);"
            "CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, account_id TEXT,"
            " created_at REAL, expires_at REAL);"
            "CREATE TABLE usage (owner_id TEXT PRIMARY KEY, questions INTEGER);"
        )
        old.execute(
            "INSERT INTO accounts VALUES"
            " ('a1','j@example.com','J','312','Logan','scrypt$x',1.0,1.0)"
        )
        old.execute("INSERT INTO usage VALUES ('browser:b1', 2)")
    old.commit()
    old.close()

    store = Accounts(path, free_questions=2)
    assert store.legacy_signups() == 1, "kept, not dropped"
    # The old count carried over under the new key, so the wall stays where it was.
    assert store.needs_signup(anonymous_owner("b1")) is True
    account, token = store.create(FORM)
    assert store.account_for_token(token).id == account.id
    store.close()
