"""Test doubles: a fake Anthropic client, a fake embedder, and fake YouTube captions.

The Anthropic SDK runs on httpx2, which respx cannot intercept, so the client is faked at the
object level instead. The final messages are real SDK types so a shape mistake fails here
rather than in production.
"""

from __future__ import annotations

import hashlib
from typing import Any

from anthropic.types.beta import BetaMessage, BetaTextBlock, BetaToolUseBlock, BetaUsage


def text_message(
    text: str,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5",
    **usage: int,
) -> BetaMessage:
    """A finished assistant message containing one text block."""
    return BetaMessage(
        id="msg_text",
        type="message",
        role="assistant",
        model=model,
        content=[BetaTextBlock(type="text", text=text)],
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=BetaUsage(
            input_tokens=usage.get("input_tokens", 10), output_tokens=usage.get("output_tokens", 5)
        ),
    )


def tool_use_message(
    name: str = "analyze_deal",
    tool_input: dict[str, Any] | None = None,
    text: str = "",
    model: str = "claude-opus-5",
) -> BetaMessage:
    """An assistant message that stops to call a tool."""
    content: list[Any] = []
    if text:
        content.append(BetaTextBlock(type="text", text=text))
    content.append(
        BetaToolUseBlock(
            type="tool_use",
            id="toolu_1",
            name=name,
            input=tool_input if tool_input is not None else {"price": 300000},
        )
    )
    return BetaMessage(
        id="msg_tool",
        type="message",
        role="assistant",
        model=model,
        content=content,
        stop_reason="tool_use",
        stop_sequence=None,
        usage=BetaUsage(input_tokens=12, output_tokens=8),
    )


def refusal_message(model: str = "claude-opus-5") -> BetaMessage:
    """A message the safety classifiers declined, with a partial text block."""
    return BetaMessage(
        id="msg_refusal",
        type="message",
        role="assistant",
        model=model,
        content=[BetaTextBlock(type="text", text="Here is how you could")],
        stop_reason="refusal",
        stop_sequence=None,
        usage=BetaUsage(input_tokens=9, output_tokens=4),
    )


class FakeStream:
    """Context manager mirroring ``client.beta.messages.stream(...)``."""

    def __init__(self, final: BetaMessage) -> None:
        self._final = final

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    @property
    def text_stream(self):
        for block in self._final.content:
            if getattr(block, "type", None) == "text" and block.text:
                for i in range(0, len(block.text), 12):
                    yield block.text[i : i + 12]

    def get_final_message(self) -> BetaMessage:
        return self._final

    def close(self) -> None:
        return None


class FakeBetaMessages:
    def __init__(self, finals: list[BetaMessage]) -> None:
        self._finals = list(finals)
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        final = self._finals.pop(0) if self._finals else text_message("Done.")
        return FakeStream(final)


class _Beta:
    def __init__(self, messages: FakeBetaMessages) -> None:
        self.messages = messages


class FakeAnthropic:
    """Stands in for ``anthropic.Anthropic`` in tests."""

    def __init__(self, finals: list[BetaMessage] | None = None) -> None:
        self.api_key = "test"
        self.beta = _Beta(FakeBetaMessages(finals or [text_message("Hello.")]))

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.beta.messages.calls


class FakeEmbedder:
    """Deterministic pseudo-embeddings, no network."""

    name = "fake-embeddings"
    dimensions = 16

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.lower().encode("utf-8")).digest()
        return [digest[i] / 255.0 for i in range(self.dimensions)]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class FakeSnippet:
    def __init__(self, text: str, start: float, duration: float) -> None:
        self.text = text
        self.start = start
        self.duration = duration


class FakeFetched:
    def __init__(self, snippets: list[FakeSnippet]) -> None:
        self.snippets = snippets

    def __iter__(self):
        return iter(self.snippets)

    def to_raw_data(self) -> list[dict[str, Any]]:
        return [{"text": s.text, "start": s.start, "duration": s.duration} for s in self.snippets]


class FakeTranscriptApi:
    """Stands in for ``YouTubeTranscriptApi``; raises ``error`` when one is given."""

    def __init__(
        self, snippets: list[tuple[str, float, float]] | None = None, error: Exception | None = None
    ) -> None:
        self._snippets = snippets or [
            ("Security deposits in Chicago have strict rules", 0.0, 5.0),
            ("You must pay interest annually on the deposit", 5.0, 5.0),
            ("Keep it in a separate Illinois account", 10.0, 5.0),
        ]
        self._error = error
        self.calls: list[tuple[str, list[str]]] = []

    def fetch(self, video_id: str, languages: list[str] | None = None) -> FakeFetched:
        self.calls.append((video_id, list(languages or [])))
        if self._error:
            raise self._error
        return FakeFetched([FakeSnippet(t, s, d) for t, s, d in self._snippets])


class FakeAdvisor:
    """Minimal advisor for web tests: streams a fixed answer."""

    def __init__(
        self, text: str = "Here is the answer [S1].", citations: list[Any] | None = None
    ) -> None:
        self.text = text
        self.citations = citations or []
        self.questions: list[str] = []

    def stream(self, question: str, conversation: Any = None):
        from markai.advisor.mark import StreamEvent
        from markai.models import AdvisorResponse

        self.questions.append(question)
        yield StreamEvent("text", self.text)
        yield StreamEvent(
            "final",
            response=AdvisorResponse(
                text=self.text,
                citations=self.citations,
                coverage="covered",
                flags=[],
                usage={"input_tokens": 1, "output_tokens": 2},
                model="claude-opus-5",
                stop_reason="end_turn",
            ),
        )
