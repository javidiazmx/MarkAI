"""Files a landlord attaches to a question: a photo of the damage, a lease, a notice.

Two rules shape this module.

Everything inside an attachment is untrusted. A PDF can contain "ignore your previous
instructions"; a photo of a notice can have text in it. The content blocks built here are
labelled as material to examine, and the system prompt says to treat them as data.

Nothing is stored. A file is turned into content blocks for one request and dropped. There
is no upload directory to secure, nothing to leak later, and no retention question to
answer, which for photographs of someone's home is the right default.
"""

from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# What Claude can actually read. Anything else is refused by name rather than sent and
# rejected upstream.
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
DOCUMENT_TYPES = {"application/pdf"}
TEXT_TYPES = {"text/plain", "text/csv", "text/markdown"}
ALLOWED_TYPES = IMAGE_TYPES | DOCUMENT_TYPES | TEXT_TYPES

# The API caps a request at 32 MB and base64 inflates by a third, so the real ceiling is
# lower than it looks. These are per file and per request.
MAX_BYTES_PER_FILE = 8_000_000
MAX_BYTES_TOTAL = 20_000_000
MAX_FILES = 5
MAX_TEXT_CHARS = 200_000


class AttachmentError(ValueError):
    """Something about the file means it cannot be sent. The message is shown to the user."""


@dataclass
class Attachment:
    """One file, already decoded, on its way into a single request."""

    name: str
    media_type: str
    data: bytes

    @property
    def kind(self) -> str:
        if self.media_type in IMAGE_TYPES:
            return "image"
        if self.media_type in DOCUMENT_TYPES:
            return "document"
        return "text"


def _sniff(data: bytes, claimed: str) -> str:
    """Trust the bytes over the label. A browser's type comes from the file extension."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    return claimed


def decode(name: str, media_type: str, base64_data: str) -> Attachment:
    """Validate one attachment and return it decoded. Raises ``AttachmentError``."""
    clean_name = (name or "file").strip()[:120] or "file"
    try:
        data = base64.b64decode(base64_data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError(f"{clean_name} could not be read.") from exc

    if not data:
        raise AttachmentError(f"{clean_name} is empty.")
    if len(data) > MAX_BYTES_PER_FILE:
        raise AttachmentError(
            f"{clean_name} is {len(data) / 1e6:.1f} MB. The limit is "
            f"{MAX_BYTES_PER_FILE / 1e6:.0f} MB per file."
        )

    actual = _sniff(data, (media_type or "").split(";")[0].strip().lower())
    if actual not in ALLOWED_TYPES:
        raise AttachmentError(
            f"{clean_name} is a {actual or 'unknown'} file. Attach a photo (JPEG, PNG, "
            f"GIF, WebP), a PDF, or a text file."
        )
    return Attachment(name=clean_name, media_type=actual, data=data)


def decode_all(raw: list[dict[str, Any]] | None) -> list[Attachment]:
    """Validate a whole batch, refusing the batch rather than silently dropping any of it."""
    items = raw or []
    if not items:
        return []
    if len(items) > MAX_FILES:
        raise AttachmentError(f"{len(items)} files at once. The limit is {MAX_FILES}.")

    decoded = [
        decode(
            str(item.get("name", "")),
            str(item.get("media_type", "")),
            str(item.get("data", "")),
        )
        for item in items
    ]
    total = sum(len(a.data) for a in decoded)
    if total > MAX_BYTES_TOTAL:
        raise AttachmentError(
            f"{total / 1e6:.1f} MB in total. The limit is {MAX_BYTES_TOTAL / 1e6:.0f} MB "
            f"per message."
        )
    return decoded


def to_content_blocks(attachments: list[Attachment]) -> list[dict[str, Any]]:
    """Content blocks for one user turn, labelled as material to examine.

    Documents and images go before the text of the question, which is what the API
    documentation asks for.
    """
    blocks: list[dict[str, Any]] = []
    for index, item in enumerate(attachments, start=1):
        label = (
            f'<attachment index="{index}" name="{_escape_attr(item.name)}" '
            f'type="{item.media_type}">'
        )
        blocks.append({"type": "text", "text": label})
        payload = base64.b64encode(item.data).decode("ascii")
        if item.kind == "image":
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": item.media_type, "data": payload},
                }
            )
        elif item.kind == "document":
            blocks.append(
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": payload},
                }
            )
        else:
            text = item.data.decode("utf-8", errors="replace")[:MAX_TEXT_CHARS]
            blocks.append({"type": "text", "text": _escape_text(text)})
        blocks.append({"type": "text", "text": "</attachment>"})
    return blocks


def _escape_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(value: str) -> str:
    return _escape_text(value).replace('"', "&quot;")


def describe(attachments: list[Attachment]) -> str:
    """A one-line summary for the log and for the conversation history. Never the contents."""
    if not attachments:
        return ""
    parts = [f"{a.name} ({a.kind}, {len(a.data) / 1000:.0f} kB)" for a in attachments]
    return "attached: " + ", ".join(parts)
