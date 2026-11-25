"""What the PO sent, as the channel hands it over."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class InboundKind(StrEnum):
    TAP = "tap"
    REPLY = "reply"
    TEXT = "text"
    TOPIC_CLOSED = "topic_closed"
    FILE = "file"


class FileRef(BaseModel):
    """A photo or document the PO sent: the id the channel gives it, its name and its size if known.

    The journal keeps it as JSON in the data column, which a file item does not otherwise use.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str
    name: str
    size: int | None = None


@dataclass(frozen=True, slots=True)
class Inbound:
    """One tap, reply or text with its sender.

    `chat` is the chat the item was sent in: the sender's private chat, or a shared chat.
    `message_ref` names the message the item was sent in; for a tap it is the message whose button
    was pressed.
    `reply_to_ref` is set on a reply. `text` is the text of a reply or text, and `data` the callback
    data of a tap. For a file, `text` is its caption and `file` names it. `thread` is the thread a
    reply, text or file was sent in, None outside a thread.
    """

    update_id: int
    kind: InboundKind
    sender: int
    chat: int
    message_ref: str
    reply_to_ref: str | None = None
    text: str | None = None
    data: str | None = None
    thread: int | None = None
    file: FileRef | None = None


def is_command(text: str | None) -> bool:
    """Return whether text names a chat command, so a reply that does takes the command route."""
    words = (text or "").split(maxsplit=1)
    return bool(words) and words[0].startswith("/")


@dataclass(frozen=True, slots=True)
class Batch:
    """Newest update id and readable items; unsupported content advances the id only."""

    last_update_id: int | None
    items: tuple[Inbound, ...]
