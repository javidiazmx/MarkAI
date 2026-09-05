"""The advisor: request shape, tool loop, refusals, follow-ups, and post-processing."""

from __future__ import annotations

import pytest

from markai.advisor.guardrails import HIGH_RISK_RESPONSE, LEGAL_DISCLAIMER, REFUSAL_TEXT
from markai.advisor.mark import (
    EMPTY_STORE_TEXT,
    TRUNCATED_NOTE,
    Conversation,
    MarkAdvisor,
    MissingApiKeyError,
)
from markai.knowledge.retriever import Retriever
from tests.fakes import FakeAnthropic, refusal_message, text_message, tool_use_message

PROMPT = "You are Mark. " + "Answer from the sources. " * 40


def build_advisor(settings, store, finals, tools=None, business=None):
    retriever = Retriever(store, None, settings)
    client = FakeAnthropic(finals)
    advisor = MarkAdvisor(
        settings,
        retriever,
        tools or [],
        PROMPT,
        business=business,
        client=client,
        store=store,
    )
    return advisor, client


def test_request_shape_matches_the_opus_5_contract(settings, store):
    advisor, client = build_advisor(settings, store, [text_message("Screening matters.")])
    advisor.ask("How should I screen tenants?")

    call = client.calls[0]
    assert call["model"] == settings.model
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["fallbacks"] == "default"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": settings.effort}
    assert call["cache_control"] == {"type": "ephemeral"}
    assert call["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert call["max_tokens"] == settings.request_max_tokens()
    assert [t["name"] for t in call["tools"]] == ["analyze_deal", "mortgage_payment"]
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in call


def test_system_prompt_is_identical_across_turns(settings, store):
    advisor, client = build_advisor(settings, store, [text_message("One."), text_message("Two.")])
    conversation = Conversation(session_id="t")
    advisor.ask("How should I screen tenants?", conversation)
    advisor.ask("What about the deposit rules for a Chicago two flat?", conversation)
    assert client.calls[0]["system"] == client.calls[1]["system"]


def test_answer_text_and_citations_come_back(settings, store):
    advisor, _ = build_advisor(settings, store, [text_message("Screen everyone the same [S1].")])
    response = advisor.ask("How should I screen tenants?")
    assert "[S1]" in response.text
    assert [c.marker for c in response.citations] == ["S1"]
    assert response.coverage in ("covered", "weak")
    assert response.usage["input_tokens"] > 0


def test_invented_markers_are_stripped(settings, store):
    advisor, _ = build_advisor(settings, store, [text_message("Real [S1] and fake [S99] source.")])
    response = advisor.ask("How should I screen tenants?")
    assert "[S99]" not in response.text
    assert "[S1]" in response.text


def test_legal_disclaimer_is_appended_and_streamed(settings, store):
    advisor, _ = build_advisor(
        settings, store, [text_message("You owe deposit interest every year.")]
    )
    deltas = []
    response = None
    for event in advisor.stream("What are the security deposit rules?"):
        if event.type == "text":
            deltas.append(event.text)
        elif event.type == "final":
            response = event.response
    assert response.text.endswith(LEGAL_DISCLAIMER)
    assert "".join(deltas).endswith(LEGAL_DISCLAIMER)
    assert response.text.count("I'm not a lawyer") == 1


def test_high_risk_question_gets_the_refusal_even_if_the_model_complies(settings, store):
    advisor, _ = build_advisor(
        settings, store, [text_message("Sure, here is how to filter those applicants out.")]
    )
    response = advisor.ask("How do I keep Section 8 tenants out of my building?")
    assert response.text.startswith(HIGH_RISK_RESPONSE)
    assert response.text.endswith(LEGAL_DISCLAIMER)
    assert "high_risk_request" in response.flags


def test_refusal_discards_the_partial_answer(settings, store):
    advisor, _ = build_advisor(settings, store, [refusal_message()])
    response = advisor.ask("How should I screen tenants?")
    assert response.text == REFUSAL_TEXT
    assert response.stop_reason == "refusal"
    assert response.citations == []
    assert "Here is how you could" not in response.text


def test_max_tokens_stop_adds_a_note(settings, store):
    advisor, _ = build_advisor(
        settings, store, [text_message("A partial answer", stop_reason="max_tokens")]
    )
    response = advisor.ask("How should I screen tenants?")
    assert TRUNCATED_NOTE in response.text
    assert response.stop_reason == "max_tokens"


def test_tool_loop_sends_results_as_one_user_message_of_strings(settings, store):
    finals = [
        tool_use_message(
            "mortgage_payment", {"principal": 225000, "annual_rate": 0.065, "years": 30}
        ),
        text_message("That payment is about $1,422 a month."),
    ]
    advisor, client = build_advisor(settings, store, finals)
    response = advisor.ask("What is the payment on a $300k purchase at 6.5%?")

    assert response.tool_calls == ["mortgage_payment"]
    second_messages = client.calls[1]["messages"]
    assert second_messages[-2]["role"] == "assistant"
    tool_turn = second_messages[-1]
    assert tool_turn["role"] == "user"
    assert len(tool_turn["content"]) == 1
    block = tool_turn["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "toolu_1"
    assert isinstance(block["content"], str)
    assert block["is_error"] is False
    assert "1422.15" in block["content"]


def test_tool_error_is_marked_as_an_error(settings, store):
    finals = [
        tool_use_message(
            "mortgage_payment", {"principal": 225000, "annual_rate": 6.5, "years": 30}
        ),
        text_message("Those units were off."),
    ]
    advisor, client = build_advisor(settings, store, finals)
    advisor.ask("What is the payment?")
    block = client.calls[1]["messages"][-1]["content"][0]
    assert block["is_error"] is True


def test_tool_loop_stops_at_the_iteration_cap(settings, store):
    advisor, client = build_advisor(settings, store, [tool_use_message() for _ in range(8)])
    response = advisor.ask("Analyze this deal for me")
    assert len(client.calls) <= 5
    assert "too many calculation steps" in response.text.lower()


def test_conversation_stores_the_bare_question_not_the_knowledge_block(settings, store):
    advisor, client = build_advisor(
        settings, store, [text_message("First."), text_message("Second.")]
    )
    conversation = Conversation(session_id="t")
    advisor.ask("How should I screen tenants?", conversation)

    assert conversation.messages[0] == {"role": "user", "content": "How should I screen tenants?"}
    assert conversation.messages[1]["role"] == "assistant"

    advisor.ask("What about the deposit rules for a Chicago two flat?", conversation)
    sent = client.calls[1]["messages"]
    assert sum(1 for m in sent if "<knowledge_base" in str(m.get("content", ""))) == 1


def test_follow_up_reuses_the_previous_question_for_retrieval(settings, store):
    advisor, client = build_advisor(
        settings, store, [text_message("Screening [S1]."), text_message("Same in the suburbs.")]
    )
    conversation = Conversation(session_id="t")
    advisor.ask("How should I screen tenants in Chicago?", conversation)
    response = advisor.ask("What about in Naperville?", conversation)

    assert "follow_up" in response.flags
    second_turn = str(client.calls[1]["messages"][-1]["content"])
    assert "<source" in second_turn


def test_a_failed_call_leaves_the_conversation_untouched(settings, store, monkeypatch):
    import anthropic

    advisor, client = build_advisor(settings, store, [text_message("never sent")])
    conversation = Conversation(session_id="t")

    def boom(**kwargs):
        raise anthropic.APIConnectionError(request=None)

    monkeypatch.setattr(client.beta.messages, "stream", boom)
    events = list(advisor.stream("How should I screen tenants?", conversation))
    assert events[-1].type == "error"
    assert conversation.messages == []


def test_missing_api_key_is_a_clear_error(settings, store):
    retriever = Retriever(store, None, settings)
    with pytest.raises(MissingApiKeyError) as excinfo:
        MarkAdvisor(settings, retriever, [], PROMPT)
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_empty_knowledge_base_says_so_without_calling_the_model(settings, empty_store):
    advisor, client = build_advisor(settings, empty_store, [text_message("should not be used")])
    response = advisor.ask("How should I screen tenants?")
    assert response.text == EMPTY_STORE_TEXT
    assert client.calls == []


def test_questions_are_logged_for_the_gaps_report(settings, store):
    advisor, _ = build_advisor(settings, store, [text_message("Screening matters [S1].")])
    advisor.ask("How should I screen tenants?")
    assert store.stats().questions_total == 1
