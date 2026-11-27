"""Queues a command reply, which has no signal, as parts that each fit one message."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.outbound import split_reply
from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.ports import Message

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from shikiri_yaku.domain.place import Place
    from shikiri_yaku.ports import Outbox


def queue_command_reply(
    outbox: Outbox,
    places: Sequence[Place],
    text: str,
    *,
    key: str,
    now: datetime,
    max_text: int,
    conversation_id: int | None = None,
) -> None:
    """Queue the reply as one journaled set of parts, each under its key and index.

    The whole reply is screened before it is cut, so a secret cannot pass the check by straddling
    a cut. A refused reply is queued whole, so the outbox refuses it and notices the PO as it does
    any other refusal. The set's claim and every part are one transaction, so a replay uses the
    complete stored response even if the live status has since changed.
    """
    if outbox.command_reply_queued(key):
        return
    parts = [text] if secret_form([text]) is not None else split_reply(text, max_text)
    outbox.queue_command_reply(
        places,
        [Message(part) for part in parts],
        key=key,
        now=now,
        conversation_id=conversation_id,
    )
