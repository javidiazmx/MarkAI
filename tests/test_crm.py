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
    has_password=False,
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
