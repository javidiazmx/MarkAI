"""Accounts: the password, the session, and the two free questions before one is needed."""

from __future__ import annotations

import time

import pytest

from markai.web.accounts import (
    MIN_PASSWORD_CHARS,
    WRONG_CREDENTIALS,
    Accounts,
    LoginError,
    SignupError,
    anonymous_owner,
    hash_password,
    parse,
    verify_password,
)

FORM = {
    "name": "Javier Diaz",
    "email": "Javier@Example.com",
    "phone": "(312) 555-0134",
    "neighborhood": "Logan Square",
    "password": "six flats and a boiler",
}


@pytest.fixture
def accounts(tmp_path) -> Accounts:
    store = Accounts(tmp_path / "accounts.db", free_questions=2)
    yield store
    store.close()


# --- passwords --------------------------------------------------------------------------


def test_a_password_verifies_against_its_own_hash():
    stored = hash_password("six flats and a boiler")
    assert verify_password("six flats and a boiler", stored) is True
    assert verify_password("six flats and a boilers", stored) is False


def test_the_hash_is_not_the_password():
    stored = hash_password("six flats and a boiler")
    assert "six flats" not in stored
    assert stored.startswith("scrypt$")


def test_two_hashes_of_one_password_differ():
    assert hash_password("same password") != hash_password("same password"), "salted"


def test_the_cost_travels_with_the_hash():
    """So the parameters can be raised later without locking anyone out."""
    stored = hash_password("six flats and a boiler")
    scheme, n, r, p, _salt, _digest = stored.split("$")
    assert (scheme, int(n) > 1000, int(r), int(p)) == ("scrypt", True, 8, 1)


@pytest.mark.parametrize("garbage", ["", "nonsense", "scrypt$x$y$z$a$b", "bcrypt$1$2$3$4$5"])
def test_a_hash_that_cannot_be_read_is_a_failure_not_a_crash(garbage):
    assert verify_password("anything", garbage) is False


# --- the form ---------------------------------------------------------------------------


def test_every_field_is_kept_and_the_email_is_the_username():
    account, password = parse(FORM)
    assert account.name == "Javier Diaz"
    assert account.email == "javier@example.com", "lowercased, so it matches every time"
    assert account.phone == "(312) 555-0134"
    assert account.neighborhood == "Logan Square"
    assert password == "six flats and a boiler"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "J", "your name"),
        ("email", "javier@", "email address"),
        ("phone", "555", "area code"),
        ("neighborhood", "", "neighborhood"),
        ("password", "short", f"at least {MIN_PASSWORD_CHARS}"),
        ("password", "", f"at least {MIN_PASSWORD_CHARS}"),
        ("password", "x" * 400, "longer than"),
        ("password", "javier@example.com", "cannot be your email"),
        ("password", "with a\nline break", "line break"),
    ],
)
def test_a_field_that_cannot_be_used_says_which_one(field, value, message):
    with pytest.raises(SignupError, match=message):
        parse({**FORM, field: value})


def test_a_phone_written_any_way_is_accepted():
    for written in ("312-555-0134", "+1 312 555 0134", "3125550134", "(312) 555 0134 x22"):
        assert parse({**FORM, "phone": written})[0].phone


def test_a_passphrase_with_spaces_is_a_fine_password():
    account, password = parse({**FORM, "password": "my six flat on division"})
    assert verify_password(password, hash_password(password))
    assert account.email == "javier@example.com"


# --- signing up and in ------------------------------------------------------------------


def test_signing_up_returns_an_account_and_a_session(accounts):
    account, token = accounts.create(FORM)
    assert token
    assert accounts.account_for_token(token).email == "javier@example.com"
    assert accounts.by_email("JAVIER@EXAMPLE.COM").id == account.id


def test_the_same_email_cannot_be_taken_twice(accounts):
    accounts.create(FORM)
    with pytest.raises(SignupError, match="already an account"):
        accounts.create({**FORM, "email": "JAVIER@example.com", "name": "Someone Else"})


def test_signing_in_with_the_right_password_works(accounts):
    created, _ = accounts.create(FORM)
    account, token = accounts.sign_in("javier@example.com", "six flats and a boiler")
    assert account.id == created.id
    assert accounts.account_for_token(token) is not None


def test_the_email_is_case_insensitive_at_sign_in(accounts):
    accounts.create(FORM)
    assert accounts.sign_in("  JAVIER@Example.com ", "six flats and a boiler")[0]


def test_a_wrong_password_and_an_unknown_email_look_identical(accounts):
    accounts.create(FORM)
    with pytest.raises(LoginError) as wrong:
        accounts.sign_in("javier@example.com", "not the password")
    with pytest.raises(LoginError) as unknown:
        accounts.sign_in("nobody@example.com", "not the password")
    assert str(wrong.value) == str(unknown.value) == WRONG_CREDENTIALS


def test_repeated_failures_are_throttled(accounts):
    accounts.create(FORM)
    for _ in range(5):
        with pytest.raises(LoginError, match="do not match"):
            accounts.sign_in("javier@example.com", "wrong")
    with pytest.raises(LoginError, match="Too many tries"):
        accounts.sign_in("javier@example.com", "wrong")
    with pytest.raises(LoginError, match="Too many tries"):
        accounts.sign_in("javier@example.com", "six flats and a boiler")


def test_a_good_password_clears_the_failure_count(accounts):
    accounts.create(FORM)
    for _ in range(3):
        with pytest.raises(LoginError):
            accounts.sign_in("javier@example.com", "wrong")
    assert accounts.sign_in("javier@example.com", "six flats and a boiler")[0]
    for _ in range(3):
        with pytest.raises(LoginError, match="do not match"):
            accounts.sign_in("javier@example.com", "wrong")


# --- sessions ---------------------------------------------------------------------------


def test_the_stored_session_is_not_the_token(accounts, tmp_path):
    import sqlite3

    _account, token = accounts.create(FORM)
    rows = sqlite3.connect(tmp_path / "accounts.db").execute("SELECT * FROM sessions").fetchall()
    assert token not in str(rows), "a stolen database yields no usable cookie"


def test_signing_out_ends_that_session(accounts):
    _account, token = accounts.create(FORM)
    accounts.end_session(token)
    assert accounts.account_for_token(token) is None


def test_an_expired_session_is_refused(accounts, monkeypatch):
    _account, token = accounts.create(FORM)
    monkeypatch.setattr(time, "time", lambda: 9e9)  # thirty years on
    assert accounts.account_for_token(token) is None


def test_a_token_nobody_issued_is_nobody(accounts):
    accounts.create(FORM)
    assert accounts.account_for_token("made-up-token") is None
    assert accounts.account_for_token(None) is None
    assert accounts.account_for_token("") is None


def test_two_sign_ins_are_two_sessions(accounts):
    """A phone and a laptop are both signed in, and signing out of one keeps the other."""
    _account, phone = accounts.create(FORM)
    _account2, laptop = accounts.sign_in("javier@example.com", "six flats and a boiler")
    accounts.end_session(phone)
    assert accounts.account_for_token(phone) is None
    assert accounts.account_for_token(laptop) is not None


def test_resetting_a_password_signs_everyone_out(accounts):
    _account, token = accounts.create(FORM)
    accounts.set_password("javier@example.com", "a whole new password")
    assert accounts.account_for_token(token) is None
    assert accounts.sign_in("javier@example.com", "a whole new password")[0]
    with pytest.raises(LoginError):
        accounts.sign_in("javier@example.com", "six flats and a boiler")


def test_resetting_an_unknown_account_says_so(accounts):
    with pytest.raises(SignupError, match="No account"):
        accounts.set_password("nobody@example.com", "a whole new password")


def test_a_reset_still_checks_the_password(accounts):
    accounts.create(FORM)
    with pytest.raises(SignupError, match=f"at least {MIN_PASSWORD_CHARS}"):
        accounts.set_password("javier@example.com", "short")


# --- the free questions -----------------------------------------------------------------


def test_two_questions_are_free_then_an_account_is_needed(accounts):
    owner = anonymous_owner("b1")
    assert accounts.free_left(owner) == 2
    assert accounts.needs_signup(owner) is False

    accounts.count_question(owner)
    assert accounts.free_left(owner) == 1
    accounts.count_question(owner)
    assert accounts.free_left(owner) == 0
    assert accounts.needs_signup(owner) is True


def test_a_signed_in_owner_is_never_walled(accounts):
    account, _token = accounts.create(FORM)
    for _ in range(5):
        accounts.count_question(account.owner_id)
    assert accounts.needs_signup(account.owner_id) is False
    assert accounts.free_left(account.owner_id) == 0


def test_one_browser_does_not_spend_anothers_questions(accounts):
    for _ in range(2):
        accounts.count_question(anonymous_owner("b1"))
    assert accounts.needs_signup(anonymous_owner("b1")) is True
    assert accounts.needs_signup(anonymous_owner("b2")) is False


def test_nobody_is_never_counted_or_walled(accounts):
    assert anonymous_owner("") == ""
    assert accounts.count_question("") == 0
    assert accounts.needs_signup("") is False


def test_no_free_questions_means_an_account_comes_first(tmp_path):
    store = Accounts(tmp_path / "accounts.db", free_questions=0)
    assert store.needs_signup(anonymous_owner("b1")) is True
    store.close()


# --- what never reaches the log ---------------------------------------------------------


def test_neither_the_fields_nor_the_password_reach_the_log(accounts, caplog):
    with caplog.at_level("DEBUG"):
        accounts.create(FORM)
        accounts.sign_in("javier@example.com", "six flats and a boiler")
    for secret in ("javier@example.com", "555-0134", "Javier", "six flats and a boiler"):
        assert secret not in caplog.text
    assert "account created" in caplog.text


def test_a_broken_counter_does_not_raise(accounts, caplog):
    accounts.close()
    with caplog.at_level("WARNING"):
        assert accounts.count_question(anonymous_owner("b1")) == 0
    assert "could not count a question" in caplog.text


# --- the old shape ----------------------------------------------------------------------


def test_a_pre_password_database_keeps_its_signups(tmp_path):
    """The first version had no password, so those rows cannot become accounts."""
    import sqlite3

    path = tmp_path / "accounts.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE accounts (browser_id TEXT PRIMARY KEY, name TEXT, email TEXT,"
        " phone TEXT, neighborhood TEXT, created_at REAL);"
        "CREATE TABLE usage (browser_id TEXT PRIMARY KEY, questions INTEGER);"
    )
    old.execute(
        "INSERT INTO accounts VALUES ('b1', 'Javier', 'j@example.com', '312', 'Logan', 1.0)"
    )
    old.execute("INSERT INTO usage VALUES ('b1', 2)")
    old.commit()
    old.close()

    store = Accounts(path, free_questions=2)
    assert store.legacy_signups() == 1, "kept, not dropped"
    assert store.by_email("j@example.com") is None, "there is nothing to sign in with"
    # The old count carried over under the new key, so the wall stays where it was.
    assert store.needs_signup(anonymous_owner("b1")) is True
    account, _token = store.create(FORM)
    assert store.account_for_token(_token).id == account.id
    store.close()
