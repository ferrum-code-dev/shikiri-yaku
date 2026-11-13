"""A platform that accepts every copy, and the one step that delivers what a test queued."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from shikiri_yaku.app.copy_editor import CopyEditor
from shikiri_yaku.app.outbox_sender import OutboxSender

if TYPE_CHECKING:
    from shikiri_yaku.app.outbox_sender import TopicGone
    from shikiri_yaku.domain.schedule import Destination
    from shikiri_yaku.ports import Channel, Clock, FinalEdits, Message, Outbox


class Platform:
    """Accepts every send and edit, and records the edits; each `edit_failures` is raised first."""

    def __init__(self) -> None:
        self.edits: list[tuple[str, Message]] = []
        self.edit_failures: list[Exception] = []
        self._sent = 0

    async def send(self, destination: Destination, message: Message) -> str:
        del destination, message
        self._sent += 1
        return f"copy-{self._sent}"

    async def edit(self, message_ref: str, message: Message) -> None:
        if self.edit_failures:
            raise self.edit_failures.pop(0)
        self.edits.append((message_ref, message))

    async def delete(self, message_ref: str) -> None:
        raise NotImplementedError


def deliver(
    outbox: Outbox,
    final: FinalEdits,
    clock: Clock,
    platform: Platform,
    *,
    gone: TopicGone | None = None,
) -> None:
    """Send the queued copies, then edit the sent copies to their final state, as the workers do."""
    channel = cast("Channel", platform)
    sender = OutboxSender(outbox, channel, clock, interval_s=1, gone=gone)
    editor = CopyEditor(final, channel, clock, interval_s=1)
    asyncio.run(sender.tick())
    asyncio.run(editor.tick())
