"""What the PO sent, as the channel hands it over."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class InboundKind(StrEnum):
    TAP = "tap"
    REPLY = "reply"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class Inbound:
    """One tap, reply or text with its sender.

    `chat` is the chat the item was sent in: the sender's private chat, or a shared chat.
    `message_ref` names the message the item was sent in; for a tap it is the message whose button
    was pressed.
    `reply_to_ref` is set on a reply. `text` is the text of a reply or text, and `data` the callback
    data of a tap. `thread` is the thread a reply or text was sent in, None outside a thread.
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


@dataclass(frozen=True, slots=True)
class Batch:
    """Newest update id and readable items; unsupported content advances the id only."""

    last_update_id: int | None
    items: tuple[Inbound, ...]
