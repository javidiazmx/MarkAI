"""The admin panel's own store: who has shown a PM-fit signal, and the staff alert email."""

from __future__ import annotations

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
