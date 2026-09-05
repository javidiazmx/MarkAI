"""Mark himself: retrieval, the Claude call, the tool loop, and answer post-processing.

Two rules hold this module together:

* The system blocks are rendered once in ``__init__`` and never change, so the prompt cache
  keeps hitting. Volatile context lives in the user turn.
* ``Conversation`` is append-only and is mutated only after a successful answer, so a failed
  request can be retried and thinking blocks stay valid.

The Anthropic SDK runs on ``httpx2``. Never hand it an object from the ``httpx`` package the
ingesters use, and never catch ``httpx`` exceptions around an SDK call.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic

from markai.advisor.calculators import TOOL_DEFINITIONS, dispatch_tool
from markai.advisor.guardrails import (
    FLAG_FOLLOW_UP,
    FLAG_LEGAL,
    REFUSAL_TEXT,
    detect_flags,
    ensure_disclaimer,
    ensure_high_risk_response,
    is_follow_up,
    is_legal_topic,
    is_not_covered_answer,
)
from markai.advisor.prompt_builder import (
    build_business_block,
    build_citations,
    build_system_blocks,
    build_user_message,
    strip_all_markers,
    strip_unused_markers,
)
from markai.config import Settings
from markai.knowledge.retriever import Retriever
from markai.models import AdvisorResponse, RetrievedChunk
from markai.sources.manifest import BusinessProfile, ToolLink

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 5
FALLBACK_BETA = "server-side-fallback-2026-07-01"
EMPTY_STORE_TEXT = (
    "My knowledge base is empty, so I've got nothing to work from yet. "
    "Add your sources to sources/sources.yaml and run `mark ingest`."
)
TRUNCATED_NOTE = "(My answer got cut off — ask a narrower question or raise MARKAI_MAX_TOKENS.)"
TOOL_LIMIT_NOTE = "(That took too many calculation steps; here's what I have.)"


class MissingApiKeyError(RuntimeError):
    """Raised when no Anthropic API key is configured and no client was injected."""


@dataclass
class StreamEvent:
    """One event from :meth:`MarkAdvisor.stream`."""

    type: Literal["text", "tool_call", "final", "error"]
    text: str = ""
    response: AdvisorResponse | None = None


@dataclass
class Conversation:
    """Append-only chat history. Stores bare questions, not the built knowledge turn."""

    session_id: str | None = None
    messages: list[Any] = field(default_factory=list)
    last_question: str | None = None
    last_chunks: list[RetrievedChunk] = field(default_factory=list)
    turns: int = 0

    def add_turn(self, question: str, answer: str, chunks: list[RetrievedChunk]) -> None:
        self.messages.append({"role": "user", "content": question})
        self.messages.append({"role": "assistant", "content": answer})
        self.last_question = question
        self.last_chunks = list(chunks)
        self.turns += 1


class MarkAdvisor:
    """Answers a landlord's question from the curated knowledge base."""

    def __init__(
        self,
        settings: Settings,
        retriever: Retriever,
        tools: list[ToolLink],
        system_prompt: str,
        business: BusinessProfile | None = None,
        client: Any | None = None,
        store: Any | None = None,
    ) -> None:
        self.settings = settings
        self.retriever = retriever
        self.tools = list(tools or [])
        self.store = store
        self.system_blocks = build_system_blocks(system_prompt, build_business_block(business))

        if client is None:
            key = settings.anthropic_key()
            if not key:
                raise MissingApiKeyError(
                    "ANTHROPIC_API_KEY is not set. Run `mark init`, or copy .env.example to "
                    ".env and put your key in it."
                )
            client = anthropic.Anthropic(api_key=key)
        self.client = client

    # -- public API ---------------------------------------------------------------------

    def ask(self, question: str, conversation: Conversation | None = None) -> AdvisorResponse:
        """Answer a question, draining the stream. Errors come back as an AdvisorResponse."""
        response: AdvisorResponse | None = None
        error: str | None = None
        for event in self.stream(question, conversation):
            if event.type == "final":
                response = event.response
            elif event.type == "error":
                error = event.text
        if response is not None:
            return response
        return AdvisorResponse(text=error or "Something went wrong.", stop_reason="error")

    def stream(self, question: str, conversation: Conversation | None = None):
        """Yield text deltas, tool notices, then exactly one ``final`` (or ``error``)."""
        flags = detect_flags(question)
        query = question
        follow_up = bool(conversation and conversation.last_question and is_follow_up(question))
        if follow_up:
            query = f"{conversation.last_question} {question}"
            flags = sorted({*flags, FLAG_FOLLOW_UP})

        if self.retriever.is_empty():
            response = AdvisorResponse(
                text=EMPTY_STORE_TEXT, coverage="none", flags=flags, model=self.settings.model
            )
            yield StreamEvent("text", EMPTY_STORE_TEXT)
            yield StreamEvent("final", response=response)
            return

        retrieval = self.retriever.retrieve(query)
        carried: list[RetrievedChunk] = []
        if follow_up and retrieval.coverage in ("none", "weak") and conversation:
            room = max(self.settings.top_k - len(retrieval.chunks), 0)
            carried = list(conversation.last_chunks)[:room]

        user_text = build_user_message(question, retrieval, self.tools, flags, carried)
        api_messages: list[Any] = list(conversation.messages) if conversation else []
        api_messages.append({"role": "user", "content": user_text})

        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        text_parts: list[str] = []
        tool_calls: list[str] = []
        model_used = self.settings.model
        stop_reason: str | None = None
        refused = False

        for iteration in range(MAX_TOOL_ITERATIONS):
            try:
                with self.client.beta.messages.stream(
                    model=self.settings.model,
                    max_tokens=self.settings.request_max_tokens(),
                    system=self.system_blocks,
                    messages=api_messages,
                    tools=TOOL_DEFINITIONS,
                    thinking={"type": "adaptive"},
                    output_config={"effort": self.settings.effort},
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                    cache_control={"type": "ephemeral"},
                ) as stream:
                    for delta in stream.text_stream:
                        if delta:
                            yield StreamEvent("text", delta)
                    final = stream.get_final_message()
            except anthropic.AuthenticationError:
                yield StreamEvent(
                    "error",
                    "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in your .env file.",
                )
                return
            except anthropic.RateLimitError:
                yield StreamEvent(
                    "error", "Anthropic is rate-limiting this key. Wait a moment and try again."
                )
                return
            except anthropic.APIStatusError as exc:
                if exc.status_code >= 500:
                    yield StreamEvent(
                        "error", f"Anthropic had a server error ({exc.status_code}). Try again."
                    )
                else:
                    yield StreamEvent("error", f"Anthropic rejected the request: {exc.message}")
                return
            except anthropic.APIConnectionError:
                yield StreamEvent(
                    "error", "Could not reach Anthropic. Check the network and try again."
                )
                return
            except TypeError as exc:
                if "authentication method" in str(exc):
                    yield StreamEvent(
                        "error",
                        "ANTHROPIC_API_KEY is not set. Run `mark init`, or put your key in .env.",
                    )
                    return
                raise

            model_used = getattr(final, "model", model_used) or model_used
            for key in usage:
                usage[key] += getattr(final.usage, key, None) or 0
            stop_reason = final.stop_reason

            chunk_text = self._final_text(final)
            if chunk_text:
                text_parts.append(chunk_text)

            if stop_reason == "refusal":
                refused = True
                details = getattr(final, "stop_details", None)
                logger.info("refusal: category=%s", getattr(details, "category", None))
                break

            if stop_reason == "tool_use":
                if iteration == MAX_TOOL_ITERATIONS - 1:
                    text_parts.append(TOOL_LIMIT_NOTE)
                    break
                api_messages.append({"role": "assistant", "content": final.content})
                results = []
                for block in final.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    name = block.name
                    tool_calls.append(name)
                    yield StreamEvent("tool_call", name)
                    try:
                        result = dispatch_tool(name, dict(block.input or {}))
                    except Exception as exc:  # dispatch_tool is defensive, this is belt and braces
                        result = {"error": str(exc)}
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, sort_keys=True, default=str),
                            "is_error": "error" in result,
                        }
                    )
                if not results:
                    break
                api_messages.append({"role": "user", "content": results})
                continue

            if stop_reason == "max_tokens":
                text_parts.append(TRUNCATED_NOTE)
            break

        streamed = "\n\n".join(part for part in text_parts if part.strip())

        if refused:
            response = AdvisorResponse(
                text=REFUSAL_TEXT,
                citations=[],
                coverage=retrieval.coverage,
                flags=flags,
                usage=usage,
                model=model_used,
                stop_reason="refusal",
                tool_calls=tool_calls,
            )
        else:
            total_chunks = len(retrieval.chunks) + len(carried)
            valid = {f"S{i}" for i in range(1, total_chunks + 1)}
            text = strip_unused_markers(streamed, valid)
            text = ensure_high_risk_response(text, flags)
            if FLAG_LEGAL not in flags and is_legal_topic(text):
                flags = sorted({*flags, FLAG_LEGAL})
            text = ensure_disclaimer(text, flags)

            if text.startswith(streamed) and len(text) > len(streamed):
                yield StreamEvent("text", text[len(streamed) :])

            citations = build_citations(retrieval, text, carried)
            if not self.settings.show_citations:
                # The prompt already asks for none; this catches the stray one.
                text = strip_all_markers(text)
                citations = []

            response = AdvisorResponse(
                text=text,
                citations=citations,
                coverage=retrieval.coverage,
                flags=flags,
                usage=usage,
                model=model_used,
                stop_reason=stop_reason,
                tool_calls=tool_calls,
            )

        if conversation is not None:
            conversation.add_turn(question, response.text, list(retrieval.chunks) + carried)

        if self.store is not None:
            try:
                self.store.log_question(
                    conversation.session_id if conversation else None,
                    question,
                    response.coverage,
                    response.flags,
                    is_not_covered_answer(response.text),
                    usage,
                )
            except Exception as exc:  # logging must never break an answer
                logger.debug("question logging failed: %s", exc)

        logger.info(
            "answered: chars=%d coverage=%s flags=%s tools=%d in=%d out=%d cache_read=%d",
            len(response.text),
            response.coverage,
            ",".join(response.flags) or "-",
            len(tool_calls),
            usage["input_tokens"],
            usage["output_tokens"],
            usage["cache_read_input_tokens"],
        )
        yield StreamEvent("final", response=response)

    # -- helpers ------------------------------------------------------------------------

    @staticmethod
    def _final_text(final: Any) -> str:
        parts = [
            block.text
            for block in getattr(final, "content", [])
            if getattr(block, "type", None) == "text" and getattr(block, "text", "")
        ]
        return "".join(parts).strip()
