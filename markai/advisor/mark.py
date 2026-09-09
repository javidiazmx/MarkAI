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
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import anthropic

from markai.advisor.attachments import to_content_blocks
from markai.advisor.calculators import TOOL_DEFINITIONS, dispatch_tool
from markai.advisor.guardrails import (
    FLAG_FOLLOW_UP,
    FLAG_HIGH_RISK,
    FLAG_LEGAL,
    LEGAL_DISCLAIMER,
    REFUSAL_TEXT,
    detect_flags,
    ensure_disclaimer,
    ensure_high_risk_response,
    is_follow_up,
    is_legal_topic,
    is_not_covered_answer,
    is_small_talk,
    plain_punctuation,
    strip_disclaimer,
)
from markai.advisor.prompt_builder import (
    build_business_block,
    build_citations,
    build_facts_block,
    build_portfolio_block,
    build_system_blocks,
    build_user_message,
    strip_all_markers,
    strip_unused_markers,
)
from markai.config import Settings
from markai.knowledge.episodes import EPISODE_TOOL, run_episode_tool
from markai.knowledge.retriever import Retriever
from markai.models import AdvisorResponse, RetrievedChunk
from markai.sources.facts import FactBook
from markai.sources.facts import select as select_facts
from markai.sources.manifest import BusinessProfile, ToolLink

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 5
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FAST_MODE_BETA = "fast-mode-2026-02-01"
EMPTY_STORE_TEXT = (
    "My knowledge base is empty, so I've got nothing to work from yet. "
    "Add your sources to sources/sources.yaml and run `mark ingest`."
)
TRUNCATED_NOTE = "(My answer got cut off. Ask a narrower question, or raise MARKAI_MAX_TOKENS.)"
TOOL_LIMIT_NOTE = "(That took too many calculation steps; here's what I have.)"


class MissingApiKeyError(RuntimeError):
    """Raised when no Anthropic API key is configured and no client was injected."""


# --- how hard to think about one question ---------------------------------------------

# Words that mean the answer is arithmetic or judgement, not a lookup. "Should I" is
# deliberately absent: half of all questions are phrased that way and most of them are
# asking what the process is, not for a decision to be weighed.
_ANALYSIS = re.compile(
    r"\b(cash\s*flow|cap\s*rate|cash[- ]on[- ]cash|dscr|noi|roi|mortgage|refinanc|"
    r"underwrit|analy[sz]e|worth it|pro\s*forma|deal|appreciat|"
    r"vale la pena|financ|rendimiento|flujo de efectivo)\b",
    re.IGNORECASE,
)
# A price in the question is the other reliable sign that something has to be worked out.
_MONEY = re.compile(r"[$€]\s?\d|\b\d[\d,.]*\s?(k|mil|million|millones)\b", re.IGNORECASE)
# A short question with a plain answer: "how long", "when does", "cuanto tiempo tengo".
# Neither "how do I" nor "como" is here: "how do I evict a tenant" is a process with steps
# to get in the right order, and that is worth thinking about.
# The Spanish stems carry no trailing \b on purpose: "cuanto", "cuando" and "cuantos" all
# continue into a word character, so a boundary there would match none of them.
_LOOKUP = re.compile(
    r"^\s*(?:(?:how (?:long|much|many)|when|what|where|who|which|is|are|does|do|can)\b"
    r"|(?:cu[aá]nt|cu[aá]nd|qu[eé]|d[oó]nd|qui[eé]n|puedo|se puede|hay que|tengo que|"
    r"es legal))",
    re.IGNORECASE,
)
LOOKUP_MAX_WORDS = 16


def effort_for(
    question: str,
    retrieval: Any,
    flags: list[str],
    has_attachments: bool,
    settings: Settings,
) -> str:
    """Pick the thinking effort for this question.

    The default earns its cost on hard questions and wastes it on "how long do I have to
    return a deposit", which the sources answer outright. Three cases:

    - **high** for anything that has to be worked out rather than looked up: money,
      whether a deal is worth doing, a photo to read, or a request that has to be refused
      carefully.
    - **low** for a short lookup the knowledge base already covers. Faster to first word
      and cheaper, and the answer is in the passages either way.
    - the configured default for everything else, which is most of it.
    """
    if (
        has_attachments
        or FLAG_HIGH_RISK in flags
        or _ANALYSIS.search(question)
        or _MONEY.search(question)
    ):
        return "high"
    covered = getattr(retrieval, "coverage", "") == "covered"
    short = len(question.split()) <= LOOKUP_MAX_WORDS
    if covered and short and _LOOKUP.match(question.strip()):
        return "low"
    return settings.effort


@dataclass
class StreamEvent:
    """One event from :meth:`MarkAdvisor.stream`."""

    type: Literal["text", "thinking", "tool_call", "final", "error"]
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
    # The legal disclaimer is said once per conversation, not under every answer.
    disclaimer_given: bool = False

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
        facts: FactBook | None = None,
    ) -> None:
        self.settings = settings
        self.retriever = retriever
        self.tools = list(tools or [])
        self.store = store
        # Built once and never rebuilt: tools render before the system blocks, so a list
        # that changed between requests would move the cache prefix and lose the cache.
        self.tool_definitions = [*TOOL_DEFINITIONS, EPISODE_TOOL]
        # The owner's rules and prices. They ride in the user turn, not the system prompt:
        # they are picked per question and stamped with today's date, and either of those
        # in a system block would move the cached prefix.
        self.facts = facts or FactBook()
        self.system_blocks = build_system_blocks(
            system_prompt, build_business_block(business), settings.cache_ttl
        )

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

    def ask(
        self,
        question: str,
        conversation: Conversation | None = None,
        attachments: list[Any] | None = None,
        portfolio: list[Any] | None = None,
        neighborhood: str | None = None,
    ) -> AdvisorResponse:
        """Answer a question, draining the stream. Errors come back as an AdvisorResponse."""
        response: AdvisorResponse | None = None
        error: str | None = None
        for event in self.stream(question, conversation, attachments, portfolio, neighborhood):
            if event.type == "final":
                response = event.response
            elif event.type == "error":
                error = event.text
        if response is not None:
            return response
        return AdvisorResponse(text=error or "Something went wrong.", stop_reason="error")

    def stream(
        self,
        question: str,
        conversation: Conversation | None = None,
        attachments: list[Any] | None = None,
        portfolio: list[Any] | None = None,
        neighborhood: str | None = None,
    ):
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

        selected_rules, selected_costs = select_facts(self.facts, question)
        facts_block = build_facts_block(selected_rules, selected_costs, date.today())
        user_text = build_user_message(
            question,
            retrieval,
            self.tools,
            flags,
            carried,
            facts_block,
            build_portfolio_block(list(portfolio or []), neighborhood),
            date.today(),
        )
        api_messages: list[Any] = list(conversation.messages) if conversation else []
        if attachments:
            # Files first, then the knowledge base and the question, which is the order the
            # API documentation asks for and reads naturally to the model.
            content: list[Any] = to_content_blocks(attachments)
            content.append({"type": "text", "text": user_text})
            api_messages.append({"role": "user", "content": content})
            logger.info("question with %d attachment(s)", len(attachments))
        else:
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

        # The system prefix is cached by its own breakpoint and is the only part that
        # repeats between questions. A second, automatic breakpoint at the end of the
        # messages would write the retrieved passages too - and those are different every
        # question, so that entry is written at 1.25x and never read. Only ask for it when
        # this prefix really will be sent again: inside a tool loop, or in a conversation
        # whose next turn replays this history.
        reuses_history = len(api_messages) > 1

        # Fast mode has its own rate limit; a 429 there drops this question back to standard
        # speed rather than failing it. Kept per-question, so one 429 does not disable it.
        fast = self.settings.fast_mode

        # How hard to think about this one. Top-level rather than the per-message beta: a
        # change mid-conversation costs one rewrite of the (small) message cache, and that
        # is a better trade than a beta parameter that would 400 every request if its shape
        # is ever wrong.
        effort = effort_for(question, retrieval, flags, bool(attachments), self.settings)

        for iteration in range(MAX_TOOL_ITERATIONS):
            cache_messages = reuses_history or iteration > 0
            try:
                with self.client.beta.messages.stream(
                    model=self.settings.model,
                    max_tokens=self.settings.request_max_tokens(),
                    system=self.system_blocks,
                    messages=api_messages,
                    tools=self.tool_definitions,
                    # "summarized" instead of the default "omitted". Opus 5 thinks before
                    # it writes, and with the reasoning hidden that is a blank screen for
                    # several seconds. Streamed, the landlord watches Jay work through it,
                    # which is both the honest picture and the difference between waiting
                    # and being ignored. It costs nothing: thinking is billed either way.
                    thinking={"type": "adaptive", "display": "summarized"},
                    output_config={"effort": effort},
                    betas=[FALLBACK_BETA, FAST_MODE_BETA] if fast else [FALLBACK_BETA],
                    fallbacks="default",
                    **({"speed": "fast"} if fast else {}),
                    **({"cache_control": {"type": "ephemeral"}} if cache_messages else {}),
                ) as stream:
                    # Raw events rather than `text_stream`, which drops the thinking.
                    for event in stream:
                        if getattr(event, "type", None) != "content_block_delta":
                            continue
                        delta = event.delta
                        kind = getattr(delta, "type", "")
                        if kind == "text_delta" and delta.text:
                            yield StreamEvent("text", delta.text)
                        elif kind == "thinking_delta" and delta.thinking:
                            yield StreamEvent("thinking", delta.thinking)
                    final = stream.get_final_message()
            except anthropic.AuthenticationError:
                yield StreamEvent(
                    "error",
                    "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in your .env file.",
                )
                return
            except anthropic.RateLimitError:
                if fast:
                    # Fast mode is rate-limited separately. Standard speed still has room.
                    logger.info("fast mode was rate-limited; retrying at standard speed")
                    fast = False
                    continue
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
                        if name == EPISODE_TOOL["name"]:
                            result = run_episode_tool(self.retriever, dict(block.input or {}))
                        else:
                            result = dispatch_tool(name, dict(block.input or {}))
                    except Exception as exc:  # the dispatchers are defensive; belt and braces
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
            text = plain_punctuation(strip_unused_markers(streamed, valid))
            text = ensure_high_risk_response(text, flags)
            if FLAG_LEGAL not in flags and is_legal_topic(text):
                flags = sorted({*flags, FLAG_LEGAL})
            # A conversation carries the notice once at most; where the surface shows a
            # standing notice of its own, not at all. Either way the decision is made here
            # and not by the model, which will write one under every answer given the
            # chance - and did, until the prompt stopped asking for it.
            wanted = self.settings.legal_disclaimer_in_answers and not (
                conversation and conversation.disclaimer_given
            )
            if wanted:
                text = ensure_disclaimer(text, flags)
            elif FLAG_HIGH_RISK not in flags:
                # The fair-housing refusal is owner-approved wording that carries the
                # notice on purpose. Nothing trims that one.
                text = strip_disclaimer(text)
            if conversation is not None and LEGAL_DISCLAIMER in text:
                conversation.disclaimer_given = True

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
                    # From the retrieval, not from the model's wording. Jay no longer
                    # announces a gap out loud, so this is the only place it is recorded,
                    # and it has to catch "weak" as well as "none": a thin match is exactly
                    # the near miss he used to explain away to the landlord and now keeps
                    # to himself. `mark gaps` shows which of the two it was. Hello and
                    # "who are you" are not content the owner should go write.
                    not is_small_talk(question)
                    and (
                        response.coverage in ("none", "weak")
                        or is_not_covered_answer(response.text)
                    ),
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
