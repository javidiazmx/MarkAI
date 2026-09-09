"""Leads on their way to the CRM: the queue, the retries, and what the payload says."""

from __future__ import annotations

import json
import sqlite3

import pytest

from markai.web.accounts import Account
from markai.web.crm import MAX_ATTEMPTS, Crm, build_payload

ACCOUNT = Account(
    id="a1",
    email="javier@example.com",
    name="Javier Diaz",
    phone="312-555-0134",
    neighborhood="Logan Square",
)


class Sender:
    """A CRM that can be told to fail, so no test needs the network."""

    def __init__(self, fail_times: int = 0, error: str = "503 from the CRM") -> None:
        self.sent: list[dict] = []
        self.fail_times = fail_times
        self.error = error

    def __call__(self, payload: dict) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(self.error)
        self.sent.append(payload)


@pytest.fixture
def sender() -> Sender:
    return Sender()


@pytest.fixture
def crm(tmp_path, sender) -> Crm:
    queue = Crm(tmp_path / "leads.db", url="https://crm.test/hook", sender=sender)
    yield queue
    queue.close()


# --- the payload ------------------------------------------------------------------------


def test_the_payload_is_flat_and_says_where_it_came_from():
    payload = build_payload(ACCOUNT)
    assert payload["name"] == "Javier Diaz"
    assert payload["email"] == "javier@example.com"
    assert payload["phone"] == "312-555-0134"
    assert payload["neighborhood"] == "Logan Square"
    assert "Jay" in payload["source"]
    assert payload["created_at"].endswith("Z")
    assert all(not isinstance(value, dict | list) for value in payload.values()), (
        "flat, so any CRM or automation tool can map it once and be done"
    )


def test_the_payload_carries_what_they_asked_about():
    payload = build_payload(ACCOUNT, {"asked_about": "Deposit timing", "questions": 2})
    assert payload["asked_about"] == "Deposit timing"
    assert payload["questions"] == 2


# --- the queue --------------------------------------------------------------------------


def test_a_lead_is_written_down_then_sent(crm, sender):
    crm.enqueue("a1", build_payload(ACCOUNT))
    assert crm.counts() == {"total": 1, "delivered": 0, "gave_up": 0, "waiting": 1}

    assert crm.deliver_pending() == (1, 0)
    assert [lead["email"] for lead in sender.sent] == ["javier@example.com"]
    assert crm.counts()["delivered"] == 1
    assert crm.pending() == [], "a delivered lead is not tried again"


def test_a_failure_is_retried_later_not_lost(tmp_path):
    sender = Sender(fail_times=1)
    crm = Crm(tmp_path / "leads.db", url="https://crm.test/hook", sender=sender)
    crm.enqueue("a1", build_payload(ACCOUNT))

    assert crm.deliver_pending() == (0, 1)
    assert crm.counts()["waiting"] == 1
    assert crm.pending() == [], "not immediately: the backoff has to pass"
    assert "503 from the CRM" in crm.all()[0].last_error

    # Once the wait is over it goes out, and the queue is empty again.
    assert crm.deliver_pending(now=9e9) == (1, 0)
    assert len(sender.sent) == 1
    crm.close()


def test_it_gives_up_after_enough_tries_and_says_so(tmp_path, caplog):
    sender = Sender(fail_times=99)
    crm = Crm(tmp_path / "leads.db", url="https://crm.test/hook", sender=sender)
    crm.enqueue("a1", build_payload(ACCOUNT))
    with caplog.at_level("WARNING"):
        for _ in range(MAX_ATTEMPTS):
            crm.deliver_pending(now=9e9)

    assert crm.counts()["gave_up"] == 1
    assert crm.pending(now=9e9) == [], "it stops trying rather than hammering forever"
    assert "gave up sending lead" in caplog.text
    assert crm.all()[0].gave_up is True

    # After the URL is fixed, the owner can put them all back.
    sender.fail_times = 0
    assert crm.reset_attempts() == 1
    assert crm.deliver_pending() == (1, 0)
    crm.close()


def test_with_no_url_the_lead_still_queues(tmp_path):
    crm = Crm(tmp_path / "leads.db")
    assert crm.configured is False
    crm.enqueue("a1", build_payload(ACCOUNT))
    assert crm.deliver_pending() == (0, 0), "nothing is sent and nothing is lost"
    assert crm.counts()["waiting"] == 1
    crm.close()


def test_a_queued_lead_survives_a_restart(tmp_path):
    first = Crm(tmp_path / "leads.db")
    first.enqueue("a1", build_payload(ACCOUNT))
    first.close()

    sender = Sender()
    second = Crm(tmp_path / "leads.db", url="https://crm.test/hook", sender=sender)
    assert second.deliver_pending() == (1, 0)
    assert sender.sent[0]["email"] == "javier@example.com"
    second.close()


def test_the_error_it_records_is_not_the_lead(tmp_path):
    """Whatever ends up in a log or a terminal must not be somebody's phone number."""
    sender = Sender(fail_times=1, error="400: {'email': 'javier@example.com'} rejected")
    crm = Crm(tmp_path / "leads.db", url="https://crm.test/hook", sender=sender)
    crm.enqueue("a1", build_payload(ACCOUNT))
    crm.deliver_pending()
    recorded = crm.all()[0].last_error
    assert "RuntimeError" in recorded
    assert len(recorded) <= 300
    crm.close()


def test_the_payload_is_stored_as_json(crm, tmp_path):
    crm.enqueue("a1", build_payload(ACCOUNT, {"asked_about": "Deposits"}))
    row = sqlite3.connect(tmp_path / "leads.db").execute("SELECT payload FROM leads").fetchone()
    assert json.loads(row[0])["asked_about"] == "Deposits"


def test_delivery_in_the_background_does_not_raise(crm, sender):
    crm.enqueue("a1", build_payload(ACCOUNT))
    crm.deliver_soon()
    if crm._worker:
        crm._worker.join(timeout=5)
    assert len(sender.sent) == 1


# --- the email a CRM parses -------------------------------------------------------------


def test_the_email_body_is_the_three_labelled_lines():
    """LeadSimple parses this. Anything clever in it is a way to lose a lead."""
    from markai.web.crm import build_email

    subject, body = build_email(build_payload(ACCOUNT, {"asked_about": "Deposits", "questions": 2}))
    assert subject == "New lead from Jay: Javier Diaz"
    assert body == ("Name: Javier Diaz\nPhone: 312-555-0134\nEmail: javier@example.com"), (
        "three lines, in that order, and nothing after them"
    )


def test_the_subject_survives_a_missing_name():
    from markai.web.crm import build_email

    subject, body = build_email({"phone": "312", "email": "j@example.com"})
    assert subject == "New lead from Jay"
    assert body.startswith("Name: \n")


def test_the_email_is_addressed_and_replies_to_the_landlord(monkeypatch):
    """One message, to the CRM address, that Mark can just hit reply on."""
    from markai.web import crm as crm_module

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
    send = crm_module._email(
        "new-deal@newlead.leadsimple.com",
        "smtp.gmail.com",
        587,
        "jay@gcrealtyinc.com",
        "app-password",
        "",
        True,
        10.0,
    )
    send(build_payload(ACCOUNT))

    message = sent["message"]
    assert (sent["host"], sent["port"]) == ("smtp.gmail.com", 587)
    assert sent["starttls"] is True and sent["login"] == "jay@gcrealtyinc.com"
    assert message["To"] == "new-deal@newlead.leadsimple.com"
    assert message["From"] == "jay@gcrealtyinc.com", "falls back to the username"
    assert message["Reply-To"] == "javier@example.com"
    assert "Phone: 312-555-0134" in message.get_content()


def test_port_465_uses_ssl_and_skips_starttls(monkeypatch):
    from markai.web import crm as crm_module

    used = {}

    class FakeSSL:
        def __init__(self, host, port, timeout=None):
            used["ssl"] = True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            used["starttls"] = True

        def login(self, *a):
            pass

        def send_message(self, message):
            pass

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
    crm_module._email("to@crm.test", "mail.test", 465, "u", "p", "from@test", True, 10.0)(
        build_payload(ACCOUNT)
    )
    assert used == {"ssl": True}, "no STARTTLS on an already encrypted connection"


def test_a_refused_send_is_retried_not_lost(tmp_path):
    """SMTP goes down, the mailbox is full, the password is wrong: all the same to us."""
    sender = Sender(fail_times=1, error="SMTPAuthenticationError")
    crm = Crm(tmp_path / "leads.db", sender=sender, describe="email to crm")
    crm.enqueue("a1", build_payload(ACCOUNT))
    assert crm.deliver_pending() == (0, 1)
    assert crm.counts()["waiting"] == 1
    assert crm.deliver_pending(now=9e9) == (1, 0)
    crm.close()


def test_the_settings_pick_email_over_a_webhook(tmp_path):
    from markai.config import Settings
    from markai.web.crm import sender_from_settings

    both = Settings(
        _env_file=None,
        data_dir=tmp_path,
        lead_email_to="new-deal@newlead.leadsimple.com",
        smtp_host="smtp.gmail.com",
        crm_webhook_url="https://hooks.example.com/catch",
    )
    sender, describe = sender_from_settings(both)
    assert sender is not None
    assert "email to new-deal@newlead.leadsimple.com" in describe

    webhook_only = Settings(
        _env_file=None, data_dir=tmp_path, crm_webhook_url="https://hooks.example.com/catch"
    )
    assert "POST to https://hooks.example.com/catch" in sender_from_settings(webhook_only)[1]

    nothing = Settings(_env_file=None, data_dir=tmp_path)
    assert sender_from_settings(nothing) == (None, "")


def test_an_address_with_no_mail_server_says_so(tmp_path):
    from markai.config import Settings
    from markai.web.crm import sender_from_settings

    half = Settings(_env_file=None, data_dir=tmp_path, lead_email_to="new-deal@newlead.test")
    sender, describe = sender_from_settings(half)
    assert sender is None
    assert "no SMTP host" in describe, "a half-configured route has to be visible"
