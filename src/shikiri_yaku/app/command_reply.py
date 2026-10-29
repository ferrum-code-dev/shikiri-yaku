"""Queues a command reply, which has no signal, as parts that each fit one message (design §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.outbound import split_reply
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.ports import MESSAGE_TEXT_LIMIT, Message

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Outbox


def item_destination(channel: str, item: Inbound) -> Destination:
    """Return the chat and forum topic the PO sent the item in, where its command reply goes."""
    return Destination(channel, str(item.chat), item.thread)


def queue_command_reply(
    outbox: Outbox,
    destinations: Sequence[Destination],
    text: str,
    *,
    key: str,
    now: datetime,
) -> None:
    """Queue the reply as one journaled group of parts, each under its key and index.

    The whole reply is screened before it is cut, so a secret cannot pass the check by straddling
    a cut. A refused reply is queued whole, so the outbox refuses it and notices the PO as it does
    any other refusal. The group claim and every part are one transaction, so a replay uses the
    complete stored response even if the live status has since changed.
    """
    if outbox.command_reply_queued(key):
        return
    parts = [text] if secret_form([text]) is not None else split_reply(text, MESSAGE_TEXT_LIMIT)
    outbox.queue_command_reply(destinations, [Message(part) for part in parts], key=key, now=now)
