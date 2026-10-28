"""What the PO sent, as the channel hands it over (design §7)."""

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

    `message_ref` names the message the item was sent in; for a tap it is the message whose button
    was pressed. `reply_to_ref` is set on a reply. `text` is the text of a reply or text, and `data`
    the callback data of a tap.
    """

    update_id: int
    kind: InboundKind
    sender: int
    message_ref: str
    reply_to_ref: str | None = None
    text: str | None = None
    data: str | None = None


@dataclass(frozen=True, slots=True)
class Batch:
    """What one poll returned: the newest update id seen, and the items that could be read.

    An update the channel does not read (another kind, or a shape it does not know) moves
    `last_update_id` but gives no item.
    """

    last_update_id: int | None
    items: tuple[Inbound, ...]
