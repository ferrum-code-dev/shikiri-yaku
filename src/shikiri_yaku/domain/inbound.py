"""What the PO sent, as the channel hands it over."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from shikiri_yaku.domain.place import Place


class InboundKind(StrEnum):
    TAP = "tap"
    REPLY = "reply"
    TEXT = "text"
    ROOM_CLOSED = "room_closed"
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
class Command:
    """A text that reads `/name[@instance] arguments`.

    `word` is the first word as typed. `name` is lower case, because a phone keyboard capitalises
    the first letter. `instance` is None when the word has no `@`.
    """

    word: str
    name: str
    instance: str | None
    arguments: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Received:
    """One tap, reply or text with its sender, as the inbox returns it.

    `key` is the channel's id of the event, which only keeps it from being stored twice.

    `place` is where the item was sent; for a tap it is the place of the tapped message.
    `message_ref` names the message the item was sent in; for a tap it is the message whose button
    was pressed.
    `reply_to_ref` is set on a reply. `text` is the text of a reply or text, and `data` the callback
    data of a tap. For a file, `text` is its caption and `file` names it.
    """

    key: str
    kind: InboundKind
    sender: str
    place: Place
    message_ref: str
    reply_to_ref: str | None = None
    text: str | None = None
    data: str | None = None
    file: FileRef | None = None

    @property
    def command(self) -> Command | None:
        """The command the text names, or None, so a reply that does takes the command route."""
        words = (self.text or "").split(maxsplit=1)
        if not words or not words[0].startswith("/"):
            return None
        name, at, instance = words[0].partition("@")
        arguments = words[1].strip() if len(words) > 1 else ""
        return Command(words[0], name.lower(), instance if at else None, arguments)


@dataclass(frozen=True, slots=True, kw_only=True)
class Inbound(Received):
    """An event as the journal returns it, with the journal's own number of it."""

    id: int


@dataclass(frozen=True, slots=True)
class Batch:
    """What one read of the inbox gave; unsupported content advances the cursor only.

    `cursor` is None when nothing was read.
    """

    cursor: str | None
    events: tuple[Received, ...]
