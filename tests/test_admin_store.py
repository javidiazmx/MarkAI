"""The admin panel's own store: who has shown a PM-fit signal, and the staff alert email."""

from __future__ import annotations

import pytest

from markai.web.accounts import Account
from markai.web.admin_store import (
    AdminStore,
    build_alert_email,
    pm_fit_alert_sender,
    send_pm_fit_alert_soon,
)

ACCOUNT = Account(
    id="a1",
    email="javier@example.com",
    name="Javier Diaz",
    phone="312-555-0134",
    neighborhood="Logan Square",
)


# --- the store ---------------------------------------------------------------------------


def test_a_new_account_has_no_fit_row(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    assert store.get("a1") is None
    assert store.all() == {}
    store.close()


def test_a_signal_makes_good_fit_true(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    assert store.record_signal("a1", "pm_interest") is True
    fit = store.get("a1")
    assert fit.signals == ["pm_interest"]
    assert fit.good_fit is True
    assert fit.manual_override is None
    store.close()


def test_the_same_signal_twice_is_not_a_new_alert(tmp_path):
    """A landlord who mentions the same thing three times fires the alert once."""
    store = AdminStore(tmp_path / "admin.db")
    assert store.record_signal("a1", "tenant_trouble") is True
    assert store.record_signal("a1", "tenant_trouble") is False
    assert store.get("a1").signals == ["tenant_trouble"]
    store.close()


def test_a_second_signal_is_added_and_is_new(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.record_signal("a1", "pm_interest")
    assert store.record_signal("a1", "vacancy_help") is True
    assert store.get("a1").signals == ["pm_interest", "vacancy_help"]
    store.close()


def test_a_manual_override_wins_over_the_signals(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.record_signal("a1", "pm_interest")
    assert store.get("a1").good_fit is True

    store.set_override("a1", False)
    assert store.get("a1").good_fit is False

    store.set_override("a1", None)
    assert store.get("a1").good_fit is True, "clearing the override goes back to the AI's call"
    store.close()


def test_an_override_with_no_prior_signals_still_saves(tmp_path):
    """Staff can mark someone a fit even if the AI never flagged them."""
    store = AdminStore(tmp_path / "admin.db")
    store.set_override("a2", True)
    fit = store.get("a2")
    assert fit.signals == []
    assert fit.good_fit is True
    store.close()


def test_all_lists_every_account(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.record_signal("a1", "pm_interest")
    store.set_override("a2", True)
    assert set(store.all().keys()) == {"a1", "a2"}
    store.close()


def test_a_row_survives_a_restart(tmp_path):
    first = AdminStore(tmp_path / "admin.db")
    first.record_signal("a1", "pm_interest")
    first.close()

    second = AdminStore(tmp_path / "admin.db")
    assert second.get("a1").signals == ["pm_interest"]
    second.close()


# --- the last-seen IP, for the admin panel's location lookup ------------------------------


def test_note_visit_records_the_ip_with_no_signal(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.note_visit("browser:b1", "203.0.113.7")
    fit = store.get("browser:b1")
    assert fit.last_ip == "203.0.113.7"
    assert fit.signals == [], "a plain visit is not a fit signal"
    store.close()


def test_note_visit_updates_the_ip_without_touching_signals(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.record_signal("a1", "pm_interest")
    store.note_visit("a1", "203.0.113.7")
    store.note_visit("a1", "198.51.100.9")
    fit = store.get("a1")
    assert fit.last_ip == "198.51.100.9"
    assert fit.signals == ["pm_interest"]
    store.close()


def test_note_visit_ignores_empty_owner_or_ip(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.note_visit("", "203.0.113.7")
    store.note_visit("a1", "")
    assert store.get("a1") is None
    store.close()


# --- reassigning a visitor's signals to their new account ---------------------------------


def test_a_visitors_signals_carry_over_to_their_new_account(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.record_signal("browser:b1", "tenant_trouble")

    assert store.reassign("browser:b1", "account:a1") == 1
    assert store.get("browser:b1") is None, "the anonymous row is gone"
    assert store.get("account:a1").signals == ["tenant_trouble"]
    store.close()


def test_reassign_merges_rather_than_overwrites_an_existing_row(tmp_path):
    """A landlord could ask something anonymous, sign up, then trip a second signal later -
    a second reassign (e.g. re-running the flow in a test) must not lose the first one."""
    store = AdminStore(tmp_path / "admin.db")
    store.record_signal("account:a1", "pm_interest")
    store.record_signal("browser:b1", "tenant_trouble")

    store.reassign("browser:b1", "account:a1")

    merged = store.get("account:a1")
    assert merged.signals == ["pm_interest", "tenant_trouble"]
    store.close()


def test_reassign_keeps_a_manual_override_from_either_side(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.set_override("browser:b1", True)
    store.reassign("browser:b1", "account:a1")
    assert store.get("account:a1").manual_override is True
    store.close()


def test_reassign_carries_the_ip_forward(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.note_visit("browser:b1", "203.0.113.7")
    store.reassign("browser:b1", "account:a1")
    assert store.get("account:a1").last_ip == "203.0.113.7"
    store.close()


def test_reassign_with_no_anonymous_row_is_a_no_op(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    assert store.reassign("browser:b1", "account:a1") == 0
    assert store.get("account:a1") is None
    store.close()


def test_reassign_ignores_empty_or_identical_owners(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.record_signal("browser:b1", "tenant_trouble")
    assert store.reassign("", "account:a1") == 0
    assert store.reassign("browser:b1", "") == 0
    assert store.reassign("browser:b1", "browser:b1") == 0
    assert store.get("browser:b1").signals == ["tenant_trouble"], "nothing was touched"
    store.close()


# --- carrying an older, bare-account-id database forward -----------------------------------


def test_an_old_database_keyed_on_a_bare_account_id_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "admin.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE pm_fit (account_id TEXT PRIMARY KEY, signals TEXT NOT NULL DEFAULT '[]',"
        " manual_override INTEGER, updated_at REAL NOT NULL);"
    )
    conn.execute(
        "INSERT INTO pm_fit (account_id, signals, updated_at) VALUES (?, ?, ?)",
        ("a1", '["pm_interest"]', 0.0),
    )
    conn.commit()
    conn.close()

    store = AdminStore(path)
    assert store.get("a1") is None, "the bare id no longer resolves"
    assert store.get("account:a1").signals == ["pm_interest"], "carried forward, now prefixed"
    assert store.get("account:a1").last_ip == "", "a column this old database never had"
    store.close()


def test_a_database_with_owner_id_but_no_last_ip_column_is_migrated(tmp_path):
    """The shape this table shipped in one release before `last_ip` was added."""
    import sqlite3

    path = tmp_path / "admin.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE pm_fit (owner_id TEXT PRIMARY KEY, signals TEXT NOT NULL DEFAULT '[]',"
        " manual_override INTEGER, updated_at REAL NOT NULL);"
    )
    conn.execute(
        "INSERT INTO pm_fit (owner_id, signals, updated_at) VALUES (?, ?, ?)",
        ("account:a1", '["pm_interest"]', 0.0),
    )
    conn.commit()
    conn.close()

    store = AdminStore(path)
    fit = store.get("account:a1")
    assert fit.signals == ["pm_interest"]
    assert fit.last_ip == ""
    store.note_visit("account:a1", "203.0.113.7")
    assert store.get("account:a1").last_ip == "203.0.113.7"
    store.close()


def test_a_database_with_last_ip_but_no_notes_or_hidden_columns_is_migrated(tmp_path):
    """The shape this table shipped in before `notes`/`hidden` were added."""
    import sqlite3

    path = tmp_path / "admin.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE pm_fit (owner_id TEXT PRIMARY KEY, signals TEXT NOT NULL DEFAULT '[]',"
        " manual_override INTEGER, last_ip TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);"
    )
    conn.execute(
        "INSERT INTO pm_fit (owner_id, signals, last_ip, updated_at) VALUES (?, ?, ?, ?)",
        ("account:a1", '["pm_interest"]', "203.0.113.7", 0.0),
    )
    conn.commit()
    conn.close()

    store = AdminStore(path)
    fit = store.get("account:a1")
    assert fit.signals == ["pm_interest"]
    assert fit.last_ip == "203.0.113.7"
    assert fit.notes == ""
    assert fit.hidden is False
    store.set_notes("account:a1", "Called them, interested in PM")
    store.set_hidden("account:a1", True)
    updated = store.get("account:a1")
    assert updated.notes == "Called them, interested in PM"
    assert updated.hidden is True
    store.close()


# --- staff notes ---------------------------------------------------------------------------


def test_set_notes_creates_a_row_with_no_prior_signal(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.set_notes("browser:b1", "Seems like a tire-kicker")
    fit = store.get("browser:b1")
    assert fit.notes == "Seems like a tire-kicker"
    assert fit.signals == []
    store.close()


def test_set_notes_updates_without_touching_signals_or_override(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.record_signal("a1", "pm_interest")
    store.set_override("a1", True)
    store.set_notes("a1", "First notes")
    store.set_notes("a1", "Updated notes")
    fit = store.get("a1")
    assert fit.notes == "Updated notes"
    assert fit.signals == ["pm_interest"]
    assert fit.manual_override is True
    store.close()


def test_set_notes_trims_and_caps_length(tmp_path):
    from markai.web.admin_store import MAX_NOTES_CHARS

    store = AdminStore(tmp_path / "admin.db")
    store.set_notes("a1", "  padded  ")
    assert store.get("a1").notes == "padded"
    store.set_notes("a1", "x" * (MAX_NOTES_CHARS + 500))
    assert len(store.get("a1").notes) == MAX_NOTES_CHARS
    store.close()


def test_set_notes_ignores_an_empty_owner_id(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.set_notes("", "orphaned notes")
    assert store.all() == {}
    store.close()


# --- soft hide -----------------------------------------------------------------------------


def test_set_hidden_creates_a_row_with_no_prior_signal(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.set_hidden("browser:b1", True)
    fit = store.get("browser:b1")
    assert fit.hidden is True
    assert fit.signals == []
    store.close()


def test_set_hidden_can_be_reversed(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.set_hidden("a1", True)
    assert store.get("a1").hidden is True
    store.set_hidden("a1", False)
    assert store.get("a1").hidden is False
    store.close()


def test_set_hidden_does_not_touch_signals_or_notes(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.record_signal("a1", "pm_interest")
    store.set_notes("a1", "Keep an eye on this one")
    store.set_hidden("a1", True)
    fit = store.get("a1")
    assert fit.signals == ["pm_interest"]
    assert fit.notes == "Keep an eye on this one"
    assert fit.hidden is True
    store.close()


def test_reassign_carries_notes_and_hidden_forward(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.set_notes("browser:b1", "Anonymous notes")
    store.set_hidden("browser:b1", True)
    store.reassign("browser:b1", "account:a1")
    merged = store.get("account:a1")
    assert merged.notes == "Anonymous notes"
    assert merged.hidden is True
    store.close()


# --- the alert email -----------------------------------------------------------------------


def test_the_alert_email_is_human_readable_not_machine_parsed():
    subject, body = build_alert_email(ACCOUNT, "tenant_trouble", "Described a tenant problem")
    assert "Javier Diaz" in subject
    assert "tenant_trouble" in body
    assert "Described a tenant problem" in body
    assert "javier@example.com" in body
    assert "312-555-0134" in body


def test_the_settings_pick_sender_only_when_both_are_set(tmp_path):
    from markai.config import Settings

    neither = Settings(_env_file=None, data_dir=tmp_path)
    assert pm_fit_alert_sender(neither) is None

    only_address = Settings(
        _env_file=None, data_dir=tmp_path, pm_fit_alert_email_to="staff@gcrealtyinc.com"
    )
    assert pm_fit_alert_sender(only_address) is None

    both = Settings(
        _env_file=None,
        data_dir=tmp_path,
        pm_fit_alert_email_to="staff@gcrealtyinc.com",
        smtp_host="smtp.gmail.com",
    )
    assert pm_fit_alert_sender(both) is not None


def test_the_alert_is_sent_over_smtp(monkeypatch):
    from markai.web import admin_store as admin_store_module

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"], sent["port"] = host, port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            sent["starttls"] = True

        def login(self, username, password):
            sent["login"] = username

        def send_message(self, message):
            sent["message"] = message

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    send = admin_store_module._email_sender(
        "staff@gcrealtyinc.com",
        "smtp.gmail.com",
        587,
        "jay@gcrealtyinc.com",
        "app-password",
        "",
        True,
        10.0,
    )
    send(ACCOUNT, "pm_interest", "Asked about hiring a property manager")

    message = sent["message"]
    assert (sent["host"], sent["port"]) == ("smtp.gmail.com", 587)
    assert message["To"] == "staff@gcrealtyinc.com"
    assert "Javier Diaz" in message.get_content()


def test_a_failed_alert_never_raises_into_the_caller():
    """Best-effort, same discipline as crm.py: an alert problem is never a broken chat answer."""

    def boom(account, flag, reason):
        raise RuntimeError("smtp is down")

    send_pm_fit_alert_soon(boom, ACCOUNT, "pm_interest", "reason")  # must not raise


def test_a_missing_sender_is_a_no_op():
    send_pm_fit_alert_soon(None, ACCOUNT, "pm_interest", "reason")  # must not raise


# --- AI classification, staff-triggered ----------------------------------------------------


def test_set_ai_analysis_creates_a_row_with_no_prior_signal(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.set_ai_analysis("browser:b1", True, "Sounds burned out and asked about a PM.")
    fit = store.get("browser:b1")
    assert fit.ai_good_fit is True
    assert fit.ai_reasoning == "Sounds burned out and asked about a PM."
    assert fit.ai_analyzed_at is not None
    assert fit.signals == []
    store.close()


def test_ai_analysis_sits_between_override_and_signals_in_precedence(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    # No signal, no override, no AI read yet: falls back to the blunt regex signals (none).
    assert store.get("a1") is None

    store.set_ai_analysis("a1", True, "Real interest in hiring a PM.")
    assert store.get("a1").good_fit is True, "the AI's own read, with no signals behind it"

    store.record_signal("a1", "tenant_trouble")
    store.set_ai_analysis("a1", False, "Just a one-off factual question, no real signal.")
    assert store.get("a1").good_fit is False, "the AI's read overrides the blunt keyword match"

    store.set_override("a1", True)
    assert store.get("a1").good_fit is True, "and a human overrides the AI"
    store.close()


def test_reanalyzing_overwrites_the_previous_verdict(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.set_ai_analysis("a1", False, "First read.")
    store.set_ai_analysis("a1", True, "Second read, after more of the conversation.")
    fit = store.get("a1")
    assert fit.ai_good_fit is True
    assert fit.ai_reasoning == "Second read, after more of the conversation."
    store.close()


def test_reassign_carries_the_ai_analysis_forward(tmp_path):
    store = AdminStore(tmp_path / "admin.db")
    store.set_ai_analysis("browser:b1", True, "Anonymous read.")
    store.reassign("browser:b1", "account:a1")
    merged = store.get("account:a1")
    assert merged.ai_good_fit is True
    assert merged.ai_reasoning == "Anonymous read."
    store.close()


def test_reassign_prefers_the_accounts_own_analysis_if_it_has_one(tmp_path):
    """If staff already analyzed the signed-up account directly, an older anonymous read
    from before signup should not clobber it."""
    store = AdminStore(tmp_path / "admin.db")
    store.set_ai_analysis("browser:b1", True, "Anonymous read.")
    store.set_ai_analysis("account:a1", False, "Analyzed after they signed up.")
    store.reassign("browser:b1", "account:a1")
    merged = store.get("account:a1")
    assert merged.ai_good_fit is False
    assert merged.ai_reasoning == "Analyzed after they signed up."
    store.close()


def test_a_database_with_no_ai_columns_is_migrated(tmp_path):
    """The shape this table shipped in before AI classification was added."""
    import sqlite3

    path = tmp_path / "admin.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE pm_fit (owner_id TEXT PRIMARY KEY, signals TEXT NOT NULL DEFAULT '[]',"
        " manual_override INTEGER, last_ip TEXT NOT NULL DEFAULT '',"
        " notes TEXT NOT NULL DEFAULT '', hidden INTEGER NOT NULL DEFAULT 0,"
        " updated_at REAL NOT NULL);"
    )
    conn.execute(
        "INSERT INTO pm_fit (owner_id, signals, updated_at) VALUES (?, ?, ?)",
        ("account:a1", '["pm_interest"]', 0.0),
    )
    conn.commit()
    conn.close()

    store = AdminStore(path)
    fit = store.get("account:a1")
    assert fit.signals == ["pm_interest"]
    assert fit.ai_good_fit is None
    assert fit.ai_reasoning == ""
    assert fit.ai_analyzed_at is None
    store.set_ai_analysis("account:a1", True, "Now analyzed.")
    assert store.get("account:a1").ai_good_fit is True
    store.close()


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [_FakeTextBlock(text)]


class _FakeClient:
    """Enough of the Anthropic SDK's shape for `classify_fit`, nothing more."""

    def __init__(self, reply_text: str, error: Exception | None = None) -> None:
        self._reply_text = reply_text
        self._error = error
        self.calls: list[dict] = []

        class _Messages:
            def create(inner_self, **kwargs):
                self.calls.append(kwargs)
                if self._error:
                    raise self._error
                return _FakeMessage(self._reply_text)

        self.messages = _Messages()


class _FakeThread:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages


def test_classify_fit_parses_a_good_verdict():
    from markai.web.admin_store import classify_fit

    client = _FakeClient(
        '{"good_fit": true, "confidence": "high", '
        '"reasoning": "Explicitly asked about hiring a property manager."}'
    )
    threads = [
        _FakeThread(
            [
                {"role": "user", "content": "Should I just hire a property manager?"},
                {"role": "assistant", "content": "Depends on a few things..."},
            ]
        )
    ]
    good_fit, reasoning = classify_fit(object(), threads, client=client)
    assert good_fit is True
    assert "property manager" in reasoning
    assert client.calls[0]["model"] == "claude-haiku-4-5-20251001"


def test_classify_fit_parses_a_not_a_fit_verdict():
    from markai.web.admin_store import classify_fit

    client = _FakeClient('{"good_fit": false, "reasoning": "Just a factual question."}')
    threads = [_FakeThread([{"role": "user", "content": "How long to return a deposit?"}])]
    good_fit, reasoning = classify_fit(object(), threads, client=client)
    assert good_fit is False
    assert reasoning == "Just a factual question."


def test_classify_fit_handles_extra_text_around_the_json():
    """Cheap models sometimes wrap the JSON in a sentence anyway - still usable."""
    from markai.web.admin_store import classify_fit

    client = _FakeClient(
        'Here is my answer: {"good_fit": true, "reasoning": "Sounds burned out."} Hope that helps!'
    )
    threads = [_FakeThread([{"role": "user", "content": "I am so tired of this tenant."}])]
    good_fit, reasoning = classify_fit(object(), threads, client=client)
    assert good_fit is True
    assert reasoning == "Sounds burned out."


def test_classify_fit_raises_on_no_conversation():
    from markai.web.admin_store import ClassificationError, classify_fit

    with pytest.raises(ClassificationError, match="No conversation|no conversation"):
        classify_fit(object(), [], client=_FakeClient("{}"))


def test_classify_fit_raises_on_unparseable_response():
    from markai.web.admin_store import ClassificationError, classify_fit

    client = _FakeClient("I'm not sure how to answer that.")
    threads = [_FakeThread([{"role": "user", "content": "Hi"}])]
    with pytest.raises(ClassificationError):
        classify_fit(object(), threads, client=client)


def test_classify_fit_raises_when_the_model_call_fails():
    from markai.web.admin_store import ClassificationError, classify_fit

    client = _FakeClient("", error=RuntimeError("503 from Anthropic"))
    threads = [_FakeThread([{"role": "user", "content": "Hi"}])]
    with pytest.raises(ClassificationError, match="Could not reach the model"):
        classify_fit(object(), threads, client=client)


def test_classify_fit_needs_no_api_key_argument_when_a_client_is_injected():
    """Settings is never touched when a fake client is passed - confirms the key lookup is
    skipped entirely, the same injectable-client pattern the rest of this project uses."""
    from markai.web.admin_store import classify_fit

    class _ExplodingSettings:
        def anthropic_key(self):
            raise AssertionError("should not be called when a client is injected")

    client = _FakeClient('{"good_fit": true, "reasoning": "ok"}')
    threads = [_FakeThread([{"role": "user", "content": "Hi"}])]
    good_fit, _ = classify_fit(_ExplodingSettings(), threads, client=client)
    assert good_fit is True
