"""Saved conversations: the titles, the isolation between browsers, and the pruning."""

from __future__ import annotations

import pytest

from markai.web.history import MAX_THREADS_PER_BROWSER, History, title_for


@pytest.fixture
def history(tmp_path):
    store = History(tmp_path / "conversations.db")
    yield store
    store.close()


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("How long do I have to return a deposit?", "How long do I have to return a deposit"),
        ("quick question: late fees", "Late fees"),
        ("¿Cómo desalojo a un inquilino que no paga?", "Cómo desalojo a un inquilino que no paga"),
        ("   ", "New conversation"),
        ("", "New conversation"),
    ],
)
def test_the_title_comes_from_the_question(question, expected):
    assert title_for(question) == expected


def test_a_long_question_is_cut_at_a_word():
    title = title_for(
        "My tenant in Berwyn stopped paying rent in January and will not answer the phone"
    )
    assert title.endswith("…")
    assert len(title) <= 61
    assert not title[:-1].endswith(" "), "the ellipsis follows a word, not a space"


def test_the_filler_is_stripped_but_the_question_survives():
    assert title_for("Hola, cuanto tiempo tengo para devolver el deposito") == (
        "Cuanto tiempo tengo para devolver el deposito"
    )


def test_an_exchange_becomes_a_listed_thread(history):
    history.record("b1", "t1", "How long for a deposit?", "45 days in Chicago.")

    threads = history.list("b1")
    assert [t.title for t in threads] == ["How long for a deposit"]
    assert threads[0].turns == 1
    assert threads[0].messages == [], "the list stays light; messages come from get()"

    full = history.get("b1", "t1")
    assert [m["role"] for m in full.messages] == ["user", "assistant"]
    assert full.messages[1]["content"] == "45 days in Chicago."


def test_a_second_question_appends_and_keeps_the_first_title(history):
    history.record("b1", "t1", "Deposit timing?", "45 days.")
    history.record("b1", "t1", "And the interest?", "Cook County sets the rate.")

    thread = history.get("b1", "t1")
    assert thread.title == "Deposit timing"
    assert thread.turns == 2
    assert len(thread.messages) == 4


def test_the_newest_conversation_is_listed_first(history):
    history.record("b1", "old", "First question", "First answer")
    history.record("b1", "new", "Second question", "Second answer")
    assert [t.id for t in history.list("b1")] == ["new", "old"]


def test_one_browser_never_sees_another(history):
    history.record("b1", "t1", "Mine", "Answer")
    history.record("b2", "t2", "Theirs", "Answer")

    assert [t.id for t in history.list("b1")] == ["t1"]
    assert history.get("b2", "t1") is None, "a known id from the wrong browser reads as missing"
    assert history.delete("b2", "t1") is False
    assert history.get("b1", "t1") is not None, "and deleting it did nothing"


def test_a_missing_browser_id_is_not_recorded(history):
    history.record("", "t1", "Question", "Answer")
    history.record("b1", "", "Question", "Answer")
    assert history.list("b1") == []
    assert history.list("") == []


def test_delete_removes_only_that_thread(history):
    history.record("b1", "t1", "One", "A")
    history.record("b1", "t2", "Two", "B")

    assert history.delete("b1", "t1") is True
    assert [t.id for t in history.list("b1")] == ["t2"]
    assert history.delete("b1", "t1") is False, "a second delete is a no-op, not an error"


def test_old_conversations_are_pruned(history):
    for index in range(MAX_THREADS_PER_BROWSER + 5):
        history.record("b1", f"t{index:03d}", f"Question {index}", "Answer")

    kept = history.list("b1", limit=MAX_THREADS_PER_BROWSER)
    assert len(kept) == MAX_THREADS_PER_BROWSER
    assert history.get("b1", "t000") is None, "the oldest fell off"
    assert history.get("b1", f"t{MAX_THREADS_PER_BROWSER + 4:03d}") is not None


def test_a_broken_database_loses_the_thread_not_the_answer(history, caplog):
    history.close()  # the same shape as a disk that has gone away mid-answer
    with caplog.at_level("WARNING"):
        history.record("b1", "t1", "Question", "Answer")
    assert "could not save conversation" in caplog.text
