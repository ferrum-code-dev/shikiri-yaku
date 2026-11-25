"""What the PO sent, as the channel hands it over."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ATTACHMENT: Final = "attachment"
ATTACHMENT_PREFIX: Final = "attachment:"
FILE_ID_LIMIT: Final = 512
FILE_NAME_LIMIT: Final = 255


class Attachment(BaseModel):
    """Bounded metadata needed to retrieve one channel attachment."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    file_id: str = Field(min_length=1, max_length=FILE_ID_LIMIT)
    name: str = Field(min_length=1, max_length=FILE_NAME_LIMIT)
    size: int | None = Field(default=None, ge=0)


def attachment_data(attachment: Attachment) -> str:
    return ATTACHMENT_PREFIX + attachment.model_dump_json()


def is_attachment(data: str | None) -> bool:
    return data == ATTACHMENT or bool(data and data.startswith(ATTACHMENT_PREFIX))


def read_attachment(data: str | None) -> Attachment | None:
    if not data or not data.startswith(ATTACHMENT_PREFIX):
        return None
    try:
        return Attachment.model_validate_json(data.removeprefix(ATTACHMENT_PREFIX))
    except ValidationError:
        return None


class InboundKind(StrEnum):
    TAP = "tap"
    REPLY = "reply"
    TEXT = "text"
    TOPIC_CLOSED = "topic_closed"


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


def is_command(text: str | None) -> bool:
    """Return whether text names a chat command, so a reply that does takes the command route."""
    words = (text or "").split(maxsplit=1)
    return bool(words) and words[0].startswith("/")


@dataclass(frozen=True, slots=True)
class Batch:
    """Newest update id and readable items; unsupported content advances the id only."""

    last_update_id: int | None
    items: tuple[Inbound, ...]
